# Guards added after a real incident: an index rebuild pointed at a root that
# wasn't really there (stray ad-hoc run against the live DB) wiped a 34k-row
# index down to one file. The same failure hits any user whose media drive is
# unmounted when a reindex or delta refresh runs. These tests pin the two
# protections: unavailable roots keep their rows, and a rescan that would
# collapse the index is refused unless forced.

import sqlite3

import pytest

import config
import library_index


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    db_file = tmp_path / "guard_test.db"
    monkeypatch.setattr(library_index, "_db_path", lambda: db_file)
    monkeypatch.setattr(config, "LIBRARY_INDEX_EXTENSIONS", [".mkv"])
    library_index.initialize_library_index_db()
    return db_file


def _seed_rows(db_file, root: str, count: int) -> None:
    with sqlite3.connect(db_file) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO library_files
            (path, name, root_path, search_name, size_bytes, modified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (rf"{root}\show\ep{i:04d}.mkv", f"ep{i:04d}.mkv", root,
                 f"ep{i:04d}.mkv", 1000 + i, 1000.0)
                for i in range(count)
            ],
        )
        conn.commit()


def _row_count(db_file) -> int:
    with sqlite3.connect(db_file) as conn:
        return conn.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]


def test_rebuild_refuses_index_collapse(index_db, tmp_path, monkeypatch):
    empty_root = tmp_path / "empty_media"
    empty_root.mkdir()
    _seed_rows(index_db, str(empty_root), 150)
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(empty_root)])

    result = library_index.rebuild_library_index()

    assert result.aborted_reason, "collapse must be refused, not applied"
    assert _row_count(index_db) == 150, "existing index must be untouched"
    assert "NOT applied" in library_index.format_reindex_result_message(result)


def test_rebuild_force_overrides_collapse_guard(index_db, tmp_path, monkeypatch):
    empty_root = tmp_path / "empty_media"
    empty_root.mkdir()
    _seed_rows(index_db, str(empty_root), 150)
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(empty_root)])

    result = library_index.rebuild_library_index(force=True)

    assert not result.aborted_reason
    assert _row_count(index_db) == 0


def test_rebuild_preserves_rows_under_missing_root(index_db, tmp_path, monkeypatch):
    live_root = tmp_path / "live_media"
    (live_root / "movie").mkdir(parents=True)
    (live_root / "movie" / "new.mkv").write_bytes(b"x" * 10)
    missing_root = str(tmp_path / "unplugged_drive")  # never created

    _seed_rows(index_db, missing_root, 150)
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS",
                        [str(live_root), missing_root])

    result = library_index.rebuild_library_index()

    assert not result.aborted_reason
    assert result.preserved_files == 150
    assert missing_root in result.missing_roots
    assert _row_count(index_db) == 151  # 150 kept + 1 newly walked
    with sqlite3.connect(index_db) as conn:
        removed_events = conn.execute(
            "SELECT COUNT(*) FROM file_events WHERE event = 'removed'"
        ).fetchone()[0]
    assert removed_events == 0, "unavailable-root files must not log as removed"


def test_refresh_refuses_index_collapse(index_db, tmp_path, monkeypatch):
    empty_root = tmp_path / "empty_media"
    empty_root.mkdir()
    _seed_rows(index_db, str(empty_root), 150)
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(empty_root)])

    result = library_index.refresh_library_index()

    assert result.aborted_reason
    assert _row_count(index_db) == 150


def test_refresh_keeps_files_under_missing_root(index_db, tmp_path, monkeypatch):
    live_root = tmp_path / "live_media"
    (live_root / "movie").mkdir(parents=True)
    (live_root / "movie" / "new.mkv").write_bytes(b"x" * 10)
    missing_root = str(tmp_path / "unplugged_drive")  # never created

    _seed_rows(index_db, missing_root, 150)
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS",
                        [str(live_root), missing_root])

    result = library_index.refresh_library_index()

    assert not result.aborted_reason
    assert result.removed == 0
    assert result.added == 1
    assert _row_count(index_db) == 151
    with sqlite3.connect(index_db) as conn:
        removed_events = conn.execute(
            "SELECT COUNT(*) FROM file_events WHERE event = 'removed'"
        ).fetchone()[0]
    assert removed_events == 0
