"""Routing planner: parse, show-folder match, season style, conservative fallback."""
import config
import torrent_routing
from torrent_routing import parse_torrent_name, plan_route


def test_parse_sxxeyy():
    p = parse_torrent_name("Severance.S02E05.1080p.WEB-DL.x265-GROUP")
    assert p.show_title.casefold() == "severance"
    assert (p.season, p.episode) == (2, 5)


def test_parse_nxm_and_brackets():
    p = parse_torrent_name("[SubsPlease] Frieren - 12 (1080p) S01E12")
    assert p.season == 1 and p.episode == 12


def test_parse_season_only():
    p = parse_torrent_name("The Bear Season 3 Complete 1080p")
    assert p.show_title.casefold().startswith("the bear")
    assert p.season == 3 and p.episode is None


def test_route_into_existing_show_and_season_style(tmp_path, monkeypatch):
    root = tmp_path / "tv"
    show = root / "Severance"
    (show / "Season 01").mkdir(parents=True)  # zero-padded style already in use
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")],
    )
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))

    plan = plan_route("Severance.S02E05.1080p.WEB-DL", "tv")
    assert plan.confident
    assert plan.dest_dir.endswith("Season 02")  # replicated zero-padding
    assert plan.new_filename == "Severance - S02E05"


def test_route_unpadded_season_style(tmp_path, monkeypatch):
    root = tmp_path / "tv"
    (root / "The Bear" / "Season 2").mkdir(parents=True)  # unpadded style
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")],
    )
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))

    plan = plan_route("The.Bear.S03E01.1080p", "tv")
    assert plan.confident
    assert plan.dest_dir.endswith("Season 3")  # style copied, no zero-pad


def test_route_stays_in_staging_when_unmatched(tmp_path, monkeypatch):
    root = tmp_path / "tv"
    (root / "Completely Different Show").mkdir(parents=True)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")],
    )
    staging = tmp_path / "staging"
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(staging))

    plan = plan_route("Some.Unknown.Show.S01E01.720p", "tv")
    assert not plan.confident
    assert plan.dest_dir == str(staging)
    assert plan.new_filename is None  # never rename on a shaky route


def test_route_seasons_split_across_drives(tmp_path, monkeypatch):
    # Same show on two roots — the best-matching folder wins regardless of root.
    root_a, root_b = tmp_path / "d_drive", tmp_path / "i_drive"
    (root_a / "Frieren" / "Season 1").mkdir(parents=True)
    (root_b / "Frieren Beyond Journeys End").mkdir(parents=True)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root_a), media_type="anime"),
         config.MediaLibraryPath(path=str(root_b), media_type="anime")],
    )
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))

    plan = plan_route("Frieren S01E05 1080p", "anime")
    assert plan.confident
    assert str(root_a) in plan.dest_dir  # exact folder-name match beats fuzzy
