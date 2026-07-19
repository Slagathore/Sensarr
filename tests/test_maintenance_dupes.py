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


def test_dupes_nested_year_folders_not_grouped(tmp_path, monkeypatch):
    """The confirmed live-audit false positive: a rebooted show's year lives
    on the SHOW folder, one level above a Season NN subfolder, so the old
    'immediate parent only' year lookup missed it entirely and collapsed
    Goosebumps 1995 into Goosebumps 2023."""
    _fixture_library(tmp_path, monkeypatch, [
        "Goosebumps (1995)/Season 01/Goosebumps - S01E01 - Pilot.mkv",
        "Goosebumps (2023)/Season 01/Goosebumps - S01E01 - Pilot.mkv",
    ])
    assert find_duplicates() == []


def test_dupes_ignore_year_from_unrelated_categorization_folder(tmp_path, monkeypatch):
    """Verification finding 1: an intermediate categorization folder
    ("Recently Added 2023") sitting ABOVE the real title folder must never
    donate its year to the file inside — that's not the movie's year, it's
    an unrelated bucket, and letting it through hid a real duplicate (the
    same movie under two differently-dated 'Recently Added' folders came
    back as 0 groups instead of 1). The ancestor's own normalized name core
    has to match the file's title core before its year counts; "Recently
    Added" never will."""
    _fixture_library(tmp_path, monkeypatch, [
        "Recently Added 2023/My Cool Movie/My Cool Movie.mkv",
        "Recently Added 2024/My Cool Movie/My Cool Movie.mkv",
    ])
    groups = find_duplicates()
    assert len(groups) == 1
    assert len(groups[0].candidates) == 2


def test_dupes_season_folder_and_ep_word_not_grouped(tmp_path, monkeypatch):
    """The other confirmed false positive: filenames with no SxxExx at all
    ('Sekirei Ep 05.mkv') used to key identically across different season
    folders. Season now comes from the nearest 'Season N' ancestor and
    episode from an 'Ep NN' filename token, so S1E05 and S2E05 stay apart."""
    _fixture_library(tmp_path, monkeypatch, [
        "Sekirei/Season 1/Sekirei Ep 05.mkv",
        "Sekirei/Season 2/Sekirei Ep 05.mkv",
    ])
    assert find_duplicates() == []


def test_dupes_ep_word_still_groups_real_copies_in_same_season(tmp_path, monkeypatch):
    """Sanity check the widened key doesn't over-correct: two copies of the
    SAME season+episode (no SxxExx, only an 'Ep NN' token) still group."""
    _fixture_library(tmp_path, monkeypatch, [
        "Sekirei/Season 1/Sekirei Ep 05 720p.mkv",
        "Sekirei/Season 1/Sekirei Ep 05 1080p.mkv",
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
