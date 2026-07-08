# =============================================================================
# auth_store.py
# =============================================================================
# Telegram user allowlist backed by SQLite.
#
# Before this module existed the bot answered ANY Telegram account that found
# its handle — including /hardreset. Now every incoming update passes through
# an authorization gate (see telegram_service._authorization_gate) that checks
# this table.
#
# Seeding / grandfathering strategy:
#   - On first run the table is seeded with the distinct requester names found
#     in the existing requests table (everyone who has ever interacted with
#     the bot). Those rows have no Telegram user ID yet.
#   - The first time a seeded user talks to the bot again, their numeric
#     Telegram ID is "claimed" onto the matching name row. From then on the
#     check is ID-based — names are display-only.
#   - IDs listed in TELEGRAM_ALLOWED_USER_IDS in .env are always allowed and
#     are upserted into the table on startup.
#
# Note the deliberate trade-off: until a seeded name row is claimed, anyone
# who sets their Telegram display name to exactly match it could claim that
# seat. For a household bot this is acceptable; the alternative (locking
# everyone out until the admin collects numeric IDs by hand) is worse. Once
# claimed, a seat is pinned to the numeric ID and cannot be re-claimed.
# =============================================================================

import logging
import sqlite3
import threading
from dataclasses import dataclass

import config
import db

logger = logging.getLogger(__name__)

_AUTH_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allowed_users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER UNIQUE,
    display_name     TEXT,
    username         TEXT,
    source           TEXT NOT NULL DEFAULT 'manual',
    added_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at       TEXT
)
"""

# Access requests: unknown users who messaged the bot and asked to be let in.
# status: 'pending' | 'approved' | 'denied'. chat_id is kept so the bot can
# message the user when the admin approves/denies from the desktop app.
_REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER UNIQUE NOT NULL,
    display_name     TEXT,
    username         TEXT,
    chat_id          INTEGER,
    status           TEXT NOT NULL DEFAULT 'pending',
    requested_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at      TEXT
)
"""


@dataclass(frozen=True)
class AllowedUser:
    row_id: int
    telegram_user_id: int | None
    display_name: str | None
    username: str | None
    source: str
    added_at: str
    claimed_at: str | None
    plex_username: str | None = None


def set_plex_username(row_id: int, plex_username: str | None) -> None:
    """Map (or clear) an allowed user's Plex account name."""
    with _AUTH_LOCK, db.connect() as conn:
        conn.execute("UPDATE allowed_users SET plex_username = ? WHERE id = ?",
                     (plex_username or None, row_id))
        conn.commit()


def _normalize_name(name: str) -> str:
    """Casefold and strip a leading @ so '@Foo' and 'foo' compare equal."""
    return name.strip().lstrip("@").casefold()


def initialize_auth_db() -> None:
    """Create the allowlist table, seed it from past requesters on first run,
    and upsert any .env-configured IDs."""
    with _AUTH_LOCK, db.connect() as conn:
        conn.execute(_SCHEMA)
        conn.execute(_REQUESTS_SCHEMA)

        # Migration: map a Telegram user to their Plex account name (for the
        # Watchlist/Recs tab and per-user features).
        existing = {row[1] for row in conn.execute("PRAGMA table_info(allowed_users)").fetchall()}
        if existing and "plex_username" not in existing:
            conn.execute("ALTER TABLE allowed_users ADD COLUMN plex_username TEXT")

        # First run: grandfather in everyone who has ever filed a request.
        row = conn.execute("SELECT COUNT(*) FROM allowed_users").fetchone()
        if row is not None and row[0] == 0:
            try:
                requesters = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT requester FROM requests"
                    ).fetchall()
                    if r[0] and r[0].strip()
                ]
            except sqlite3.OperationalError:
                requesters = []  # requests table not created yet
            for name in requesters:
                conn.execute(
                    "INSERT INTO allowed_users (display_name, source) VALUES (?, 'seeded-from-requests')",
                    (name,),
                )
            if requesters:
                logger.info(
                    "Seeded Telegram allowlist with %d past requester(s): %s",
                    len(requesters), ", ".join(requesters),
                )

        # Always-allowed IDs from .env — upsert so edits take effect on restart.
        for user_id in config.TELEGRAM_ALLOWED_USER_IDS:
            conn.execute(
                """
                INSERT INTO allowed_users (telegram_user_id, source, claimed_at)
                VALUES (?, 'env', CURRENT_TIMESTAMP)
                ON CONFLICT(telegram_user_id) DO NOTHING
                """,
                (user_id,),
            )
        conn.commit()


def is_user_allowed(user_id: int) -> bool:
    with _AUTH_LOCK, db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()
    return row is not None


def try_claim_seat(user_id: int, display_name: str, username: str | None) -> bool:
    """Attempt to pin a numeric Telegram ID onto an unclaimed seeded name row.

    Matches the seeded display_name against the user's @username or full name
    (case-insensitive, @-insensitive). Returns True if a seat was claimed.
    """
    candidates = {_normalize_name(display_name)} if display_name.strip() else set()
    if username:
        candidates.add(_normalize_name(username))
    if not candidates:
        return False

    with _AUTH_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT id, display_name FROM allowed_users WHERE telegram_user_id IS NULL"
        ).fetchall()
        for row_id, seeded_name in rows:
            if seeded_name and _normalize_name(seeded_name) in candidates:
                conn.execute(
                    """
                    UPDATE allowed_users
                    SET telegram_user_id = ?, username = ?, display_name = ?,
                        claimed_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND telegram_user_id IS NULL
                    """,
                    (user_id, username, display_name, row_id),
                )
                conn.commit()
                logger.info(
                    "Telegram user %s (%s) claimed allowlist seat previously seeded as '%s'.",
                    user_id, display_name, seeded_name,
                )
                return True
    return False


def add_allowed_user(
    user_id: int, *, display_name: str | None = None, username: str | None = None,
    source: str = "manual",
) -> None:
    """Manually allow a Telegram user ID (idempotent)."""
    with _AUTH_LOCK, db.connect() as conn:
        conn.execute(
            """
            INSERT INTO allowed_users
                (telegram_user_id, display_name, username, source, claimed_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, display_name),
                username     = COALESCE(excluded.username, username)
            """,
            (user_id, display_name, username, source),
        )
        conn.commit()


def list_allowed_users() -> list[AllowedUser]:
    with _AUTH_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_user_id, display_name, username, source, added_at,
                   claimed_at, plex_username
            FROM allowed_users ORDER BY id
            """
        ).fetchall()
    return [AllowedUser(*row) for row in rows]


def remove_allowed_user(row_id: int) -> bool:
    """Revoke a user's access. Returns True if a row was deleted."""
    with _AUTH_LOCK, db.connect() as conn:
        cursor = conn.execute("DELETE FROM allowed_users WHERE id = ?", (row_id,))
        conn.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Access requests — unknown users ask, admin approves in the desktop app
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccessRequest:
    request_id: int
    telegram_user_id: int
    display_name: str | None
    username: str | None
    chat_id: int | None
    status: str
    requested_at: str
    resolved_at: str | None


_REQUEST_COLUMNS = (
    "id, telegram_user_id, display_name, username, chat_id, status, requested_at, resolved_at"
)


def create_access_request(
    user_id: int, *, display_name: str | None, username: str | None,
    chat_id: int | None,
) -> str:
    """Record that an unknown user wants access.

    Returns the request's status after the call: 'pending' (newly created or
    already waiting) or 'denied' (admin previously said no — stays no until
    the admin adds them another way).
    """
    with _AUTH_LOCK, db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM access_requests WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()
        if row is not None:
            return str(row[0])
        conn.execute(
            """
            INSERT INTO access_requests (telegram_user_id, display_name, username, chat_id)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, display_name, username, chat_id),
        )
        conn.commit()
    logger.info(
        "New Telegram access request from id=%s name=%r username=%r",
        user_id, display_name, username,
    )
    return "pending"


def list_access_requests(*, status: str = "pending") -> list[AccessRequest]:
    with _AUTH_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM access_requests WHERE status = ? ORDER BY id",
            (status,),
        ).fetchall()
    return [AccessRequest(*row) for row in rows]


def pending_access_request_count() -> int:
    with _AUTH_LOCK, db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM access_requests WHERE status = 'pending'"
        ).fetchone()
    return int(row[0]) if row else 0


def _resolve_access_request(request_id: int, new_status: str) -> AccessRequest | None:
    with _AUTH_LOCK, db.connect() as conn:
        row = conn.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM access_requests WHERE id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE access_requests SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, request_id),
        )
        conn.commit()
    resolved = AccessRequest(*row)
    return AccessRequest(
        request_id=resolved.request_id,
        telegram_user_id=resolved.telegram_user_id,
        display_name=resolved.display_name,
        username=resolved.username,
        chat_id=resolved.chat_id,
        status=new_status,
        requested_at=resolved.requested_at,
        resolved_at=resolved.resolved_at,
    )


def approve_access_request(request_id: int) -> AccessRequest | None:
    """Approve a pending request: allowlist the user. Returns the request row
    (with chat_id) so the caller can notify them via the bot, or None if the
    request wasn't pending."""
    resolved = _resolve_access_request(request_id, "approved")
    if resolved is not None:
        add_allowed_user(
            resolved.telegram_user_id,
            display_name=resolved.display_name,
            username=resolved.username,
            source="admin-approved",
        )
    return resolved


def deny_access_request(request_id: int) -> AccessRequest | None:
    """Deny a pending request. The user stays blocked and is not re-prompted."""
    return _resolve_access_request(request_id, "denied")
