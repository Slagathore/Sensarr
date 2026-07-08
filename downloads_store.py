# =============================================================================
# downloads_store.py
# =============================================================================
# SQLite persistence for the torrent download pipeline:
#   downloads         — one row per grab (status, progress, planned route)
#   download_history  — audit trail: every download / rename / move with the
#                       before and after values, so nothing ever "just moved"
#                       without a record (Cole's requirement).
# =============================================================================

import threading
from dataclasses import dataclass

import db

_DL_LOCK = threading.Lock()

_SCHEMA_DOWNLOADS = """
CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    INTEGER,
    title         TEXT NOT NULL,
    magnet        TEXT NOT NULL,
    source        TEXT,
    media_type    TEXT NOT NULL DEFAULT 'unknown',
    status        TEXT NOT NULL DEFAULT 'queued',
    progress      REAL NOT NULL DEFAULT 0,
    staging_dir   TEXT,
    planned_dest  TEXT,
    planned_name  TEXT,
    route_reason  TEXT,
    auto_rename   INTEGER NOT NULL DEFAULT 0,
    auto_move     INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at  TEXT,
    show_id       INTEGER,
    season        INTEGER,
    episode       INTEGER
)
"""
# status: queued | downloading | downloaded | moved | error | cancelled
# show_id/season/episode are set when the grab targets a tracked show's
# episode — routing then uses show_tracker.plan_for_episode (deterministic)
# instead of fuzzy name matching.

# Columns added after the table first shipped — applied via ALTER on upgrade.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("downloads", "show_id", "INTEGER"),
    ("downloads", "season", "INTEGER"),
    ("downloads", "episode", "INTEGER"),
    # Quality-replacement grabs: the old (cam/low-quality) file to delete
    # once this download has moved into the library.
    ("downloads", "replace_path", "TEXT"),
]

_SCHEMA_HISTORY = """
CREATE TABLE IF NOT EXISTS download_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id  INTEGER NOT NULL,
    action       TEXT NOT NULL,
    before_value TEXT,
    after_value  TEXT,
    at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
# action: grabbed | downloaded | renamed | moved | error | cancelled


@dataclass(frozen=True)
class DownloadRow:
    download_id: int
    request_id: int | None
    title: str
    magnet: str
    source: str | None
    media_type: str
    status: str
    progress: float
    staging_dir: str | None
    planned_dest: str | None
    planned_name: str | None
    route_reason: str | None
    auto_rename: bool
    auto_move: bool
    error: str | None
    created_at: str
    completed_at: str | None
    show_id: int | None = None
    season: int | None = None
    episode: int | None = None
    replace_path: str | None = None


@dataclass(frozen=True)
class HistoryRow:
    history_id: int
    download_id: int
    action: str
    before_value: str | None
    after_value: str | None
    at: str


_DOWNLOAD_COLUMNS = (
    "id, request_id, title, magnet, source, media_type, status, progress, "
    "staging_dir, planned_dest, planned_name, route_reason, auto_rename, "
    "auto_move, error, created_at, completed_at, show_id, season, episode, "
    "replace_path"
)


def initialize_downloads_db() -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(_SCHEMA_DOWNLOADS)
        conn.execute(_SCHEMA_HISTORY)
        for table, column, col_def in _MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if existing and column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        conn.commit()


def _row_to_download(row) -> DownloadRow:
    return DownloadRow(
        download_id=row[0], request_id=row[1], title=row[2], magnet=row[3],
        source=row[4], media_type=row[5], status=row[6], progress=row[7],
        staging_dir=row[8], planned_dest=row[9], planned_name=row[10],
        route_reason=row[11], auto_rename=bool(row[12]), auto_move=bool(row[13]),
        error=row[14], created_at=row[15], completed_at=row[16],
        show_id=row[17], season=row[18], episode=row[19],
        replace_path=row[20],
    )


def create_download(
    *, title: str, magnet: str, source: str | None, media_type: str,
    request_id: int | None, staging_dir: str, planned_dest: str | None,
    planned_name: str | None, route_reason: str | None,
    auto_rename: bool, auto_move: bool,
    show_id: int | None = None, season: int | None = None,
    episode: int | None = None, replace_path: str | None = None,
) -> int:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO downloads
                (request_id, title, magnet, source, media_type, staging_dir,
                 planned_dest, planned_name, route_reason, auto_rename, auto_move,
                 show_id, season, episode, replace_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, title, magnet, source, media_type, staging_dir,
             planned_dest, planned_name, route_reason,
             int(auto_rename), int(auto_move), show_id, season, episode,
             replace_path),
        )
        conn.commit()
        download_id = int(cursor.lastrowid or 0)
    add_history(download_id, "grabbed", before=None, after=title)
    return download_id


def set_status(download_id: int, status: str, *, error: str | None = None,
               completed: bool = False) -> None:
    with _DL_LOCK, db.connect() as conn:
        if completed:
            conn.execute(
                "UPDATE downloads SET status = ?, error = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, error, download_id),
            )
        else:
            conn.execute(
                "UPDATE downloads SET status = ?, error = ? WHERE id = ?",
                (status, error, download_id),
            )
        conn.commit()


def set_progress(download_id: int, progress: float) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET progress = ?, status = 'downloading' WHERE id = ?",
            (progress, download_id),
        )
        conn.commit()


def set_route(download_id: int, *, planned_dest: str | None,
              planned_name: str | None, route_reason: str | None) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET planned_dest = ?, planned_name = ?, route_reason = ? WHERE id = ?",
            (planned_dest, planned_name, route_reason, download_id),
        )
        conn.commit()


def get_download(download_id: int) -> DownloadRow | None:
    with _DL_LOCK, db.connect() as conn:
        row = conn.execute(
            f"SELECT {_DOWNLOAD_COLUMNS} FROM downloads WHERE id = ?",
            (download_id,),
        ).fetchone()
    return _row_to_download(row) if row else None


def list_downloads(*, limit: int = 100) -> list[DownloadRow]:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_DOWNLOAD_COLUMNS} FROM downloads ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_download(r) for r in rows]


def request_ids_with_downloads() -> set[int]:
    """Request IDs that already have a grab — used by auto-grab to skip them."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT request_id FROM downloads WHERE request_id IS NOT NULL"
        ).fetchall()
    return {int(r[0]) for r in rows}


def add_history(download_id: int, action: str, *, before: str | None,
                after: str | None) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "INSERT INTO download_history (download_id, action, before_value, after_value) VALUES (?, ?, ?, ?)",
            (download_id, action, before, after),
        )
        conn.commit()


def list_history(*, limit: int = 200) -> list[HistoryRow]:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, download_id, action, before_value, after_value, at
            FROM download_history ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [HistoryRow(*r) for r in rows]
