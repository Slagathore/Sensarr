"""Shows tracking: episode file parsing, sync, missing/upcoming computation."""
from datetime import date, timedelta

import pytest

import show_tracker
import shows_store
from media_lookup import EpisodeInfo
from show_tracker import _parse_episode_from_file


def test_parse_episode_standard_forms():
    assert _parse_episode_from_file("Severance.S02E05.1080p.WEB.mkv") == (2, 5)
    assert _parse_episode_from_file("The Bear 3x01 HDTV.mkv") == (3, 1)


def test_parse_episode_absolute_anime_numbering():
    # Season None = "not in the filename"; the folder scanner derives it from
    # the parent "Season NN" folder, defaulting to 1 for flat folders.
    assert _parse_episode_from_file("[SubsPlease] Frieren - 12 (1080p) [ABCD].mkv") == (None, 12)
    # Resolution and year tokens must not be mistaken for episode numbers.
    assert _parse_episode_from_file("[Group] Show - 03 [720p][2023].mkv") == (None, 3)


def test_parse_episode_word_form():
    # DVD-rip style "Episode NN - Title" (season comes from the folder).
    assert _parse_episode_from_file("Episode 05 - Stan of Arabia - Part 1.mp4") == (None, 5)


def test_scan_derives_season_from_folder(tmp_path):
    show = tmp_path / "Folder Show"
    (show / "Season 02").mkdir(parents=True)
    (show / "Season 02" / "Episode 03 - Some Title.mp4").write_bytes(b"x")
    found = show_tracker._scan_folders_for_episodes((str(show),))
    assert (2, 3) in found


def test_parse_episode_no_number():
    assert _parse_episode_from_file("Some Movie.mkv") is None


def test_parse_episode_version_suffix_and_bare_e():
    # Version suffixes sit flush against the number ("v2") — real Kaiju No. 8 naming.
    assert _parse_episode_from_file("[Judas] Kaijuu 8 Gou - S01E05v2.mkv") == (1, 5)
    # Bare "E01" marker with no season token — real Medaka Box naming.
    assert _parse_episode_from_file("[Ranger] Medaka Box - E07 [BD][1080p].mkv") == (None, 7)
    # "Wall-E 2" style names must NOT hit the bare-E pattern.
    assert _parse_episode_from_file("WALL-E (2008).mkv") is None


def test_scan_skips_extras_folders(tmp_path):
    show = tmp_path / "Some Show"
    (show / "Season 01").mkdir(parents=True)
    (show / "Trailers").mkdir()
    (show / "Other").mkdir()
    (show / "Season 01" / "Some Show - S01E01.mkv").write_bytes(b"x")
    (show / "Trailers" / "Season 1 Trailer 1.mkv").write_bytes(b"x")
    (show / "Other" / "Some Show - NCOP 1.mkv").write_bytes(b"x")
    found = show_tracker._scan_folders_for_episodes((str(show),))
    assert set(found) == {(1, 1)}


def test_remap_disk_seasons_to_continuous_tracker():
    # Tracker (TMDB) merged two cours into one 24-episode "Season 1";
    # disk uses Season 01 (1-12) + Season 02 (1-12). Real Mashle layout.
    from media_lookup import EpisodeInfo
    episodes = [EpisodeInfo(1, n, f"ep{n}", "2023-01-01") for n in range(1, 25)]
    found = {(1, n): f"/s1/e{n}.mkv" for n in range(1, 13)}
    found |= {(2, n): f"/s2/e{n}.mkv" for n in range(1, 13)}
    remapped, moved = show_tracker._remap_disk_seasons_to_tracker(found, episodes)
    assert moved
    assert set(remapped) == {(1, n) for n in range(1, 25)}
    assert remapped[(1, 13)] == "/s2/e1.mkv"
    # A show whose tracker HAS season 2 is left completely alone.
    episodes2 = ([EpisodeInfo(1, n, "", None) for n in range(1, 13)]
                 + [EpisodeInfo(2, n, "", None) for n in range(1, 13)])
    same, moved2 = show_tracker._remap_disk_seasons_to_tracker(dict(found), episodes2)
    assert not moved2 and same == found


@pytest.fixture()
def tracked_show(tmp_path):
    show_dir = tmp_path / "Test Show"
    (show_dir / "Season 01").mkdir(parents=True)
    (show_dir / "Season 01" / "Test Show - S01E01.mkv").write_bytes(b"x")
    (show_dir / "Season 01" / "Test Show - S01E02.mkv").write_bytes(b"x")
    show_id = shows_store.upsert_show(
        title="Test Show", media_type="tv", source="tvdb", external_id="tt-test-1",
    )
    shows_store.add_show_folder(show_id, str(show_dir))
    return show_id


def test_sync_marks_missing_and_upcoming(tracked_show, monkeypatch):
    today = date.today()
    aired = (today - timedelta(days=30)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()
    soon = (today + timedelta(days=5)).isoformat()
    far = (today + timedelta(days=60)).isoformat()

    fake_episodes = [
        EpisodeInfo(1, 1, "Pilot", aired),
        EpisodeInfo(1, 2, "Second", aired),
        EpisodeInfo(1, 3, "Third (missing)", recent),
        EpisodeInfo(1, 4, "Soon", soon),
        EpisodeInfo(1, 5, "Far", far),
    ]
    monkeypatch.setitem(
        show_tracker.EPISODE_FETCHERS, "tvdb",
        (lambda _id: fake_episodes, lambda _id: "Continuing"),
    )

    summary = show_tracker.sync_show(tracked_show)
    # Message quotes the same numbers as the table: have/known, missing.
    assert "2/5 on disk" in summary and "1 missing" in summary

    missing = shows_store.missing_episodes(tracked_show)
    assert [(e.season, e.episode) for e in missing] == [(1, 3)]  # aired, no file
    # Unaired episodes are upcoming, not missing.
    upcoming = shows_store.upcoming_episodes(days=14)
    upcoming_eps = [e.episode for s, e in upcoming if s.show_id == tracked_show]
    assert 4 in upcoming_eps          # airs in 5 days → inside the window
    assert 5 not in upcoming_eps      # airs in 60 days → outside the window

    show = shows_store.get_show(tracked_show)
    assert show is not None
    assert show.status == "Continuing"
    assert show.have_count == 2 and show.missing_count == 1
    assert show.next_air_date == soon


def test_sync_is_idempotent(tracked_show, monkeypatch):
    eps = [EpisodeInfo(1, 1, "Pilot", "2020-01-01")]
    monkeypatch.setitem(
        show_tracker.EPISODE_FETCHERS, "tvdb",
        (lambda _id: eps, lambda _id: "Ended"),
    )
    show_tracker.sync_show(tracked_show)
    show_tracker.sync_show(tracked_show)
    all_eps = shows_store.list_episodes(tracked_show)
    assert len([e for e in all_eps if (e.season, e.episode) == (1, 1)]) == 1


def test_seasons_split_across_drives(tmp_path, monkeypatch):
    drive_a = tmp_path / "d" / "Split Show"
    drive_b = tmp_path / "i" / "Split Show"
    (drive_a / "Season 01").mkdir(parents=True)
    (drive_b / "Season 02").mkdir(parents=True)
    (drive_a / "Season 01" / "Split Show - S01E01.mkv").write_bytes(b"x")
    (drive_b / "Season 02" / "Split Show - S02E01.mkv").write_bytes(b"x")

    show_id = shows_store.upsert_show(
        title="Split Show", media_type="tv", source="tvdb", external_id="tt-split",
    )
    shows_store.add_show_folder(show_id, str(drive_a))
    shows_store.add_show_folder(show_id, str(drive_b))

    eps = [EpisodeInfo(1, 1, "A", "2020-01-01"), EpisodeInfo(2, 1, "B", "2021-01-01")]
    monkeypatch.setitem(
        show_tracker.EPISODE_FETCHERS, "tvdb",
        (lambda _id: eps, lambda _id: "Ended"),
    )
    show_tracker.sync_show(show_id)

    show = shows_store.get_show(show_id)
    assert show is not None
    assert show.have_count == 2 and show.missing_count == 0
    assert len(show.folders) == 2


def test_untrack_removes_everything(tracked_show):
    shows_store.remove_show(tracked_show)
    assert shows_store.get_show(tracked_show) is None
    assert shows_store.list_episodes(tracked_show) == []
