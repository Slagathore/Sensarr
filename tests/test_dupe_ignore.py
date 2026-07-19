# =============================================================================
# tests/test_dupe_ignore.py
# =============================================================================
# Task G item 2 — "Not a duplicate" verdicts persist in their own store
# (dupe_ignore.json, never maintenance_cache.json) and find_duplicates()
# honours them on every fresh walk, not just the one that made the call.
# =============================================================================
from pathlib import Path

import config
import dupe_ignore
from maintenance import find_duplicates


def _mk(root: Path, rel: str, size: int = 1024) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"0" * size)


def _fixture_library(tmp_path, monkeypatch, files: list[str]) -> Path:
    root = tmp_path / "tv"
    for rel in files:
        _mk(root, rel)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="tv")])
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    return root


def _isolate_store(tmp_path, monkeypatch) -> None:
    """Every test gets its own dupe_ignore.json — the real store lives in a
    session-wide temp dir per tests/conftest.py, which would leak verdicts
    across unrelated tests if left un-isolated."""
    store_dir = tmp_path / "store"
    monkeypatch.setattr(dupe_ignore, "_data_dir", lambda: store_dir)


# ---------------------------------------------------------------------------
# Keying
# ---------------------------------------------------------------------------

def test_folder_pair_key_is_order_independent(tmp_path):
    a = str(tmp_path / "A")
    b = str(tmp_path / "B")
    assert dupe_ignore.folder_pair_key(a, b) == dupe_ignore.folder_pair_key(b, a)


def test_file_pair_key_is_order_independent(tmp_path):
    a = str(tmp_path / "a.mkv")
    b = str(tmp_path / "b.mkv")
    assert dupe_ignore.file_pair_key([a, b]) == dupe_ignore.file_pair_key([b, a])


# ---------------------------------------------------------------------------
# Store round-trip
# ---------------------------------------------------------------------------

def test_ignore_and_unignore_folder_pair_round_trips(tmp_path, monkeypatch):
    _isolate_store(tmp_path, monkeypatch)
    a, b = str(tmp_path / "Show/Season 1"), str(tmp_path / "Show/Season 2")
    dupe_ignore.ignore_folder_pair(a, b)
    assert dupe_ignore.folder_pair_key(a, b) in dupe_ignore.ignored_folder_pair_set()
    assert len(dupe_ignore.list_ignored_folder_pairs()) == 1

    # Re-ignoring the same pair (either order) must not duplicate the entry.
    dupe_ignore.ignore_folder_pair(b, a)
    assert len(dupe_ignore.list_ignored_folder_pairs()) == 1

    assert dupe_ignore.unignore_folder_pair(a, b) is True
    assert dupe_ignore.list_ignored_folder_pairs() == []
    assert dupe_ignore.unignore_folder_pair(a, b) is False  # already gone


def test_ignore_and_unignore_file_pair_round_trips(tmp_path, monkeypatch):
    _isolate_store(tmp_path, monkeypatch)
    p1, p2 = str(tmp_path / "a.mkv"), str(tmp_path / "b.mkv")
    dupe_ignore.ignore_file_pair([p1, p2])
    assert dupe_ignore.file_pair_key([p1, p2]) in dupe_ignore.ignored_file_pair_set()
    assert dupe_ignore.unignore_file_pair([p2, p1]) is True
    assert dupe_ignore.ignored_file_pair_set() == set()


def test_corrupt_store_degrades_to_empty_not_a_crash(tmp_path, monkeypatch):
    _isolate_store(tmp_path, monkeypatch)
    path = dupe_ignore._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all {{{", encoding="utf-8")
    assert dupe_ignore.list_ignored_folder_pairs() == []
    assert dupe_ignore.list_ignored_file_pairs() == []
    # And the store is still writable afterwards (a bad file self-heals).
    dupe_ignore.ignore_file_pair(["a", "b"])
    assert len(dupe_ignore.list_ignored_file_pairs()) == 1


# ---------------------------------------------------------------------------
# Integration with find_duplicates()
# ---------------------------------------------------------------------------

def test_ignored_file_pair_is_filtered_from_find_duplicates(tmp_path, monkeypatch):
    _isolate_store(tmp_path, monkeypatch)
    root = _fixture_library(tmp_path, monkeypatch, [
        "ShowX/Season 01/ShowX - S01E02 720p.mkv",
        "ShowX/Season 01/ShowX.S01E02.1080p.mkv",
    ])
    groups = find_duplicates()
    assert len(groups) == 1

    dupe_ignore.ignore_file_pair(groups[0].candidates)

    # Survives a completely fresh find_duplicates() call (Re-run semantics).
    assert find_duplicates() == []


def test_ignored_folder_pair_is_filtered_from_find_duplicates(tmp_path, monkeypatch):
    _isolate_store(tmp_path, monkeypatch)
    root = _fixture_library(tmp_path, monkeypatch, [
        "ShowX/Season 01 (old)/ShowX - S01E01.mkv",
        "ShowX/Season 01 (new)/ShowX - S01E01.mkv",
        "ShowX/Season 01 (old)/ShowX - S01E02.mkv",
        "ShowX/Season 01 (new)/ShowX - S01E02.mkv",
    ])
    groups = find_duplicates()
    assert len(groups) == 2  # one group per episode, two matching folders

    folder_a = str(root / "ShowX" / "Season 01 (old)")
    folder_b = str(root / "ShowX" / "Season 01 (new)")
    dupe_ignore.ignore_folder_pair(folder_a, folder_b)

    # The whole folder pair is gone — including on a brand new walk.
    assert find_duplicates() == []


def test_ignore_verdict_survives_a_fresh_process_like_call(tmp_path, monkeypatch):
    """Nothing in find_duplicates() or dupe_ignore keeps in-memory state
    between calls other than the file on disk — this proves the verdict
    isn't riding along on some cached Python object."""
    _isolate_store(tmp_path, monkeypatch)
    _fixture_library(tmp_path, monkeypatch, [
        "ShowY/Season 01/ShowY - S01E05 720p.mkv",
        "ShowY/Season 01/ShowY.S01E05.1080p.mkv",
    ])
    groups = find_duplicates()
    dupe_ignore.ignore_file_pair(groups[0].candidates)

    # Reload the store fresh from disk (simulates a restart) and re-check.
    reloaded = dupe_ignore._load()
    assert len(reloaded["file_pairs"]) == 1
    assert find_duplicates() == []
