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
    assert _parse_episode_from_file("[SubsPlease] Frieren - 12 (1080p) [ABCD].mkv") == (1, 12)
    # Resolution and year tokens must not be mistaken for episode numbers.
    assert _parse_episode_from_file("[Group] Show - 03 [720p][2023].mkv") == (1, 3)


def test_parse_episode_no_number():
    assert _parse_episode_from_file("Some Movie.mkv") is None


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
    assert "5 episodes known" in summary and "2 on disk" in summary

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
