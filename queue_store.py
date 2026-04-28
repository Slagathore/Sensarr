# =============================================================================
# queue_store.py
# =============================================================================
# SQLite-backed request queue.  Schema is self-migrating: first run creates the
# table; subsequent runs add any new columns that don't yet exist so existing
# databases are upgraded transparently.
#
# Schema v1 (original): id, requester, content, status, created_at, completed_at
# Schema v2 (request flow): + media_type, resolved_title, external_id,
#           external_url, found_in_library, library_checked_at
# =============================================================================

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import config

_DB_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueRequest:
    request_id: int
    requester: str
    content: str              # raw user text
    status: str               # "open" | "done"
    created_at: str
    completed_at: str | None
    media_type: str           # "movie" | "tv" | "anime" | "xanime" | "other" | "unknown"
    resolved_title: str | None  # official title resolved from TMDB/TVDB/MAL
    external_id: str | None   # ID on external DB (TMDB id, MAL id, etc.)
    external_url: str | None  # link to the entry on the external DB
    found_in_library: bool    # True once the daily check confirms it's been added
    library_checked_at: str | None  # timestamp of last library check


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    path = Path(config.APP_DB_PATH)
    if path.is_absolute():
        return path
    return config.APP_DIR / path


# ---------------------------------------------------------------------------
# Schema initialisation + migration
# ---------------------------------------------------------------------------

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    requester     TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'open',
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at  TEXT
)
"""

# New columns added in schema v2; each tuple is (column_name, column_def)
_V2_COLUMNS: list[tuple[str, str]] = [
    ("media_type",         "TEXT    NOT NULL DEFAULT 'unknown'"),
    ("resolved_title",     "TEXT"),
    ("external_id",        "TEXT"),
    ("external_url",       "TEXT"),
    ("found_in_library",   "INTEGER NOT NULL DEFAULT 0"),
    ("library_checked_at", "TEXT"),
]


def initialize_queue_db() -> None:
    """Create the requests table and apply any pending column migrations."""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with _DB_LOCK, sqlite3.connect(db_path) as conn:
        conn.execute(_SCHEMA_V1)
        conn.commit()

        # Discover which columns already exist
        existing_cols: set[str] = {
            row[1]
            for row in conn.execute("PRAGMA table_info(requests)").fetchall()
        }

        for col_name, col_def in _V2_COLUMNS:
            if col_name not in existing_cols:
                conn.execute(
                    f"ALTER TABLE requests ADD COLUMN {col_name} {col_def}"
                )

        conn.commit()


# ---------------------------------------------------------------------------
# Row → dataclass helper
# ---------------------------------------------------------------------------

def _row_to_request(row: sqlite3.Row) -> QueueRequest:
    return QueueRequest(
        request_id=row["id"],
        requester=row["requester"],
        content=row["content"],
        status=row["status"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        media_type=row["media_type"] or "unknown",
        resolved_title=row["resolved_title"],
        external_id=row["external_id"],
        external_url=row["external_url"],
        found_in_library=bool(row["found_in_library"]),
        library_checked_at=row["library_checked_at"],
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add_request(
    content: str,
    requester: str,
    *,
    media_type: str = "unknown",
    resolved_title: str | None = None,
    external_id: str | None = None,
    external_url: str | None = None,
) -> QueueRequest:
    """
    Add a new open request to the queue.

    Args:
        content: Raw user-supplied text describing what they want.
        requester: Telegram username or display name of the requester.
        media_type: Category — "movie", "tv", "anime", "xanime", "other", "unknown".
        resolved_title: Official/canonical title from TMDB/TVDB/MAL (if known).
        external_id: ID string on the external DB.
        external_url: URL to the entry on the external DB.
    """
    clean_content = " ".join(content.split())
    clean_requester = " ".join(requester.split()) or "Unknown"
    if not clean_content:
        raise ValueError("Request content cannot be empty.")

    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO requests
                (requester, content, status, media_type, resolved_title, external_id, external_url)
            VALUES (?, ?, 'open', ?, ?, ?, ?)
            """,
            (clean_requester, clean_content, media_type, resolved_title, external_id, external_url),
        )
        conn.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a row id for the new request.")
        request_id = int(cursor.lastrowid)

    created = get_request(request_id)
    if created is None:
        raise RuntimeError("Failed to read back the newly-created request.")
    return created


def get_request(request_id: int) -> QueueRequest | None:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, requester, content, status, created_at, completed_at,
                   media_type, resolved_title, external_id, external_url,
                   found_in_library, library_checked_at
            FROM requests WHERE id = ?
            """,
            (request_id,),
        ).fetchone()

    return _row_to_request(row) if row is not None else None


def list_requests(*, status: str = "open", limit: int = 50) -> list[QueueRequest]:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, requester, content, status, created_at, completed_at,
                   media_type, resolved_title, external_id, external_url,
                   found_in_library, library_checked_at
            FROM requests
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()

    return [_row_to_request(row) for row in rows]


def complete_request(request_id: int) -> bool:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        cursor = conn.execute(
            """
            UPDATE requests
            SET status = 'done', completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'open'
            """,
            (request_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_library_status(request_id: int, *, found: bool) -> None:
    """Mark a request as found (or not found) in the Plex library after a check."""
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            UPDATE requests
            SET found_in_library = ?, library_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (1 if found else 0, request_id),
        )
        conn.commit()


def get_requests_needing_library_check(*, max_age_hours: int = 20) -> list[QueueRequest]:
    """
    Return open requests that haven't been library-checked in the last
    max_age_hours, or have never been checked.
    """
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, requester, content, status, created_at, completed_at,
                   media_type, resolved_title, external_id, external_url,
                   found_in_library, library_checked_at
            FROM requests
            WHERE status = 'open'
              AND (
                library_checked_at IS NULL
                OR datetime(library_checked_at) < datetime('now', ? || ' hours')
              )
            ORDER BY id ASC
            """,
            (f"-{max_age_hours}",),
        ).fetchall()

    return [_row_to_request(row) for row in rows]


def find_duplicate_requests() -> list[list[QueueRequest]]:
    """
    Find open requests that look like duplicates of each other.

    Two requests are considered duplicates when their resolved_title (or content,
    normalised to lowercase stripped of punctuation) is identical and they have
    the same media_type.

    Returns a list of groups; each group contains 2+ QueueRequest objects.
    """
    requests = list_requests(status="open", limit=500)

    def _normalize(text: str) -> str:
        import re
        text = text.casefold()
        text = re.sub(r"[^\w\s]", "", text)
        return " ".join(text.split())

    seen: dict[str, list[QueueRequest]] = {}
    for req in requests:
        key_text = req.resolved_title or req.content
        key = f"{req.media_type}::{_normalize(key_text)}"
        seen.setdefault(key, []).append(req)

    return [group for group in seen.values() if len(group) >= 2]


def open_request_count() -> int:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status = 'open'"
        ).fetchone()
    return int(row[0]) if row is not None else 0


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_MEDIA_TYPE_EMOJI: dict[str, str] = {
    "movie":   "🎬",
    "tv":      "📺",
    "anime":   "🍜",
    "xanime":  "🔞",
    "other":   "❓",
    "unknown": "📝",
}


def format_requests_message_user(*, status: str = "open", limit: int = 20) -> str:
    """
    User-facing request list: shows title + checkmark if in library.
    Requester names are intentionally omitted for privacy.
    """
    requests = list_requests(status=status, limit=limit)
    if not requests:
        return "No open requests right now."

    lines = [f"📋 Open requests ({len(requests)} shown):"]
    for item in requests:
        emoji = _MEDIA_TYPE_EMOJI.get(item.media_type, "📝")
        display = item.resolved_title or item.content
        checkmark = " ✅" if item.found_in_library else ""
        lines.append(f"{emoji} {display}{checkmark}")

    return "\n".join(lines)


def format_requests_message(*, status: str = "open", limit: int = 10) -> str:
    """Legacy alias — calls the user-facing formatter. Used by /requests command."""
    return format_requests_message_user(status=status, limit=limit)


def format_requests_message_dev(*, status: str = "open", limit: int = 50) -> str:
    """
    Dev-panel format: shows full info including requester, media type, and external link.
    Used internally; never shown to regular Telegram users.
    """
    requests = list_requests(status=status, limit=limit)
    if not requests:
        return "No open requests right now."

    lines = [f"Open requests ({len(requests)} shown):"]
    for item in requests:
        emoji = _MEDIA_TYPE_EMOJI.get(item.media_type, "📝")
        display = item.resolved_title or item.content
        checkmark = " ✅" if item.found_in_library else ""
        link = f" → {item.external_url}" if item.external_url else ""
        lines.append(
            f"#{item.request_id} {emoji} [{item.created_at}] {item.requester}: "
            f"{display}{checkmark}{link}"
        )
    return "\n".join(lines)
