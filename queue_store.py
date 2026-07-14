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
# Schema v3 (identity contract): + identity_source, canonical_year,
#           origin_countries_json, aliases_json, season, batch_id,
#           placed_at, library_verified_at. resolved_title is RETAINED as the
#           canonical title (there is deliberately NO canonical_title column).
# =============================================================================

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import config
import db
import media_identity

_DB_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Request lifecycle (replaces the old bare open|done pair)
# ---------------------------------------------------------------------------
# needs_identity : typed movie/tv/anime row with no qualified identity yet —
#                  NEVER auto-grabbable, visible with a resolve action.
# open           : resolved + auto-grabbable, no download yet.
# deferred       : grabbable but held (oversize-only pass, etc.) with a reason.
# grabbing       : a download is in flight.
# verifying      : download complete, pre-move identity/route verification.
# placed         : verified files moved to the planned location (placed_at).
# fulfilled      : library index confirmed the intended identity is present.
# needs_attention: verification failed / partial — quarantined, needs a human.
# cancelled      : abandoned by the user.
STATUS_NEEDS_IDENTITY = "needs_identity"
STATUS_OPEN = "open"
STATUS_DEFERRED = "deferred"
STATUS_GRABBING = "grabbing"
STATUS_VERIFYING = "verifying"
STATUS_PLACED = "placed"
STATUS_FULFILLED = "fulfilled"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_CANCELLED = "cancelled"

# The set the user still has outstanding work on (everything that is not a
# terminal state). The desktop Requests tab and the Telegram lists render this.
ACTIVE_STATUSES: tuple[str, ...] = (
    STATUS_NEEDS_IDENTITY, STATUS_OPEN, STATUS_DEFERRED, STATUS_GRABBING,
    STATUS_VERIFYING, STATUS_PLACED, STATUS_NEEDS_ATTENTION,
)
TERMINAL_STATUSES: tuple[str, ...] = (STATUS_FULFILLED, STATUS_CANCELLED)
# Only fully-resolved 'open' rows are auto-grabbable. needs_identity is
# structurally excluded here — a bare external_id is not an identity.
AUTO_GRABBABLE_STATUSES: tuple[str, ...] = (STATUS_OPEN,)

# Bumped when the one-time data backfill in initialize_queue_db() lands, so it
# runs exactly once against legacy rows and never re-classifies rows created
# afterwards. Column ALTERs stay idempotent independently of this.
_SCHEMA_USER_VERSION = 3

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueRequest:
    request_id: int
    requester: str
    content: str              # raw user text
    status: str               # see the STATUS_* lifecycle constants above
    created_at: str
    completed_at: str | None
    media_type: str           # "movie" | "tv" | "anime" | "xanime" | "other" | "unknown"
    resolved_title: str | None  # canonical title resolved from TMDB/TVDB/MAL
    external_id: str | None   # ID on external DB (TMDB id, MAL id, etc.)
    external_url: str | None  # link to the entry on the external DB
    found_in_library: bool    # OBSERVATION only — never treated as completion
    library_checked_at: str | None  # timestamp of last library check
    # Schema v3 — provider-qualified identity + season targeting.
    identity_source: str | None = None  # tmdb|tvdb|jikan|anidb|anilist|omdb
    canonical_year: int | None = None
    origin_countries_json: str | None = None  # e.g. '["US"]'
    aliases_json: str | None = None           # includes the ASCII search alias
    season: int | None = None                 # NULL for movies; a season for tv
    batch_id: str | None = None               # groups an "All seasons" expansion
    placed_at: str | None = None              # verified files moved to plan
    library_verified_at: str | None = None    # library index confirmed identity

    @property
    def is_qualified(self) -> bool:
        """True when this row carries a provider-qualified identity and is
        therefore eligible to be auto-grabbed. 'other' is exempt by design."""
        if self.media_type in ("other", "unknown"):
            return False
        return bool(self.identity_source) and bool(self.external_id)

    @property
    def origin_countries(self) -> list[str]:
        if not self.origin_countries_json:
            return []
        try:
            data = json.loads(self.origin_countries_json)
            return [str(c) for c in data] if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    @property
    def aliases(self) -> list[str]:
        if not self.aliases_json:
            return []
        try:
            data = json.loads(self.aliases_json)
            return [str(a) for a in data] if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []


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

# Additive column migrations; each tuple is (column_name, column_def). These
# are idempotent (guarded by an existing-column check) and safe to re-run.
_V2_COLUMNS: list[tuple[str, str]] = [
    ("media_type",         "TEXT    NOT NULL DEFAULT 'unknown'"),
    ("resolved_title",     "TEXT"),
    ("external_id",        "TEXT"),
    ("external_url",       "TEXT"),
    ("found_in_library",   "INTEGER NOT NULL DEFAULT 0"),
    ("library_checked_at", "TEXT"),
]

_V3_COLUMNS: list[tuple[str, str]] = [
    ("identity_source",       "TEXT"),
    ("canonical_year",        "INTEGER"),
    ("origin_countries_json", "TEXT"),
    ("aliases_json",          "TEXT"),
    ("season",                "INTEGER"),
    ("batch_id",              "TEXT"),
    ("placed_at",             "TEXT"),
    ("library_verified_at",   "TEXT"),
]

# Maps an external_url host to the source token media_lookup already emits.
# The stored URL is offline proof the id resolved at creation time, so this
# backfill never needs the network. imdb.com maps to 'omdb' because that is the
# source media_lookup uses for imdb-id results (media_lookup.py search_omdb).
_URL_HOST_TO_SOURCE: list[tuple[str, str]] = [
    ("themoviedb.org", "tmdb"),
    ("thetvdb.com",    "tvdb"),
    ("myanimelist.net", "jikan"),
    ("anidb.net",      "anidb"),
    ("anilist.co",     "anilist"),
    ("imdb.com",       "omdb"),
]


def _source_from_url(url: str | None) -> str | None:
    if not url:
        return None
    low = url.lower()
    for host, source in _URL_HOST_TO_SOURCE:
        if host in low:
            return source
    return None


def initialize_queue_db() -> None:
    """Create the requests table, apply pending column migrations, and run the
    one-time v3 lifecycle/identity backfill (gated by PRAGMA user_version so it
    touches legacy rows exactly once and never re-classifies later rows)."""
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with _DB_LOCK, db.connect(db_path) as conn:
        conn.execute(_SCHEMA_V1)
        conn.commit()

        # Discover which columns already exist. NOTE: the table name here is a
        # static literal — never interpolate user-supplied identifiers into
        # these ALTER/PRAGMA statements.
        existing_cols: set[str] = {
            row[1]
            for row in conn.execute("PRAGMA table_info(requests)").fetchall()
        }

        for col_name, col_def in (_V2_COLUMNS + _V3_COLUMNS):
            if col_name not in existing_cols:
                conn.execute(
                    f"ALTER TABLE requests ADD COLUMN {col_name} {col_def}"
                )

        conn.commit()

        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if user_version < _SCHEMA_USER_VERSION:
            _backfill_v3(conn)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_USER_VERSION}")
            conn.commit()


def _backfill_v3(conn: sqlite3.Connection) -> None:
    """One-time normalisation of legacy rows into the v3 lifecycle.

    - done -> fulfilled.
    - open rows WITH an external_id whose provider is readable from the stored
      URL get identity_source backfilled and STAY open (auto-grabbable). The
      URL is offline proof the id resolved; no network call is made.
    - open typed rows we cannot qualify offline (no external_id, or a provider
      we cannot read from the URL) become needs_identity — never silently
      auto-grabbable. 'unknown' rows are INCLUDED here: a typeless row that
      auto-grab used to coerce to 'other' and grab anyway is exactly how #85
      happened. Only an explicit 'other' row is exempt (a deliberate human
      choice, per Task A).
    - found_in_library and the poisoned 55/86 state are NOT repaired here; that
      is Task C's evidence-based reconciliation.
    """
    conn.execute(
        "UPDATE requests SET status = ? WHERE status = 'done'",
        (STATUS_FULFILLED,),
    )

    rows = conn.execute(
        "SELECT id, media_type, external_id, external_url, identity_source,"
        " resolved_title, content, aliases_json"
        " FROM requests WHERE status = 'open'"
    ).fetchall()

    for row in rows:
        (req_id, media_type, external_id, external_url, identity_source,
         resolved_title, content, aliases_json) = row
        media_type = media_type or "unknown"
        # Only an explicit 'other' request is exempt from the identity rule.
        if media_type == "other":
            continue
        source = identity_source or _source_from_url(external_url)
        has_id = bool(external_id and str(external_id).strip())
        if has_id and source:
            if source != identity_source:
                conn.execute(
                    "UPDATE requests SET identity_source = ? WHERE id = ?",
                    (source, req_id),
                )
            # Store the ASCII-preferring search alias now (item 5) so the
            # Task A/B query builders have it without re-deriving it. Only
            # fill it when absent — never clobber an existing alias.
            if not aliases_json:
                alias = media_identity.search_alias(resolved_title, content)
                if alias:
                    conn.execute(
                        "UPDATE requests SET aliases_json = ? WHERE id = ?",
                        (json.dumps([alias]), req_id),
                    )
            # stays 'open' — provider-qualified, auto-grabbable
        else:
            conn.execute(
                "UPDATE requests SET status = ? WHERE id = ?",
                (STATUS_NEEDS_IDENTITY, req_id),
            )


# ---------------------------------------------------------------------------
# Row → dataclass helper
# ---------------------------------------------------------------------------

# Every column the row->dataclass helper reads. Kept in one place so the
# SELECTs and _row_to_request never drift apart.
_SELECT_COLS = (
    "id, requester, content, status, created_at, completed_at, "
    "media_type, resolved_title, external_id, external_url, "
    "found_in_library, library_checked_at, identity_source, canonical_year, "
    "origin_countries_json, aliases_json, season, batch_id, placed_at, "
    "library_verified_at"
)


def _row_to_request(row: sqlite3.Row) -> QueueRequest:
    keys = set(row.keys())

    def opt(name):
        return row[name] if name in keys else None

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
        identity_source=opt("identity_source"),
        canonical_year=opt("canonical_year"),
        origin_countries_json=opt("origin_countries_json"),
        aliases_json=opt("aliases_json"),
        season=opt("season"),
        batch_id=opt("batch_id"),
        placed_at=opt("placed_at"),
        library_verified_at=opt("library_verified_at"),
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
    status: str = STATUS_OPEN,
    identity_source: str | None = None,
    canonical_year: int | None = None,
    origin_countries: list[str] | None = None,
    aliases: list[str] | None = None,
    season: int | None = None,
    batch_id: str | None = None,
) -> QueueRequest:
    """
    Add a new request to the queue.

    Args:
        content: Raw user-supplied text describing what they want.
        requester: Telegram username or display name of the requester.
        media_type: Category — "movie", "tv", "anime", "xanime", "other", "unknown".
        resolved_title: Canonical title from TMDB/TVDB/MAL (if known).
        external_id: ID string on the external DB.
        external_url: URL to the entry on the external DB.
        status: initial lifecycle state (defaults to 'open'; intake surfaces
            that cannot resolve an identity pass STATUS_NEEDS_IDENTITY).
        identity_source: provider token (tmdb|tvdb|jikan|anidb|anilist|omdb).
        canonical_year / origin_countries / aliases / season / batch_id:
            v3 identity + season-targeting fields.
    """
    clean_content = " ".join(content.split())
    clean_requester = " ".join(requester.split()) or "Unknown"
    if not clean_content:
        raise ValueError("Request content cannot be empty.")

    countries_json = json.dumps(origin_countries) if origin_countries else None
    aliases_json = json.dumps(aliases) if aliases else None

    initialize_queue_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO requests
                (requester, content, status, media_type, resolved_title,
                 external_id, external_url, identity_source, canonical_year,
                 origin_countries_json, aliases_json, season, batch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (clean_requester, clean_content, status, media_type, resolved_title,
             external_id, external_url, identity_source, canonical_year,
             countries_json, aliases_json, season, batch_id),
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
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM requests WHERE id = ?",
            (request_id,),
        ).fetchone()

    return _row_to_request(row) if row is not None else None


def list_requests(*, status: str | None = "open", limit: int = 50) -> list[QueueRequest]:
    """List requests filtered by status.

    `status` accepts a concrete lifecycle state, or one of the selectors
    "active" (every non-terminal state) and "all" (no filter). Callers that
    want the outstanding-work view (desktop tab, Telegram lists) pass "active".
    """
    initialize_queue_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        if status == "all" or status is None:
            where, params = "", []
        elif status == "active":
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            where, params = f"WHERE status IN ({placeholders})", list(ACTIVE_STATUSES)
        else:
            where, params = "WHERE status = ?", [status]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM requests {where} "
            f"ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()

    return [_row_to_request(row) for row in rows]


def complete_request(request_id: int) -> bool:
    """Mark a request fulfilled. Works from any non-terminal state (an
    already fulfilled/cancelled row is left untouched)."""
    initialize_queue_db()
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    with _DB_LOCK, db.connect(_db_path()) as conn:
        cursor = conn.execute(
            f"""
            UPDATE requests
            SET status = ?, completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status NOT IN ({placeholders})
            """,
            (STATUS_FULFILLED, request_id, *TERMINAL_STATUSES),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_identity(
    request_id: int,
    *,
    media_type: str,
    resolved_title: str | None,
    external_id: str | None,
    external_url: str | None = None,
    identity_source: str | None = None,
    canonical_year: int | None = None,
    origin_countries: list[str] | None = None,
    aliases: list[str] | None = None,
    season: int | None = None,
) -> QueueRequest | None:
    """Attach a resolved provider identity to an existing row (the resolve path
    for needs_identity rows). Flips the row to 'open' (auto-grabbable) only when
    the identity is actually qualified; otherwise it stays needs_identity."""
    qualified = bool(identity_source) and bool(external_id)
    new_status = STATUS_OPEN if qualified else STATUS_NEEDS_IDENTITY
    countries_json = json.dumps(origin_countries) if origin_countries else None
    aliases_json = json.dumps(aliases) if aliases else None
    initialize_queue_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.execute(
            """
            UPDATE requests
            SET media_type = ?, resolved_title = ?, external_id = ?,
                external_url = ?, identity_source = ?, canonical_year = ?,
                origin_countries_json = ?, aliases_json = ?, season = ?,
                status = ?
            WHERE id = ?
            """,
            (media_type, resolved_title, external_id, external_url,
             identity_source, canonical_year, countries_json, aliases_json,
             season, new_status, request_id),
        )
        conn.commit()
    return get_request(request_id)


def set_status(request_id: int, status: str) -> None:
    """Move a request to an explicit lifecycle state. Stamps placed_at /
    library_verified_at when entering those states."""
    initialize_queue_db()
    extra = ""
    if status == STATUS_PLACED:
        extra = ", placed_at = CURRENT_TIMESTAMP"
    elif status == STATUS_FULFILLED:
        extra = ", library_verified_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP"
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.execute(
            f"UPDATE requests SET status = ?{extra} WHERE id = ?",
            (status, request_id),
        )
        conn.commit()


def update_library_status(request_id: int, *, found: bool) -> None:
    """Mark a request as found (or not found) in the Plex library after a check."""
    initialize_queue_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
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
    Return active requests that haven't been library-checked in the last
    max_age_hours, or have never been checked. needs_identity rows are excluded
    — they carry no resolved title, and checking them by raw content is the
    exact false-positive path that poisoned request #85. (Task C replaces this
    substring check with identity-based reconciliation.)
    """
    initialize_queue_db()
    checkable = tuple(s for s in ACTIVE_STATUSES if s != STATUS_NEEDS_IDENTITY)
    placeholders = ",".join("?" for _ in checkable)
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT {_SELECT_COLS}
            FROM requests
            WHERE status IN ({placeholders})
              AND (
                library_checked_at IS NULL
                OR datetime(library_checked_at) < datetime('now', ? || ' hours')
              )
            ORDER BY id ASC
            """,
            (*checkable, f"-{max_age_hours}"),
        ).fetchall()

    return [_row_to_request(row) for row in rows]


def find_duplicate_requests() -> list[list[QueueRequest]]:
    """
    Find open requests that look like duplicates of each other.

    Two requests are duplicates when they share a provider-qualified identity
    tuple (media_type, identity_source, external_id, season) — the durable key,
    which correctly separates S01 and S02 of the same show and correctly merges
    two rows resolved to the same tmdb id under different typed titles. Rows
    that lack a qualified identity fall back to the normalised-title key so
    needs_identity / 'other' rows still dedupe by what the user typed.

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
        if req.is_qualified:
            key = (f"id::{req.media_type}::{req.identity_source}::"
                   f"{req.external_id}::{req.season}")
        else:
            key_text = req.resolved_title or req.content
            key = f"{req.media_type}::{_normalize(key_text)}"
        seen.setdefault(key, []).append(req)

    return [group for group in seen.values() if len(group) >= 2]


def open_request_count() -> int:
    """Count of requests with outstanding work (every non-terminal state)."""
    initialize_queue_db()
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    with _DB_LOCK, db.connect(_db_path()) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM requests WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
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


def _needs_identity_tag(item: QueueRequest) -> str:
    return " (needs identity)" if item.status == STATUS_NEEDS_IDENTITY else ""


def format_requests_message_user(*, status: str = "active", limit: int = 20) -> str:
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
        lines.append(f"{emoji} {display}{checkmark}{_needs_identity_tag(item)}")

    return "\n".join(lines)


def format_requests_message(*, status: str = "active", limit: int = 10) -> str:
    """Legacy alias — calls the user-facing formatter. Used by /requests command."""
    return format_requests_message_user(status=status, limit=limit)


def format_requests_message_dev(*, status: str = "active", limit: int = 50) -> str:
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
        display = (item.resolved_title or item.content) + _needs_identity_tag(item)
        checkmark = " ✅" if item.found_in_library else ""
        link = f" → {item.external_url}" if item.external_url else ""
        lines.append(
            f"#{item.request_id} {emoji} [{item.created_at}] {item.requester}: "
            f"{display}{checkmark}{link}"
        )
    return "\n".join(lines)
