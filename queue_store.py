import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import config

_DB_LOCK = threading.Lock()


@dataclass(frozen=True)
class QueueRequest:
    request_id: int
    requester: str
    content: str
    status: str
    created_at: str
    completed_at: str | None


def _db_path() -> Path:
    path = Path(config.APP_DB_PATH)
    if path.is_absolute():
        return path
    return config.APP_DIR / path


def initialize_queue_db() -> None:
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with _DB_LOCK, sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            )
            """
        )
        conn.commit()


def add_request(content: str, requester: str) -> QueueRequest:
    clean_content = " ".join(content.split())
    clean_requester = " ".join(requester.split()) or "Unknown"
    if not clean_content:
        raise ValueError("Request content cannot be empty.")

    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO requests (requester, content, status)
            VALUES (?, ?, 'open')
            """,
            (clean_requester, clean_content),
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
            SELECT id, requester, content, status, created_at, completed_at
            FROM requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()

    if row is None:
        return None
    return QueueRequest(
        request_id=row["id"],
        requester=row["requester"],
        content=row["content"],
        status=row["status"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def list_requests(*, status: str = "open", limit: int = 50) -> list[QueueRequest]:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, requester, content, status, created_at, completed_at
            FROM requests
            WHERE status = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()

    return [
        QueueRequest(
            request_id=row["id"],
            requester=row["requester"],
            content=row["content"],
            status=row["status"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )
        for row in rows
    ]


def complete_request(request_id: int) -> bool:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        cursor = conn.execute(
            """
            UPDATE requests
            SET status = 'done',
                completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'open'
            """,
            (request_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def open_request_count() -> int:
    initialize_queue_db()
    with _DB_LOCK, sqlite3.connect(_db_path()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE status = 'open'"
        ).fetchone()

    return int(row[0]) if row is not None else 0


def format_requests_message(*, status: str = "open", limit: int = 10) -> str:
    requests = list_requests(status=status, limit=limit)
    if not requests:
        return "No open requests right now."

    lines = [f"Open requests ({len(requests)} shown):"]
    for item in requests:
        lines.append(
            f"#{item.request_id} [{item.created_at}] {item.requester}: {item.content}"
        )
    return "\n".join(lines)
