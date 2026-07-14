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
    # Explicit failure-memory context ("pack:title:2") for grabs that have
    # neither an episode context nor a request id (flag-driven season packs).
    ("downloads", "failure_key", "TEXT"),
    # Quality-replacement grabs: the old (cam/low-quality) file to delete
    # once this download has moved into the library.
    ("downloads", "replace_path", "TEXT"),
    # Exact file list reported by the download engine — post-processing uses
    # THIS instead of fuzzy staging-folder matching (which once swapped two
    # simultaneous downloads' files).
    ("downloads", "files_json", "TEXT"),
    # Immutable snapshot of what this grab was meant to satisfy (the
    # MediaIdentity + season/episode target + size prefs) frozen at grab time.
    # Every later check (verification, reconciliation, restart) reads THIS, so
    # editing/deleting the request or restarting the app can never change what
    # an in-flight download was for. NOTE: the table/column names in the ALTER
    # above are static literals — never interpolate user input into them.
    ("downloads", "want_json", "TEXT"),
    # Task C item 3 — tombstones. remove() ARCHIVES the row instead of deleting
    # it out from under its own provenance/history; the UI hides archived rows
    # by default. A quarantined download (verify-failed, kept in staging) is a
    # normal row whose verification landed on 'needs_attention'.
    ("downloads", "removed_at", "TEXT"),
    ("downloads", "removed_reason", "TEXT"),
    # Verification outcome for the whole download (rolled up from per-file
    # download_files rows): pending | verified | failed | partial | quarantined.
    ("downloads", "verification_state", "TEXT"),
    ("downloads", "verification_reason", "TEXT"),
    # The selection_run this download came from (Task C item 6 shares one across
    # a zero-seeder race so every racer links back to the single decision).
    ("downloads", "selection_run_id", "INTEGER"),
    # Task F: persistent quality label ('bluray-1080p', 'cam', ...) written at
    # grab time from the RTN parse of the chosen release — CAM knowledge lives
    # in the database, not filenames. Rides into media_quality on verified move.
    ("downloads", "quality_label", "TEXT"),
    # Task E retention (RESOLVED DECISION 5): keep_details exempts a run from
    # the 90-day loser-detail prune; verdict_histogram_json is the forever
    # rejection aggregate ({"cam_or_trash": 3, ...}) that survives pruning.
    ("selection_runs", "keep_details", "INTEGER NOT NULL DEFAULT 0"),
    ("selection_runs", "verdict_histogram_json", "TEXT"),
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
    files_json: str | None = None
    failure_key: str | None = None
    want_json: str | None = None
    removed_at: str | None = None
    removed_reason: str | None = None
    verification_state: str | None = None
    verification_reason: str | None = None
    selection_run_id: int | None = None
    quality_label: str | None = None


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
    "replace_path, files_json, failure_key, want_json, removed_at, "
    "removed_reason, verification_state, verification_reason, "
    "selection_run_id, quality_label"
)


_SCHEMA_DEFERRALS = """
CREATE TABLE IF NOT EXISTS grab_deferrals (
    key             TEXT PRIMARY KEY,
    first_seen      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason          TEXT,
    next_attempt_at TEXT
)
"""
# Deferrals gained reason + next_attempt_at (Task C item 9 / Task E) so the grab
# queue can render WHY a request is held and WHEN it will next be tried, plus
# the candidate stats + last selection_run_id of the pass that deferred (Task E
# item 1). Applied via ALTER on upgrade for tables that predate the columns.
_DEFERRAL_MIGRATIONS: list[tuple[str, str, str]] = [
    ("grab_deferrals", "reason", "TEXT"),
    ("grab_deferrals", "next_attempt_at", "TEXT"),
    ("grab_deferrals", "candidate_stats_json", "TEXT"),
    ("grab_deferrals", "selection_run_id", "INTEGER"),
]

# ---------------------------------------------------------------------------
# Selection provenance (Task B item 4 / RESOLVED DECISION 12): normalized
# selection_runs + candidate_decisions. Every automatic (and manual-preflight)
# torrent_select pass can be persisted here so the grab queue can render "what
# did it choose, why did that one win, and what did every loser fail on?".
#
# Phase 1 builds the tables + ONE transactional writer + a read API. NOTHING
# writes them automatically yet — the wiring lands in Phase 3. The ALTER/CREATE
# identifiers here are static literals; never interpolate user input into them.
# ---------------------------------------------------------------------------
_SCHEMA_SELECTION_RUNS = """
CREATE TABLE IF NOT EXISTS selection_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    mode             TEXT NOT NULL,
    profile          TEXT NOT NULL,
    rtn_version      TEXT NOT NULL,
    request_id       INTEGER,
    download_id      INTEGER,
    chosen_infohash  TEXT,
    chosen_title     TEXT,
    reason           TEXT,
    pool_stats_json  TEXT
)
"""

_SCHEMA_CANDIDATE_DECISIONS = """
CREATE TABLE IF NOT EXISTS candidate_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_run_id INTEGER NOT NULL,
    rank_position    INTEGER,
    infohash         TEXT,
    title            TEXT,
    passed           INTEGER NOT NULL DEFAULT 0,
    reason_code      TEXT NOT NULL,
    detail           TEXT,
    score_total      REAL,
    score_components_json TEXT,
    seeders          INTEGER,
    size_bytes       INTEGER
)
"""


# ---------------------------------------------------------------------------
# Blocklist (Task C item 2) — scoped, reason-coded. A release wrong for one
# identity can be right for another (Angry Birds 2 is wrong for movie 1, correct
# for a later movie-2 request), so every entry carries a subject scope. Race
# losers and transient failures NEVER land here (Design stance 7).
# ---------------------------------------------------------------------------
_SCHEMA_BLOCKLIST = """
CREATE TABLE IF NOT EXISTS blocklist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type  TEXT NOT NULL,   -- request_identity | show_season | show_episode | global_bad_release
    subject_key   TEXT NOT NULL,   -- e.g. 'tmdb:153518', 'show:12:s19:e03', '*'
    infohash      TEXT,
    parsed_title  TEXT,
    size_bytes    INTEGER,
    release_group TEXT,
    reason_code   TEXT NOT NULL,
    reason_detail TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by    TEXT
)
"""

# Reason codes that MEAN a permanent, subject-scoped blocklist entry.
BLOCK_REASON_IDENTITY_MISMATCH = "identity_mismatch"
BLOCK_REASON_USER_WRONG_PICK = "user_wrong_pick"
BLOCK_REASON_PAYLOAD_NAME_MISMATCH = "payload_name_mismatch"
BLOCK_REASON_USER_CANCEL_AND_BLOCK = "user_cancel_and_block"
BLOCK_REASON_GLOBAL_BAD_RELEASE = "global_bad_release"
# Reason codes that must NEVER create an entry.
NON_BLOCKING_REASONS = frozenset({
    "race_loser", "user_cancel_no_block",
    "download_stalled", "tracker_timeout", "client_error",
})

SUBJECT_GLOBAL = "*"

# ---------------------------------------------------------------------------
# Provenance (Task C item 3) — a file-link table, not a JSON blob. Supports
# several episode downloads fulfilling one season request, one pack fulfilling
# one request, and one deduplicated download satisfying two household requests.
# ---------------------------------------------------------------------------
_SCHEMA_DOWNLOAD_FILES = """
CREATE TABLE IF NOT EXISTS download_files (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id          INTEGER NOT NULL,
    source_relative_path TEXT,
    source_absolute_path TEXT,
    final_path           TEXT,
    media_role           TEXT,    -- primary_video | episode | subtitle | sample | extra | unknown
    parsed_title         TEXT,
    parsed_year          INTEGER,
    parsed_season        INTEGER,
    parsed_episode       INTEGER,
    language             TEXT,
    flags_json           TEXT,
    size_bytes           INTEGER,
    verification_state   TEXT,    -- verified | failed | duplicate | skipped
    verification_reason  TEXT,
    moved_at             TEXT,
    removed_at           TEXT
)
"""

_SCHEMA_REQUEST_DOWNLOADS = """
CREATE TABLE IF NOT EXISTS request_downloads (
    request_id  INTEGER NOT NULL,
    download_id INTEGER NOT NULL,
    role        TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (request_id, download_id)
)
"""

# ---------------------------------------------------------------------------
# Needs-placement (Task D item 3) — a download that COULDN'T route confidently
# and is sitting in staging. The grab-queue subtab (Phase 6) renders these with
# a one-click "create <root>/<Show>/Season NN" action; the store row + the
# action function (DownloadManager.create_placement_folder) are built now.
# ---------------------------------------------------------------------------
_SCHEMA_NEEDS_PLACEMENT = """
CREATE TABLE IF NOT EXISTS needs_placement (
    download_id   INTEGER PRIMARY KEY,
    show_id       INTEGER,
    season        INTEGER,
    suggested_dir TEXT,
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at   TEXT
)
"""


def _init_failed_grabs(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_grabs (
            context_key TEXT NOT NULL,
            magnet_hash TEXT NOT NULL,
            failed_at   REAL NOT NULL,
            PRIMARY KEY (context_key, magnet_hash)
        )
    """)


def record_failed_grab(context_key: str, magnet_hash: str) -> None:
    """Remember that this release failed for this episode/request, so the
    next retry prefers a different copy."""
    import time as _time
    if not context_key or not magnet_hash:
        return
    with _DL_LOCK, db.connect() as conn:
        _init_failed_grabs(conn)
        conn.execute(
            "INSERT INTO failed_grabs (context_key, magnet_hash, failed_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(context_key, magnet_hash)"
            " DO UPDATE SET failed_at = excluded.failed_at",
            (context_key, magnet_hash.lower(), _time.time()),
        )
        conn.commit()


def failed_grab_times(context_key: str) -> dict[str, float]:
    """magnet_hash -> last failure timestamp for one episode/request key."""
    with _DL_LOCK, db.connect() as conn:
        _init_failed_grabs(conn)
        rows = conn.execute(
            "SELECT magnet_hash, failed_at FROM failed_grabs WHERE context_key = ?",
            (context_key,)).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def _ensure_deferral_columns(conn) -> None:
    conn.execute(_SCHEMA_DEFERRALS)
    existing = {r[1] for r in conn.execute(
        "PRAGMA table_info(grab_deferrals)").fetchall()}
    for _t, col, col_def in _DEFERRAL_MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE grab_deferrals ADD COLUMN {col} {col_def}")


def check_grab_deferral(key: str, *, wait_hours: float = 24.0,
                        reason: str | None = None,
                        candidate_stats: dict | None = None,
                        selection_run_id: int | None = None) -> bool:
    """Oversize-only-result cooldown for ROUTINE grabs: first sighting
    records the key (with an optional reason + a computed next_attempt_at,
    plus the deferring pass's candidate stats and selection run when the
    caller has them) and returns False (wait); once wait_hours have passed it
    returns True (go ahead). Cleared on a successful grab. All the extra
    columns feed the grab-queue view (Task E)."""
    import datetime as _dt
    import json as _json
    now = _dt.datetime.now(_dt.timezone.utc)
    with _DL_LOCK, db.connect() as conn:
        _ensure_deferral_columns(conn)
        row = conn.execute(
            "SELECT first_seen FROM grab_deferrals WHERE key = ?", (key,)).fetchone()
        if row is None:
            next_at = (now + _dt.timedelta(hours=wait_hours)).isoformat()
            conn.execute(
                "INSERT INTO grab_deferrals (key, reason, next_attempt_at,"
                " candidate_stats_json, selection_run_id) VALUES (?, ?, ?, ?, ?)",
                (key, reason, next_at,
                 _json.dumps(candidate_stats) if candidate_stats else None,
                 selection_run_id))
            conn.commit()
            return False
        # Refresh the freshest evidence on an existing deferral without
        # resetting its clock.
        if candidate_stats or selection_run_id is not None:
            conn.execute(
                "UPDATE grab_deferrals SET candidate_stats_json ="
                " COALESCE(?, candidate_stats_json), selection_run_id ="
                " COALESCE(?, selection_run_id) WHERE key = ?",
                (_json.dumps(candidate_stats) if candidate_stats else None,
                 selection_run_id, key))
            conn.commit()
    try:
        # SQLite CURRENT_TIMESTAMP is naive UTC.
        first = _dt.datetime.fromisoformat(row[0]).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return True
    return (now - first).total_seconds() >= wait_hours * 3600


def set_grab_deferral(key: str, *, wait_hours: float = 24.0,
                      reason: str | None = None,
                      candidate_stats: dict | None = None,
                      selection_run_id: int | None = None) -> None:
    """Explicit deferral upsert (the grab-queue 'Defer' action): unlike
    check_grab_deferral this always (re)sets the clock to now + wait_hours."""
    import datetime as _dt
    import json as _json
    now = _dt.datetime.now(_dt.timezone.utc)
    next_at = (now + _dt.timedelta(hours=wait_hours)).isoformat()
    with _DL_LOCK, db.connect() as conn:
        _ensure_deferral_columns(conn)
        conn.execute(
            "INSERT INTO grab_deferrals (key, first_seen, reason,"
            " next_attempt_at, candidate_stats_json, selection_run_id)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET first_seen = excluded.first_seen,"
            " reason = excluded.reason, next_attempt_at = excluded.next_attempt_at,"
            " candidate_stats_json = COALESCE(excluded.candidate_stats_json,"
            "                                 candidate_stats_json),"
            " selection_run_id = COALESCE(excluded.selection_run_id,"
            "                             selection_run_id)",
            (key, now.isoformat(), reason, next_at,
             _json.dumps(candidate_stats) if candidate_stats else None,
             selection_run_id))
        conn.commit()


def _deferral_row_to_dict(row) -> dict:
    import json as _json
    stats = None
    if row[4]:
        try:
            stats = _json.loads(row[4])
        except (ValueError, TypeError):
            stats = None
    return {"key": row[0], "first_seen": row[1], "reason": row[2],
            "next_attempt_at": row[3], "candidate_stats": stats,
            "selection_run_id": row[5]}


def get_grab_deferral(key: str) -> dict | None:
    """Deferral detail (reason + next_attempt_at + candidate stats + last
    selection run) for the grab-queue view."""
    with _DL_LOCK, db.connect() as conn:
        _ensure_deferral_columns(conn)
        row = conn.execute(
            "SELECT key, first_seen, reason, next_attempt_at,"
            " candidate_stats_json, selection_run_id"
            " FROM grab_deferrals WHERE key = ?", (key,)).fetchone()
    return _deferral_row_to_dict(row) if row is not None else None


def list_grab_deferrals() -> list[dict]:
    """Every live deferral row (the grab queue joins these onto requests)."""
    with _DL_LOCK, db.connect() as conn:
        _ensure_deferral_columns(conn)
        rows = conn.execute(
            "SELECT key, first_seen, reason, next_attempt_at,"
            " candidate_stats_json, selection_run_id"
            " FROM grab_deferrals ORDER BY key").fetchall()
    return [_deferral_row_to_dict(r) for r in rows]


def deferral_expired(detail: dict | None,
                     now_iso: str | None = None) -> bool:
    """True when a deferral's next_attempt_at has passed (or is unreadable —
    a broken clock must never hold a request forever)."""
    import datetime as _dt
    if detail is None:
        return True
    next_at = detail.get("next_attempt_at")
    if not next_at:
        return True
    now = (_dt.datetime.fromisoformat(now_iso) if now_iso
           else _dt.datetime.now(_dt.timezone.utc))
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    try:
        when = _dt.datetime.fromisoformat(next_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return True
    return now >= when


def clear_grab_deferral(key: str) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(_SCHEMA_DEFERRALS)
        conn.execute("DELETE FROM grab_deferrals WHERE key = ?", (key,))
        conn.commit()


def initialize_downloads_db() -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(_SCHEMA_DOWNLOADS)
        conn.execute(_SCHEMA_HISTORY)
        conn.execute(_SCHEMA_DEFERRALS)
        conn.execute(_SCHEMA_SELECTION_RUNS)
        conn.execute(_SCHEMA_CANDIDATE_DECISIONS)
        conn.execute(_SCHEMA_BLOCKLIST)
        conn.execute(_SCHEMA_DOWNLOAD_FILES)
        conn.execute(_SCHEMA_REQUEST_DOWNLOADS)
        conn.execute(_SCHEMA_NEEDS_PLACEMENT)
        for table, column, col_def in (_MIGRATIONS + _DEFERRAL_MIGRATIONS):
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
        replace_path=row[20], files_json=row[21], failure_key=row[22],
        want_json=row[23], removed_at=row[24], removed_reason=row[25],
        verification_state=row[26], verification_reason=row[27],
        selection_run_id=row[28], quality_label=row[29],
    )


def create_download(
    *, title: str, magnet: str, source: str | None, media_type: str,
    request_id: int | None, staging_dir: str, planned_dest: str | None,
    planned_name: str | None, route_reason: str | None,
    auto_rename: bool, auto_move: bool,
    show_id: int | None = None, season: int | None = None,
    episode: int | None = None, replace_path: str | None = None,
    failure_key: str | None = None, want_json: str | None = None,
    selection_run_id: int | None = None, quality_label: str | None = None,
) -> int:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO downloads
                (request_id, title, magnet, source, media_type, staging_dir,
                 planned_dest, planned_name, route_reason, auto_rename, auto_move,
                 show_id, season, episode, replace_path, failure_key, want_json,
                 selection_run_id, quality_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (request_id, title, magnet, source, media_type, staging_dir,
             planned_dest, planned_name, route_reason,
             int(auto_rename), int(auto_move), show_id, season, episode,
             replace_path, failure_key, want_json, selection_run_id,
             quality_label),
        )
        conn.commit()
        download_id = int(cursor.lastrowid or 0)
    add_history(download_id, "grabbed", before=None, after=title)
    return download_id


def set_want(download_id: int, want: dict) -> None:
    """Freeze the want snapshot for a download. Written once at grab time and
    then treated as immutable — later checks READ it, never rewrite it."""
    import json as _json
    with _DL_LOCK, db.connect() as conn:
        conn.execute("UPDATE downloads SET want_json = ? WHERE id = ?",
                     (_json.dumps(want), download_id))
        conn.commit()


def get_want(download_id: int) -> dict | None:
    """Read back the frozen want snapshot, or None if this row predates it."""
    import json as _json
    row = get_download(download_id)
    if row is None or not row.want_json:
        return None
    try:
        data = _json.loads(row.want_json)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def delete_download(download_id: int) -> None:
    """Hard-delete a download row. Retained for callers that truly want the row
    gone; the DEFAULT removal path is tombstone_download (Task C item 3), which
    keeps the row + its provenance and merely archives it."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        conn.commit()


def list_quarantined_downloads(*, limit: int = 200) -> list[DownloadRow]:
    """Archived rows whose bytes are KEPT for possible adoption (Task C item 8):
    verify-failed / wrong-pick quarantines, not user recycles. The grab-queue
    quarantine browser renders these with age + size + total usage."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_DOWNLOAD_COLUMNS} FROM downloads"
            " WHERE removed_at IS NOT NULL AND removed_reason LIKE 'quarantine:%'"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_download(r) for r in rows]


def restore_download(download_id: int) -> None:
    """Un-archive a tombstoned row (quarantine adoption clears the tombstone)."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET removed_at = NULL, removed_reason = NULL"
            " WHERE id = ?", (download_id,))
        conn.commit()


def set_request_id(download_id: int, request_id: int | None) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute("UPDATE downloads SET request_id = ? WHERE id = ?",
                     (request_id, download_id))
        conn.commit()


def tombstone_download(download_id: int, *, reason: str) -> None:
    """Archive a download instead of deleting it (Task C item 3). The row and
    its download_files / request_downloads provenance survive; the UI hides
    archived rows by default. History and links are never touched."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET removed_at = CURRENT_TIMESTAMP,"
            " removed_reason = ? WHERE id = ?", (reason, download_id))
        conn.commit()


def set_verification(download_id: int, state: str,
                     reason: str | None = None) -> None:
    """Roll-up verification outcome for the whole download
    (pending|verified|failed|partial|quarantined)."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET verification_state = ?,"
            " verification_reason = ? WHERE id = ?",
            (state, reason, download_id))
        conn.commit()


def link_download_to_run(download_id: int, selection_run_id: int) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE downloads SET selection_run_id = ? WHERE id = ?",
            (selection_run_id, download_id))
        conn.commit()


def set_files(download_id: int, relative_paths: list[str]) -> None:
    """Record the engine-reported file list (paths relative to staging)."""
    import json as _json
    with _DL_LOCK, db.connect() as conn:
        conn.execute("UPDATE downloads SET files_json = ? WHERE id = ?",
                     (_json.dumps(relative_paths), download_id))
        conn.commit()


def set_status(download_id: int, status: str, *, error: str | None = None,
               completed: bool = False) -> None:
    if status == "error":
        _remember_failure(download_id)
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


def _remember_failure(download_id: int) -> None:
    """On error: log the release's info-hash against its episode/request
    context so retries prefer a different copy."""
    import re as _re
    row = get_download(download_id)
    if row is None:
        return
    m = _re.search(r"btih:([A-Za-z0-9]{32,40})", row.magnet or "")
    if not m:
        return
    key = getattr(row, "failure_key", None)
    if not key:
        if row.show_id is not None and row.season is not None and row.episode is not None:
            key = f"ep:{row.show_id}:{row.season}:{row.episode}"
        elif row.request_id is not None:
            key = f"req:{row.request_id}"
    if key:
        record_failed_grab(key, m.group(1))


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


def list_downloads(*, limit: int = 100,
                   include_removed: bool = False) -> list[DownloadRow]:
    initialize_downloads_db()
    where = "" if include_removed else "WHERE removed_at IS NULL"
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_DOWNLOAD_COLUMNS} FROM downloads {where} "
            f"ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_download(r) for r in rows]


def request_ids_with_downloads() -> set[int]:
    """Request IDs that already have a live grab — used by auto-grab to skip
    them. error/cancelled rows don't count (a failed grab must not strand its
    request forever; the blocklist, not the row's mere existence, is what stops
    a re-grab of a WRONG release). Archived (tombstoned) rows don't count."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT request_id FROM downloads"
            " WHERE request_id IS NOT NULL"
            " AND status NOT IN ('error', 'cancelled')"
            " AND removed_at IS NULL"
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


def history_for_download(download_id: int) -> list[HistoryRow]:
    """Every history row for one download (oldest first). Used by the move loop
    to attribute a partial file at a target to THIS download's own interrupted
    earlier pass — anything unattributed is a foreign file and never replaced."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, download_id, action, before_value, after_value, at
            FROM download_history WHERE download_id = ? ORDER BY id
            """,
            (download_id,),
        ).fetchall()
    return [HistoryRow(*r) for r in rows]


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


# ---------------------------------------------------------------------------
# Selection provenance API (Task B item 4 / RESOLVED DECISION 12)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectionRunRow:
    selection_run_id: int
    created_at: str
    mode: str
    profile: str
    rtn_version: str
    request_id: int | None
    download_id: int | None
    chosen_infohash: str | None
    chosen_title: str | None
    reason: str | None
    pool_stats_json: str | None
    # Task E retention (RESOLVED DECISION 5)
    keep_details: bool = False
    verdict_histogram_json: str | None = None


@dataclass(frozen=True)
class CandidateDecisionRow:
    candidate_decision_id: int
    selection_run_id: int
    rank_position: int | None
    infohash: str | None
    title: str | None
    passed: bool
    reason_code: str
    detail: str | None
    score_total: float | None
    score_components_json: str | None
    seeders: int | None
    size_bytes: int | None


def record_selection_run(decision, *, request_id: int | None = None,
                         download_id: int | None = None) -> int:
    """Persist a torrent_select.SelectionDecision transactionally: one
    selection_runs row plus one candidate_decisions row PER candidate, in a
    single transaction (never a read-modify-write from several sites — the whole
    decision lands atomically or not at all).

    `decision` is duck-typed against torrent_select.SelectionDecision so this
    module does not import the RTN-backed selector at module load. Returns the
    new selection_run id.

    Phase 1: this exists for Phase 3/4 to call. Nothing calls it automatically
    yet.
    """
    import json as _json

    verdicts = list(getattr(decision, "verdicts", ()) or ())
    scores = list(getattr(decision, "scores", ()) or ())
    # rank_position by infohash for any candidate that made the scored top list.
    positions = {getattr(s, "infohash", None): i + 1
                 for i, s in enumerate(scores)}
    score_by_hash = {getattr(s, "infohash", None): s for s in scores}

    # The forever rejection aggregate (Task E retention): stored at write time
    # so pruning the per-loser detail rows can never lose the histogram.
    histogram: dict = {}
    for v in verdicts:
        code = getattr(v, "reason_code", "") or ""
        histogram[code] = histogram.get(code, 0) + 1

    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO selection_runs
                (created_at, mode, profile, rtn_version, request_id, download_id,
                 chosen_infohash, chosen_title, reason, pool_stats_json,
                 verdict_histogram_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                getattr(decision, "created_at", ""),
                getattr(decision, "mode", ""),
                getattr(decision, "profile", ""),
                getattr(decision, "rtn_version", ""),
                request_id, download_id,
                getattr(decision, "chosen_infohash", None),
                getattr(decision, "chosen_title", None),
                getattr(decision, "reason", "") or "",
                _json.dumps(getattr(decision, "pool_stats", {}) or {}),
                _json.dumps(histogram),
            ),
        )
        run_id = int(cursor.lastrowid or 0)

        rows = []
        for v in verdicts:
            ih = getattr(v, "infohash", None)
            sb = score_by_hash.get(ih)
            rows.append((
                run_id,
                positions.get(ih),
                ih,
                getattr(v, "title", None),
                int(bool(getattr(v, "passed", False))),
                getattr(v, "reason_code", ""),
                getattr(v, "detail", ""),
                getattr(sb, "total", None) if sb is not None else None,
                (_json.dumps(getattr(sb, "components", {}))
                 if sb is not None else None),
                getattr(sb, "seeders", None) if sb is not None else None,
                getattr(sb, "size_bytes", None) if sb is not None else None,
            ))
        if rows:
            conn.executemany(
                """
                INSERT INTO candidate_decisions
                    (selection_run_id, rank_position, infohash, title, passed,
                     reason_code, detail, score_total, score_components_json,
                     seeders, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        conn.commit()
    return run_id


_RUN_COLS = ("id, created_at, mode, profile, rtn_version, request_id, "
             "download_id, chosen_infohash, chosen_title, reason, "
             "pool_stats_json, keep_details, verdict_histogram_json")


def _row_to_run(row) -> SelectionRunRow:
    return SelectionRunRow(
        selection_run_id=row[0], created_at=row[1], mode=row[2],
        profile=row[3], rtn_version=row[4], request_id=row[5],
        download_id=row[6], chosen_infohash=row[7], chosen_title=row[8],
        reason=row[9], pool_stats_json=row[10], keep_details=bool(row[11]),
        verdict_histogram_json=row[12])


def get_selection_run(selection_run_id: int) -> SelectionRunRow | None:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLS} FROM selection_runs WHERE id = ?",
            (selection_run_id,),
        ).fetchone()
    return _row_to_run(row) if row else None


def list_candidate_decisions(selection_run_id: int) -> list[CandidateDecisionRow]:
    """Every candidate's verdict for one run, scored survivors first (by
    rank_position), then rejections."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, selection_run_id, rank_position, infohash, title, passed,
                   reason_code, detail, score_total, score_components_json,
                   seeders, size_bytes
            FROM candidate_decisions
            WHERE selection_run_id = ?
            ORDER BY (rank_position IS NULL), rank_position, id
            """,
            (selection_run_id,),
        ).fetchall()
    return [
        CandidateDecisionRow(
            candidate_decision_id=r[0], selection_run_id=r[1],
            rank_position=r[2], infohash=r[3], title=r[4], passed=bool(r[5]),
            reason_code=r[6], detail=r[7], score_total=r[8],
            score_components_json=r[9], seeders=r[10], size_bytes=r[11],
        )
        for r in rows
    ]


def get_selection_run_for_download(download_id: int) -> SelectionRunRow | None:
    """Most recent selection run linked to a download (Phase 3/4 consumer)."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLS} FROM selection_runs WHERE download_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (download_id,),
        ).fetchone()
    return _row_to_run(row) if row else None


def set_keep_details(selection_run_id: int, keep: bool = True) -> None:
    """Per-run keep-details control (RESOLVED DECISION 5): a flagged run's
    per-loser candidate_decisions rows are exempt from the retention prune."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE selection_runs SET keep_details = ? WHERE id = ?",
            (1 if keep else 0, selection_run_id))
        conn.commit()


def export_selection_run_json(selection_run_id: int) -> str | None:
    """Full JSON export of one run — the escape hatch to save every loser's
    forensic detail BEFORE the 90-day prune deletes it. Returns None when the
    run does not exist."""
    import json as _json
    run = get_selection_run(selection_run_id)
    if run is None:
        return None
    decisions = list_candidate_decisions(selection_run_id)

    def _maybe_json(text):
        if not text:
            return None
        try:
            return _json.loads(text)
        except (ValueError, TypeError):
            return text

    payload = {
        "selection_run_id": run.selection_run_id,
        "created_at": run.created_at,
        "mode": run.mode,
        "profile": run.profile,
        "rtn_version": run.rtn_version,
        "request_id": run.request_id,
        "download_id": run.download_id,
        "chosen_infohash": run.chosen_infohash,
        "chosen_title": run.chosen_title,
        "reason": run.reason,
        "pool_stats": _maybe_json(run.pool_stats_json),
        "verdict_histogram": _maybe_json(run.verdict_histogram_json),
        "keep_details": run.keep_details,
        "candidates": [
            {
                "rank_position": d.rank_position,
                "infohash": d.infohash,
                "title": d.title,
                "passed": d.passed,
                "reason_code": d.reason_code,
                "detail": d.detail,
                "score_total": d.score_total,
                "score_components": _maybe_json(d.score_components_json),
                "seeders": d.seeders,
                "size_bytes": d.size_bytes,
            }
            for d in decisions
        ],
    }
    return _json.dumps(payload, indent=2)


def prune_selection_run_details(*, days: float = 90.0,
                                now: str | None = None) -> dict:
    """The ONE retention prune entrypoint (RESOLVED DECISION 5), run from the
    app's daily pass.

    Deletes per-LOSER candidate_decisions rows for runs older than `days`,
    EXCEPT: runs flagged keep_details, the chosen candidate's own row, and
    NEVER a selection_runs row (the forever receipt: wanted identity, chosen
    release, winning breakdown, verification, timestamps) or its
    verdict_histogram_json (the forever rejection aggregate). Runs written
    before histograms existed get theirs computed from the detail rows and
    stored BEFORE those rows are deleted.
    """
    import datetime as _dt
    import json as _json
    if now is None:
        now_dt = _dt.datetime.now(_dt.timezone.utc)
    else:
        now_dt = _dt.datetime.fromisoformat(now)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=_dt.timezone.utc)
    cutoff = (now_dt - _dt.timedelta(days=days)).isoformat()

    initialize_downloads_db()
    runs_pruned = 0
    rows_deleted = 0
    with _DL_LOCK, db.connect() as conn:
        runs = conn.execute(
            "SELECT id, chosen_infohash, verdict_histogram_json"
            " FROM selection_runs"
            " WHERE keep_details = 0 AND created_at != ''"
            "   AND created_at < ?", (cutoff,)).fetchall()
        for run_id, chosen_infohash, histogram_json in runs:
            if not histogram_json:
                # Pre-histogram run: preserve the aggregate before deleting
                # the only place it can still be derived from.
                hist: dict = {}
                for (code,) in conn.execute(
                        "SELECT reason_code FROM candidate_decisions"
                        " WHERE selection_run_id = ?", (run_id,)).fetchall():
                    hist[code or ""] = hist.get(code or "", 0) + 1
                conn.execute(
                    "UPDATE selection_runs SET verdict_histogram_json = ?"
                    " WHERE id = ?", (_json.dumps(hist), run_id))
            if chosen_infohash:
                cursor = conn.execute(
                    "DELETE FROM candidate_decisions"
                    " WHERE selection_run_id = ?"
                    "   AND (infohash IS NULL OR infohash != ?)",
                    (run_id, chosen_infohash))
            else:
                cursor = conn.execute(
                    "DELETE FROM candidate_decisions"
                    " WHERE selection_run_id = ?", (run_id,))
            if cursor.rowcount > 0:
                runs_pruned += 1
                rows_deleted += cursor.rowcount
        conn.commit()
    return {"runs_pruned": runs_pruned, "rows_deleted": rows_deleted,
            "cutoff": cutoff}


# ---------------------------------------------------------------------------
# Blocklist API (Task C item 2) — scoped, reason-coded
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlocklistRow:
    blocklist_id: int
    subject_type: str
    subject_key: str
    infohash: str | None
    parsed_title: str | None
    size_bytes: int | None
    release_group: str | None
    reason_code: str
    reason_detail: str | None
    created_at: str
    created_by: str | None


_BLOCKLIST_COLS = ("id, subject_type, subject_key, infohash, parsed_title, "
                   "size_bytes, release_group, reason_code, reason_detail, "
                   "created_at, created_by")


def add_blocklist_entry(*, subject_type: str, subject_key: str,
                        reason_code: str, infohash: str | None = None,
                        parsed_title: str | None = None,
                        size_bytes: int | None = None,
                        release_group: str | None = None,
                        reason_detail: str | None = None,
                        created_by: str | None = None) -> int:
    """Record a permanent, subject-scoped blocklist entry. Reasons in
    NON_BLOCKING_REASONS (race_loser + the transient failures) are refused here
    — they must never create an entry (Design stance 7). Returns the new row id,
    or 0 when the reason is non-blocking (a deliberate no-op)."""
    if reason_code in NON_BLOCKING_REASONS:
        return 0
    initialize_downloads_db()
    ih = infohash.lower() if infohash else None
    with _DL_LOCK, db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO blocklist (subject_type, subject_key, infohash,"
            " parsed_title, size_bytes, release_group, reason_code,"
            " reason_detail, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subject_type, subject_key, ih, parsed_title, size_bytes,
             release_group, reason_code, reason_detail, created_by))
        conn.commit()
        return int(cursor.lastrowid or 0)


def _row_to_blocklist(r) -> BlocklistRow:
    return BlocklistRow(
        blocklist_id=r[0], subject_type=r[1], subject_key=r[2], infohash=r[3],
        parsed_title=r[4], size_bytes=r[5], release_group=r[6],
        reason_code=r[7], reason_detail=r[8], created_at=r[9], created_by=r[10])


def blocklist_entries_for_subject(subject_key: str | None) -> list[BlocklistRow]:
    """Every entry that applies to a want with this subject_key: the
    subject-scoped rows PLUS any global_bad_release rows (which apply to
    everything). A None/empty subject_key still returns the globals."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        if subject_key:
            rows = conn.execute(
                f"SELECT {_BLOCKLIST_COLS} FROM blocklist"
                " WHERE subject_key = ? OR subject_type = 'global_bad_release'",
                (subject_key,)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_BLOCKLIST_COLS} FROM blocklist"
                " WHERE subject_type = 'global_bad_release'").fetchall()
    return [_row_to_blocklist(r) for r in rows]


def list_blocklist(*, limit: int = 500) -> list[BlocklistRow]:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_BLOCKLIST_COLS} FROM blocklist"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_blocklist(r) for r in rows]


def remove_blocklist_entry(blocklist_id: int) -> None:
    with _DL_LOCK, db.connect() as conn:
        conn.execute("DELETE FROM blocklist WHERE id = ?", (blocklist_id,))
        conn.commit()


def widen_blocklist_to_global(blocklist_id: int) -> None:
    """Promote a subject-scoped entry to global_bad_release (Task C item 8:
    only an explicit, separately confirmed user action may do this)."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE blocklist SET subject_type = 'global_bad_release',"
            " subject_key = ?, reason_code = ? WHERE id = ?",
            (SUBJECT_GLOBAL, BLOCK_REASON_GLOBAL_BAD_RELEASE, blocklist_id))
        conn.commit()


# ---------------------------------------------------------------------------
# Provenance API (Task C item 3) — download_files + request_downloads junction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DownloadFileRow:
    file_id: int
    download_id: int
    source_relative_path: str | None
    source_absolute_path: str | None
    final_path: str | None
    media_role: str | None
    parsed_title: str | None
    parsed_year: int | None
    parsed_season: int | None
    parsed_episode: int | None
    language: str | None
    flags_json: str | None
    size_bytes: int | None
    verification_state: str | None
    verification_reason: str | None
    moved_at: str | None
    removed_at: str | None


_DOWNLOAD_FILE_COLS = (
    "id, download_id, source_relative_path, source_absolute_path, final_path, "
    "media_role, parsed_title, parsed_year, parsed_season, parsed_episode, "
    "language, flags_json, size_bytes, verification_state, verification_reason, "
    "moved_at, removed_at")


def add_download_file(download_id: int, *, source_relative_path: str | None,
                      source_absolute_path: str | None,
                      media_role: str | None, parsed_title: str | None = None,
                      parsed_year: int | None = None,
                      parsed_season: int | None = None,
                      parsed_episode: int | None = None,
                      language: str | None = None,
                      flags_json: str | None = None,
                      size_bytes: int | None = None,
                      final_path: str | None = None,
                      verification_state: str | None = None,
                      verification_reason: str | None = None,
                      moved_at: str | None = None) -> int:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO download_files (download_id, source_relative_path,"
            " source_absolute_path, final_path, media_role, parsed_title,"
            " parsed_year, parsed_season, parsed_episode, language, flags_json,"
            " size_bytes, verification_state, verification_reason, moved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (download_id, source_relative_path, source_absolute_path,
             final_path, media_role, parsed_title, parsed_year, parsed_season,
             parsed_episode, language, flags_json, size_bytes,
             verification_state, verification_reason, moved_at))
        conn.commit()
        return int(cursor.lastrowid or 0)


def _row_to_download_file(r) -> DownloadFileRow:
    return DownloadFileRow(
        file_id=r[0], download_id=r[1], source_relative_path=r[2],
        source_absolute_path=r[3], final_path=r[4], media_role=r[5],
        parsed_title=r[6], parsed_year=r[7], parsed_season=r[8],
        parsed_episode=r[9], language=r[10], flags_json=r[11], size_bytes=r[12],
        verification_state=r[13], verification_reason=r[14], moved_at=r[15],
        removed_at=r[16])


def list_download_files(download_id: int, *,
                        include_removed: bool = False) -> list[DownloadFileRow]:
    initialize_downloads_db()
    where = "WHERE download_id = ?"
    if not include_removed:
        where += " AND removed_at IS NULL"
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_DOWNLOAD_FILE_COLS} FROM download_files {where}"
            " ORDER BY id", (download_id,)).fetchall()
    return [_row_to_download_file(r) for r in rows]


def clear_download_files(download_id: int) -> None:
    """Drop the per-file rows for a download (a fresh post-process pass rebuilds
    them; keeps re-runs idempotent instead of duplicating file rows)."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute("DELETE FROM download_files WHERE download_id = ?",
                     (download_id,))
        conn.commit()


def link_request_download(request_id: int, download_id: int,
                          role: str | None = None) -> None:
    """Junction row tying a request to a download (Task C item 3/4). Idempotent
    on (request_id, download_id)."""
    if request_id is None or download_id is None:
        return
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "INSERT INTO request_downloads (request_id, download_id, role)"
            " VALUES (?, ?, ?) ON CONFLICT(request_id, download_id)"
            " DO UPDATE SET role = excluded.role",
            (request_id, download_id, role))
        conn.commit()


@dataclass(frozen=True)
class NeedsPlacementRow:
    download_id: int
    show_id: int | None
    season: int | None
    suggested_dir: str | None
    reason: str | None
    created_at: str


def record_needs_placement(download_id: int, *, show_id: int | None,
                           season: int | None, suggested_dir: str | None,
                           reason: str | None) -> None:
    """Flag a download that landed in staging without a confident route. Upsert
    on download_id so a re-processed download refreshes rather than duplicates."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "INSERT INTO needs_placement (download_id, show_id, season,"
            " suggested_dir, reason, resolved_at) VALUES (?, ?, ?, ?, ?, NULL)"
            " ON CONFLICT(download_id) DO UPDATE SET show_id = excluded.show_id,"
            " season = excluded.season, suggested_dir = excluded.suggested_dir,"
            " reason = excluded.reason, resolved_at = NULL",
            (download_id, show_id, season, suggested_dir, reason))
        conn.commit()


def clear_needs_placement(download_id: int) -> None:
    """Mark a needs-placement row resolved (the download was placed)."""
    with _DL_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE needs_placement SET resolved_at = CURRENT_TIMESTAMP"
            " WHERE download_id = ?", (download_id,))
        conn.commit()


def list_needs_placement() -> list[NeedsPlacementRow]:
    """Unresolved needs-placement rows for the grab queue."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT download_id, show_id, season, suggested_dir, reason,"
            " created_at FROM needs_placement WHERE resolved_at IS NULL"
            " ORDER BY created_at DESC").fetchall()
    return [NeedsPlacementRow(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]


def get_needs_placement(download_id: int) -> NeedsPlacementRow | None:
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        r = conn.execute(
            "SELECT download_id, show_id, season, suggested_dir, reason,"
            " created_at FROM needs_placement WHERE download_id = ?"
            " AND resolved_at IS NULL", (download_id,)).fetchone()
    return NeedsPlacementRow(r[0], r[1], r[2], r[3], r[4], r[5]) if r else None


def downloads_for_request(request_id: int, *,
                          include_removed: bool = True) -> list[DownloadRow]:
    """Every download linked to a request via the junction OR the legacy
    downloads.request_id column (so pre-junction rows still resolve)."""
    initialize_downloads_db()
    with _DL_LOCK, db.connect() as conn:
        ids = {r[0] for r in conn.execute(
            "SELECT download_id FROM request_downloads WHERE request_id = ?",
            (request_id,)).fetchall()}
        ids |= {r[0] for r in conn.execute(
            "SELECT id FROM downloads WHERE request_id = ?",
            (request_id,)).fetchall()}
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        where = f"WHERE id IN ({placeholders})"
        if not include_removed:
            where += " AND removed_at IS NULL"
        rows = conn.execute(
            f"SELECT {_DOWNLOAD_COLUMNS} FROM downloads {where} ORDER BY id",
            tuple(ids)).fetchall()
    return [_row_to_download(r) for r in rows]
