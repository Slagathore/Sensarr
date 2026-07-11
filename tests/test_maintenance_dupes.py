"""Duplicate-detection guards and the Combo Clean rename builder."""
from pathlib import Path

import config
from maintenance import build_combo_renames, find_duplicates


def _mk(root: Path, rel: str, size: int = 1024) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"0" * size)


def _fixture_library(tmp_path, monkeypatch, files: list[str]):
    root = tmp_path / "tv"
    for rel in files:
        _mk(root, rel)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")])
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    return root


def test_dupes_skip_known_false_positives(tmp_path, monkeypatch):
    _fixture_library(tmp_path, monkeypatch, [
        # pt1/pt2 of one special — NOT duplicates
        "SNL/Specials/SNL - S00E95 - pt1 - Debate.avi",
        "SNL/Specials/SNL - S00E95 - pt2 - Interview.avi",
        # a .5 recap next to the real episode — NOT duplicates
        "Vivy/Vivy - S01E13.mkv",
        "Vivy/Vivy - S01E13.5.mkv",
        # extras folders never compared against anything
        "Scrubs/Season 1/Featurettes/Deleted Scenes.mkv",
        "Venture/Extras/Deleted Scenes.mkv",
        # SxxXyy specials are distinct episodes
        "Robot Chicken/S03/Robot Chicken S03X01 Christmas.mp4",
        "Robot Chicken/S04/Robot Chicken S04X01 Christmas.mp4",
        # promo stubs are junk, not media
        "MovieA (2006)/ETRG.mp4",
        "MovieB (2008)/ETRG.mp4",
        # year variants of a rebooted show — NOT duplicates
        "Goosebumps (1995)/Goosebumps - S01E01 - Pilot.mkv",
        "Goosebumps (2023)/Goosebumps - S01E01 - Pilot.mkv",
    ])
    assert find_duplicates() == []


def test_dupes_still_catch_real_copies(tmp_path, monkeypatch):
    _fixture_library(tmp_path, monkeypatch, [
        "ShowX/Season 01/ShowX - S01E02 720p.mkv",
        "ShowX/Season 01/ShowX.S01E02.1080p.mkv",
    ])
    groups = find_duplicates()
    assert len(groups) == 1
    assert len(groups[0].candidates) == 2


def test_combo_clean_rules(tmp_path, monkeypatch):
    root = _fixture_library(tmp_path, monkeypatch, [
        "Show/Show.Name.S01E01.1080p.WEBRip.x265.10bit-GalaxyTV.mkv",
        "Show/[SubsPlease] Other Show - 05 {extra tag}.mkv",
        "Show/Already Clean Name S01E02.mkv",
    ])
    pairs = build_combo_renames("tv")
    renames = {Path(p.original).name: Path(p.sanitized).name for p in pairs}
    # dots → spaces, junk words and the group tag gone, dangles trimmed
    assert renames["Show.Name.S01E01.1080p.WEBRip.x265.10bit-GalaxyTV.mkv"] \
        == "Show Name S01E01.mkv"
    # bracketed and braced chunks removed
    assert renames["[SubsPlease] Other Show - 05 {extra tag}.mkv"] \
        == "Other Show - 05.mkv"
    # untouched files produce no pair
    assert "Already Clean Name S01E02.mkv" not in renames


def test_prefer_unfailed_rotates_copies(tmp_path, monkeypatch):
    """Failed releases are skipped for a week; when everything failed,
    the least-recently-failed copy is retried (rotation, not starvation)."""
    import time

    import db as appdb
    monkeypatch.setattr(config, "APP_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(appdb, "DB_PATH", str(tmp_path / "t.db"), raising=False)

    import downloads_store as ds
    from download_manager import _prefer_unfailed
    from torrent_search import TorrentResult

    def result(h):
        return TorrentResult(title=h, magnet=f"magnet:?xt=urn:btih:{h}",
                             size_bytes=1, seeders=1, source="tpb",
                             media_type="tv")

    a, b, c = (result("a" * 40), result("b" * 40), result("c" * 40))
    key = "ep:1:1:1"

    # nothing recorded — untouched
    assert _prefer_unfailed([a, b, c], key) == [a, b, c]

    # a failed recently — dropped while b/c remain
    ds.record_failed_grab(key, "a" * 40)
    assert _prefer_unfailed([a, b, c], key) == [b, c]

    # all failed — least-recently-failed comes back alone
    ds.record_failed_grab(key, "b" * 40)
    time.sleep(0.01)
    ds.record_failed_grab(key, "c" * 40)
    survivors = _prefer_unfailed([a, b, c], key)
    assert [r.title for r in survivors] == ["a" * 40]
