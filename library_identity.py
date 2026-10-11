# =============================================================================
# library_identity.py  (Fix-sprint Task J — library identity backbone)
# =============================================================================
# The durable "what IS this file" table. 94% of library files already sit under
# a tracked show_folder with an external id, and ~29846 are already matched to a
# specific episode row — yet dupes, rename, and presence checks used to re-derive
# identity from a filename regex every single time. This table stops that: once a
# file's identity is known (from an episode row, a show-folder mapping, a placed
# download, or a batch provider lookup), it is stored keyed by path and the
# consumers join against it instead of parsing strings.
#
# Contract:
#   - A row exists ONLY for a resolved file. An unresolved file has no row and
#     surfaces as unidentified. Never invent an identity.
#   - Rows are written at placement (the download pipeline knows the request
#     identity when it moves files), at manual resolve, and by the backfill job.
#   - Rows die with the file: remove_from_index / the refresh delta prune every
#     row whose path is no longer in the library_files index.
#   - provenance (resolved_by) is stored and surfaced; the consumers never
#     auto-act (delete/rename) on an inherited or batch-resolved identity without
#     showing it.
#
# This module imports only `db` at module scope so it stays importable under the
# CI pytest subset. Every heavier collaborator (shows_store, downloads_store,
# queue_store, media_lookup, verification, library_index, config) is imported
# lazily inside the function that needs it.
# =============================================================================

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import db

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# resolved_by values, in ascending trust order. manual is a human decision and
# outranks everything; an exact episode-row match outranks an inherited folder
# mapping; a placed download outranks an inherited mapping; a batch provider
# lookup (parsed title+year) is the weakest and is never allowed to overwrite a
# stronger source on a re-run.
RESOLVED_MANUAL = "manual"
RESOLVED_EPISODE = "episode"
RESOLVED_DOWNLOAD = "download"
RESOLVED_SHOW_FOLDER = "show_folder"
RESOLVED_BATCH = "batch_lookup"

_TRUST: dict[str, int] = {
    RESOLVED_MANUAL: 5,
    RESOLVED_EPISODE: 4,
    RESOLVED_DOWNLOAD: 3,
    RESOLVED_SHOW_FOLDER: 2,
    RESOLVED_BATCH: 1,
}


@dataclass(frozen=True)
class LibraryIdentityRow:
    path: str
    media_type: str
    identity_source: str | None
    external_id: str | None
    show_id: int | None
    season: int | None
    episode: int | None
    canonical_title: str | None
    canonical_year: int | None
    resolved_by: str
    resolved_at: str

    @property
    def is_qualified(self) -> bool:
        """Provider-qualified: both a provider AND an id. A bare external_id is
        meaningless without knowing tmdb vs tvdb vs mal."""
        return bool(self.identity_source) and bool(self.external_id)

    @property
    def is_movie(self) -> bool:
        return self.media_type == "movie"

    @property
    def namespace(self) -> str:
        """movie-vs-episodic namespace. TMDB (and others) reuse the same integer
        across their movie and TV namespaces — movie 550 and show 550 are
        different works — so the id alone is NOT a durable key. Every identity
        key and presence join carries this so a movie can never cross-match a
        show and vice versa."""
        return "movie" if self.is_movie else "ep"

    @property
    def group_key(self) -> tuple:
        """The duplicate-grouping key for an IDENTIFIED file. Leads with the
        "id" discriminator (so it can never collide with an unidentified file's
        string key) and the movie/episodic namespace (so provider-id reuse
        across namespaces never merges a movie into a show). Movies key on the
        id alone; episodes additionally on season/episode so different episodes
        of one show never read as copies of each other."""
        return ("id", self.namespace, self.identity_source,
                str(self.external_id), self.season, self.episode)


_COLS = ("path, media_type, identity_source, external_id, show_id, season, "
         "episode, canonical_title, canonical_year, resolved_by, resolved_at")


def _row(r) -> LibraryIdentityRow:
    return LibraryIdentityRow(
        path=r[0], media_type=r[1], identity_source=r[2], external_id=r[3],
        show_id=r[4], season=r[5], episode=r[6], canonical_title=r[7],
        canonical_year=r[8], resolved_by=r[9], resolved_at=r[10])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_identity (
    path            TEXT PRIMARY KEY,
    media_type      TEXT NOT NULL DEFAULT 'unknown',
    identity_source TEXT,
    external_id     TEXT,
    show_id         INTEGER,
    season          INTEGER,
    episode         INTEGER,
    canonical_title TEXT,
    canonical_year  INTEGER,
    resolved_by     TEXT NOT NULL,
    resolved_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def initialize_library_identity_db() -> None:
    with _DB_LOCK, db.connect() as conn:
        conn.execute(_SCHEMA)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_library_identity_external "
            "ON library_identity (identity_source, external_id)")
        conn.commit()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_identity(path: str) -> LibraryIdentityRow | None:
    initialize_library_identity_db()
    with _DB_LOCK, db.connect() as conn:
        r = conn.execute(
            f"SELECT {_COLS} FROM library_identity WHERE path = ?",
            (path,)).fetchone()
    return _row(r) if r is not None else None


def get_identities(paths: list[str]) -> dict[str, LibraryIdentityRow]:
    """Bulk lookup for find_duplicates — one query, not one per file."""
    if not paths:
        return {}
    initialize_library_identity_db()
    out: dict[str, LibraryIdentityRow] = {}
    with _DB_LOCK, db.connect() as conn:
        # SQLite caps variables at 999; chunk to stay well under it.
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(
                    f"SELECT {_COLS} FROM library_identity "
                    f"WHERE path IN ({marks})", chunk).fetchall():
                out[r[0]] = _row(r)
    return out


def all_identities() -> list[LibraryIdentityRow]:
    initialize_library_identity_db()
    with _DB_LOCK, db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLS} FROM library_identity").fetchall()
    return [_row(r) for r in rows]


def _namespace_clause(movie: bool | None, col: str = "") -> str:
    """SQL fragment filtering the movie/episodic namespace off the stored
    media_type. movie=True -> only movie rows; movie=False -> only episodic
    rows; movie=None -> no namespace filter (back-compat)."""
    if movie is None:
        return ""
    prefix = f"{col}." if col else ""
    return (f" AND {prefix}media_type = 'movie'" if movie
            else f" AND {prefix}media_type != 'movie'")


def find_paths_by_identity(identity_source: str, external_id: str, *,
                           season: int | None = None,
                           season_any: bool = False,
                           movie: bool | None = None) -> list[str]:
    """Paths carrying a given provider identity. `movie` scopes the query to the
    movie or episodic namespace (a TMDB id is reused across both, so this is
    required to keep a movie query from matching a same-id show). With
    season_any (a whole-show want) any season matches; otherwise a specific
    season filters and a movie (season None) matches rows with a NULL season."""
    if not (identity_source and external_id):
        return []
    initialize_library_identity_db()
    q = ("SELECT path FROM library_identity "
         "WHERE identity_source = ? AND external_id = ?")
    q += _namespace_clause(movie)
    params: list = [identity_source, str(external_id)]
    if not season_any:
        if season is None:
            q += " AND season IS NULL"
        else:
            q += " AND season = ?"
            params.append(int(season))
    with _DB_LOCK, db.connect() as conn:
        rows = conn.execute(q, params).fetchall()
    return [r[0] for r in rows]


def identity_present_in_library(identity_source: str, external_id: str, *,
                                season: int | None = None,
                                season_any: bool = False,
                                movie: bool | None = None) -> bool:
    """True when a file with this provider identity is BOTH resolved here AND
    still present in the live library_files index. Joining against
    library_files means a stale identity row (file since deleted) never reports
    a false presence — the index is the source of truth for "is it on disk".
    `movie` scopes to the movie vs episodic namespace (provider ids collide
    across the two) — a movie query never matches a same-id show row."""
    if not (identity_source and external_id):
        return False
    initialize_library_identity_db()
    q = ("SELECT 1 FROM library_identity li "
         "JOIN library_files lf ON li.path = lf.path "
         "WHERE li.identity_source = ? AND li.external_id = ?")
    q += _namespace_clause(movie, "li")
    params: list = [identity_source, str(external_id)]
    if not season_any:
        if season is None:
            q += " AND li.season IS NULL"
        else:
            q += " AND li.season = ?"
            params.append(int(season))
    q += " LIMIT 1"
    try:
        with _DB_LOCK, db.connect() as conn:
            return conn.execute(q, params).fetchone() is not None
    except Exception:
        # library_files may not exist yet in a bare test DB — treat as absent.
        return False


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def set_identity(path: str, *, media_type: str,
                 identity_source: str | None, external_id: str | None,
                 resolved_by: str, show_id: int | None = None,
                 season: int | None = None, episode: int | None = None,
                 canonical_title: str | None = None,
                 canonical_year: int | None = None,
                 only_if_stronger: bool = False) -> bool:
    """Upsert one identity row. With only_if_stronger the write is skipped when
    a row already exists whose resolved_by trust is >= the incoming one — this
    is how the backfill preserves trust order across re-runs (an inherited
    show_folder pass never clobbers an exact episode or a live placement).
    Returns True when a row was written."""
    initialize_library_identity_db()
    with _DB_LOCK, db.connect() as conn:
        if only_if_stronger:
            existing = conn.execute(
                "SELECT resolved_by FROM library_identity WHERE path = ?",
                (path,)).fetchone()
            if existing is not None:
                if _TRUST.get(existing[0], 0) >= _TRUST.get(resolved_by, 0):
                    return False
        conn.execute(
            """
            INSERT INTO library_identity
                (path, media_type, identity_source, external_id, show_id,
                 season, episode, canonical_title, canonical_year, resolved_by,
                 resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                media_type = excluded.media_type,
                identity_source = excluded.identity_source,
                external_id = excluded.external_id,
                show_id = excluded.show_id,
                season = excluded.season,
                episode = excluded.episode,
                canonical_title = excluded.canonical_title,
                canonical_year = excluded.canonical_year,
                resolved_by = excluded.resolved_by,
                resolved_at = CURRENT_TIMESTAMP
            """,
            (path, media_type or "unknown", identity_source,
             (str(external_id) if external_id is not None else None), show_id,
             season, episode, canonical_title, canonical_year, resolved_by))
        conn.commit()
    return True


def remove_identities(paths: list[str]) -> int:
    """Drop identity rows for deleted/removed files (called from
    library_index.remove_from_index and the refresh delta)."""
    if not paths:
        return 0
    initialize_library_identity_db()
    with _DB_LOCK, db.connect() as conn:
        cur = conn.executemany(
            "DELETE FROM library_identity WHERE path = ?",
            [(p,) for p in paths])
        conn.commit()
        return cur.rowcount or 0


def prune_orphans() -> int:
    """Delete every identity row whose path is no longer in the live
    library_files index. Called at the tail of a full rebuild / delta refresh
    (after the index reflects the current disk) so identities and files stay in
    lockstep. Files under a currently-unavailable root are PRESERVED in
    library_files by the index, so their identities survive here too — this
    never wipes an offline drive's identities."""
    initialize_library_identity_db()
    try:
        with _DB_LOCK, db.connect() as conn:
            cur = conn.execute(
                "DELETE FROM library_identity WHERE path NOT IN "
                "(SELECT path FROM library_files)")
            conn.commit()
            return cur.rowcount or 0
    except Exception:
        logger.debug("prune_orphans skipped (library_files unavailable).",
                     exc_info=True)
        return 0


def placed_file_contradicts(path: str, *, media_type: str,
                            identity_source, external_id,
                            canonical_title, canonical_year) -> bool:
    """True when a placed movie file's own name contradicts the request it was
    placed for. The July wrong-movie grabs ('The Last House on the Left (2009)'
    for 'The Last House' 2026, 'The Boss Baby Christmas Bonus (2022)' for 'The
    Boss Baby' 2017) were stamped with the request's identity, so the identity
    join kept closing the request while the placement check kept reopening it.
    This is the same test maintenance._placement_still_valid runs, so the two
    can never disagree about one file. Movies only; best-effort."""
    if media_type != "movie":
        return False
    try:
        import media_identity
        import verification
        want = media_identity.MediaIdentity(
            media_type="movie", identity_source=identity_source,
            external_id=(str(external_id) if external_id is not None else None),
            canonical_title=canonical_title, canonical_year=canonical_year)
        parsed = verification.parse_file(Path(path))
        return not media_identity.compare_media_identity(want, parsed).ok
    except Exception:
        return False


def purge_contradicting_placements(identity_source: str | None, external_id) -> int:
    """Drop download-stamped movie rows for this identity whose file name
    contradicts it. Called when the reconcile reopens a request, so a wrong
    file stops answering 'present' for it. Returns how many rows went."""
    if not (identity_source and external_id):
        return 0
    initialize_library_identity_db()
    with _DB_LOCK, db.connect() as conn:
        rows = conn.execute(
            "SELECT path, canonical_title, canonical_year FROM library_identity "
            "WHERE identity_source = ? AND external_id = ? AND resolved_by = ?"
            + _namespace_clause(True),
            (identity_source, str(external_id), RESOLVED_DOWNLOAD)).fetchall()
    bad = [r[0] for r in rows if placed_file_contradicts(
        r[0], media_type="movie", identity_source=identity_source,
        external_id=external_id, canonical_title=r[1], canonical_year=r[2])]
    return remove_identities(bad)


# ---------------------------------------------------------------------------
# Placement hook — called from download_manager._on_download_placed
# ---------------------------------------------------------------------------

def record_placement(row, moved_final_paths: list[str]) -> int:
    """Write identity rows for the files a download just placed. The request
    the download is linked to carries the resolved identity; each placed file
    inherits it, with per-file season/episode from the download_files
    provenance where available (a season pack places several episodes).

    resolved_by = 'download'. Only writes when the request carries a
    provider-qualified identity — an 'other'/unqualified request places no
    identity row (there is nothing durable to key on). Best-effort: never
    raises into the move path."""
    if not moved_final_paths or getattr(row, "request_id", None) is None:
        return 0
    try:
        import downloads_store
        import queue_store
        req = queue_store.get_request(row.request_id)
        if req is None or not (req.identity_source and req.external_id):
            return 0
        # Map final_path -> parsed season/episode from the per-file provenance.
        by_final: dict[str, tuple[int | None, int | None]] = {}
        for f in downloads_store.list_download_files(row.download_id):
            if f.final_path:
                by_final[f.final_path] = (f.parsed_season, f.parsed_episode)
        written = 0
        for path in moved_final_paths:
            p_season, p_episode = by_final.get(path, (None, None))
            season = p_season if p_season is not None else getattr(row, "season", None)
            episode = p_episode if p_episode is not None else getattr(row, "episode", None)
            if req.media_type == "movie":
                season, episode = None, None
            if placed_file_contradicts(
                    path, media_type=req.media_type,
                    identity_source=req.identity_source,
                    external_id=req.external_id,
                    canonical_title=req.resolved_title or req.content,
                    canonical_year=req.canonical_year):
                logger.warning(
                    "Download #%s placed %s, which does not match request #%s "
                    "(%s); not tagging it with that identity.",
                    getattr(row, "download_id", "?"), path, req.request_id,
                    req.resolved_title or req.content)
                continue
            set_identity(
                path, media_type=req.media_type,
                identity_source=req.identity_source, external_id=req.external_id,
                resolved_by=RESOLVED_DOWNLOAD,
                show_id=getattr(row, "show_id", None),
                season=season, episode=episode,
                canonical_title=req.resolved_title or req.content,
                canonical_year=req.canonical_year)
            written += 1
        return written
    except Exception:
        logger.exception("record_placement failed for download #%s",
                         getattr(row, "download_id", "?"))
        return 0


# ---------------------------------------------------------------------------
# Manual resolve — called from the dupes UI "Resolve" action (Task J item 4)
# ---------------------------------------------------------------------------

def resolve_identity_manual(path: str, *, media_type: str,
                            identity_source: str, external_id: str,
                            canonical_title: str | None = None,
                            canonical_year: int | None = None,
                            season: int | None = None,
                            episode: int | None = None,
                            show_id: int | None = None) -> None:
    """A human pinned this exact file to a provider identity. Highest trust —
    overwrites any inherited/batch guess."""
    set_identity(path, media_type=media_type, identity_source=identity_source,
                 external_id=external_id, resolved_by=RESOLVED_MANUAL,
                 show_id=show_id, season=season, episode=episode,
                 canonical_title=canonical_title, canonical_year=canonical_year)


# ---------------------------------------------------------------------------
# Coverage report
# ---------------------------------------------------------------------------

def coverage_report() -> dict:
    """How much of the live index is identified, split by provenance. The
    orchestrator prints this after the live backfill."""
    initialize_library_identity_db()
    out = {"identified": 0, "by_source": {}, "indexed": 0, "unidentified": 0}
    with _DB_LOCK, db.connect() as conn:
        out["identified"] = int(conn.execute(
            "SELECT COUNT(*) FROM library_identity").fetchone()[0])
        for resolved_by, n in conn.execute(
                "SELECT resolved_by, COUNT(*) FROM library_identity "
                "GROUP BY resolved_by").fetchall():
            out["by_source"][resolved_by] = int(n)
        try:
            out["indexed"] = int(conn.execute(
                "SELECT COUNT(*) FROM library_files").fetchone()[0])
        except Exception:
            out["indexed"] = 0
    out["unidentified"] = max(0, out["indexed"] - out["identified"])
    return out


# ---------------------------------------------------------------------------
# Backfill (registry job) — episodes -> show_folder -> download -> batch lookup
# ---------------------------------------------------------------------------

def _indexed_paths() -> list[tuple[str, str]]:
    """(path, name) for every file in the live index. Falls back to empty when
    the index table isn't there yet."""
    try:
        with _DB_LOCK, db.connect() as conn:
            return [(r[0], r[1]) for r in conn.execute(
                "SELECT path, name FROM library_files").fetchall()]
    except Exception:
        return []


def _rec(path: str, *, media_type: str, identity_source, external_id,
         resolved_by: str, show_id=None, season=None, episode=None,
         canonical_title=None, canonical_year=None) -> dict:
    return {"path": path, "media_type": media_type or "unknown",
            "identity_source": identity_source, "external_id": external_id,
            "show_id": show_id, "season": season, "episode": episode,
            "canonical_title": canonical_title, "canonical_year": canonical_year,
            "resolved_by": resolved_by}


def _existing_identity_paths() -> set[str]:
    """Every path that already has an identity row — one query, so the network
    phase's per-file skip test never opens a connection per file."""
    initialize_library_identity_db()
    with _DB_LOCK, db.connect() as conn:
        return {r[0] for r in conn.execute(
            "SELECT path FROM library_identity").fetchall()}


def _bulk_upsert(records: list[dict]) -> int:
    """Trust-gated batch write of a phase's identity records through ONE
    connection (kills the per-row connect+DDL pattern — a 34k-file backfill was
    opening ~60k connections). Collapses duplicate paths within the batch to the
    strongest resolved_by, drops any whose existing DB row is already >= trust,
    then executemany-upserts the survivors. Returns rows written."""
    if not records:
        return 0
    initialize_library_identity_db()
    # Strongest record per path within this batch.
    by_path: dict[str, dict] = {}
    for r in records:
        cur = by_path.get(r["path"])
        if (cur is None or _TRUST.get(r["resolved_by"], 0)
                > _TRUST.get(cur["resolved_by"], 0)):
            by_path[r["path"]] = r
    paths = list(by_path)
    with _DB_LOCK, db.connect() as conn:
        existing: dict[str, str] = {}
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for p, rb in conn.execute(
                    "SELECT path, resolved_by FROM library_identity "
                    f"WHERE path IN ({marks})", chunk).fetchall():
                existing[p] = rb
        rows: list[tuple] = []
        for p, r in by_path.items():
            ex = existing.get(p)
            if ex is not None and _TRUST.get(ex, 0) >= _TRUST.get(r["resolved_by"], 0):
                continue
            rows.append((
                p, r["media_type"] or "unknown", r["identity_source"],
                (str(r["external_id"]) if r["external_id"] is not None else None),
                r["show_id"], r["season"], r["episode"], r["canonical_title"],
                r["canonical_year"], r["resolved_by"]))
        if not rows:
            return 0
        conn.executemany(
            """
            INSERT INTO library_identity
                (path, media_type, identity_source, external_id, show_id,
                 season, episode, canonical_title, canonical_year, resolved_by,
                 resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(path) DO UPDATE SET
                media_type = excluded.media_type,
                identity_source = excluded.identity_source,
                external_id = excluded.external_id,
                show_id = excluded.show_id,
                season = excluded.season,
                episode = excluded.episode,
                canonical_title = excluded.canonical_title,
                canonical_year = excluded.canonical_year,
                resolved_by = excluded.resolved_by,
                resolved_at = CURRENT_TIMESTAMP
            """,
            rows)
        conn.commit()
        return len(rows)


def _episode_records(progress, shows) -> list[dict]:
    """Phase 1 (exact): every tracked episode with a file on disk."""
    import shows_store
    out: list[dict] = []
    for i, show in enumerate(shows):
        if not (show.source and show.external_id):
            continue
        for ep in shows_store.list_episodes(show.show_id):
            if not (ep.has_file and ep.file_path):
                continue
            out.append(_rec(
                ep.file_path, media_type=show.media_type,
                identity_source=show.source, external_id=show.external_id,
                resolved_by=RESOLVED_EPISODE, show_id=show.show_id,
                season=ep.season, episode=ep.episode,
                canonical_title=show.title, canonical_year=show.year))
        progress(current=i + 1, total=len(shows),
                 phase="Backfill: episode rows")
    return out


def _show_folder_records(progress, indexed: list[tuple[str, str]],
                         shows) -> list[dict]:
    """Phase 2 (inherited): other files under a mapped show folder inherit that
    show's identity. Season/episode are parsed from the filename (SxxExx, then a
    'Season N' ancestor + 'Ep NN' token) — best-effort, marked resolved_by
    'show_folder' so no consumer auto-acts on it without showing provenance. An
    episodic file whose episode number can't be parsed still gets a row (for
    presence), but find_duplicates keeps it string-keyed so distinct episodes
    never collapse."""
    import os

    import maintenance
    out: list[dict] = []
    # folder -> show, longest folder first so the most specific mapping wins.
    folder_show: list[tuple[str, object]] = []
    for show in shows:
        if not (show.source and show.external_id):
            continue
        for folder in show.folders:
            folder_show.append((os.path.normcase(os.path.normpath(folder)), show))
    folder_show.sort(key=lambda t: -len(t[0]))
    if not folder_show:
        return out

    for i, (path, name) in enumerate(indexed):
        norm = os.path.normcase(os.path.normpath(path))
        show = None
        for folder, s in folder_show:
            if norm == folder or norm.startswith(folder + os.sep):
                show = s
                break
        if show is None:
            continue
        ep = maintenance._parse_episode(name)
        season = ep[0] if ep else None
        episode = ep[1] if ep else None
        # A letter-suffixed episode ("S07E05sp") is a distinct special the
        # integer episode column can't represent — leave episode unset so
        # the row still serves presence checks but never identity-groups
        # in find_duplicates (which keys the suffix via its string path).
        if ep is not None:
            m = maintenance._DUP_EP_SUFFIX_RE.search(name)
            if m and int(m.group(1)) == ep[1]:
                episode = None
        if season is None:
            for part in reversed(os.path.dirname(path).split(os.sep)):
                m = maintenance._DUP_SEASON_DIR_RE.search(part)
                if m:
                    season = int(m.group(1))
                    break
        if episode is None:
            m = maintenance._DUP_EP_WORD_RE.search(name)
            if m:
                episode = int(m.group(1))
        out.append(_rec(
            path, media_type=show.media_type, identity_source=show.source,
            external_id=show.external_id, resolved_by=RESOLVED_SHOW_FOLDER,
            show_id=show.show_id, season=season, episode=episode,
            canonical_title=show.title, canonical_year=show.year))
        if i % 500 == 0:
            progress(current=i + 1, total=len(indexed),
                     phase="Backfill: show-folder inheritance")
    return out


def _download_records(progress) -> list[dict]:
    """Phase 3 (movies): a placed movie download's verified file inherits the
    identity of the request it fulfilled. Covers movies moved before the
    placement hook existed."""
    import downloads_store
    import queue_store
    out: list[dict] = []
    reqs = queue_store.list_requests(status="all", limit=5000)
    movie_reqs = [r for r in reqs
                  if r.media_type == "movie" and r.identity_source and r.external_id]
    for i, req in enumerate(movie_reqs):
        for dl in downloads_store.downloads_for_request(req.request_id):
            for f in downloads_store.list_download_files(dl.download_id):
                if f.verification_state not in ("verified", "duplicate"):
                    continue
                if not f.final_path:
                    continue
                if placed_file_contradicts(
                        f.final_path, media_type="movie",
                        identity_source=req.identity_source,
                        external_id=req.external_id,
                        canonical_title=req.resolved_title or req.content,
                        canonical_year=req.canonical_year):
                    continue
                out.append(_rec(
                    f.final_path, media_type="movie",
                    identity_source=req.identity_source,
                    external_id=req.external_id, resolved_by=RESOLVED_DOWNLOAD,
                    canonical_title=req.resolved_title or req.content,
                    canonical_year=req.canonical_year))
        if movie_reqs:
            progress(current=i + 1, total=len(movie_reqs),
                     phase="Backfill: moved-download history")
    return out


def _backfill_batch_lookup(progress, cancel_check, indexed: list[tuple[str, str]],
                           *, batch_limit: int | None,
                           throttle: float) -> int:
    """Phase 4 (network, the remainder): movie files still without any identity
    are looked up on TMDB by parsed title+year. Resumable (files that already
    have a row are skipped via a single preloaded set, so a capped run picks up
    where it left off), rate-limited (throttle between calls), and
    cooperative-cancellable. Writes resolved_by 'batch_lookup' in small
    committed bursts — never a row for an unresolved title, never a per-row
    connection."""
    import maintenance
    import media_lookup
    import verification
    from pathlib import Path
    written = 0
    looked_up = 0
    resolved = _existing_identity_paths()  # one query, not one-per-file
    pending: list[dict] = []
    # Only movie-typed files: TV/anime is covered by episodes + show_folder.
    candidates = [(p, n) for (p, n) in indexed
                  if p not in resolved
                  and maintenance.media_type_for_path(p) == "movie"]
    total = len(candidates)

    def _flush() -> None:
        nonlocal pending, written
        if pending:
            written += _bulk_upsert(pending)
            pending = []

    for i, (path, name) in enumerate(candidates):
        if cancel_check():
            _flush()
            from maint_jobs import JobCancelled
            raise JobCancelled(items_done=written)
        if batch_limit is not None and looked_up >= batch_limit:
            break
        parsed = verification.parse_file(Path(name))
        title = (parsed.parsed_title or "").strip()
        if not title:
            continue
        looked_up += 1
        try:
            results = media_lookup.search_tmdb_movies(title, parsed.year, limit=1)
        except Exception:
            results = []
        if throttle:
            time.sleep(throttle)
        if not results:
            continue
        match = results[0]
        # Guard: only accept a genuinely-similar title so a bad parse can't
        # stamp a wrong id onto a file.
        if media_lookup.best_title_similarity(title, match) < 0.6:
            continue
        pending.append(_rec(
            path, media_type="movie", identity_source=match.source,
            external_id=match.external_id, resolved_by=RESOLVED_BATCH,
            canonical_title=match.title, canonical_year=match.year))
        if len(pending) >= 50:
            _flush()
        if looked_up % 20 == 0:
            progress(current=i + 1, total=total,
                     phase="Backfill: provider lookup (remainder)")
    _flush()
    return written


def backfill_identities(progress=None, cancel_check=None, *,
                        allow_network: bool = True,
                        batch_limit: int | None = None,
                        throttle: float = 0.3) -> dict:
    """Full backfill in trust order. Safe to call from the maint job registry
    (signature (progress, cancel_check)) or directly. Resumable and idempotent:
    each phase only writes an identity a re-run would strengthen, and the
    network phase skips files that already have a row.

    Writes are batched through one connection per phase (executemany), not one
    connection per file. After the phases, prune_orphans() drops any identity
    whose path is no longer in the live index — so a stale episode file_path
    (never validated against the index during the episode phase) can't leave a
    phantom identity behind.

    allow_network=False stops before the batch provider lookup (the DB-only
    phases 1-3), which is what tests exercise without mocking TMDB."""
    if progress is None:
        def progress(*_a, **_k):  # noqa: E731 - no-op sink for direct calls
            return None
    if cancel_check is None:
        def cancel_check() -> bool:
            return False

    initialize_library_identity_db()
    import shows_store
    shows = shows_store.list_shows()
    indexed = _indexed_paths()

    summary = {"episodes": 0, "show_folder": 0, "download": 0,
               "batch_lookup": 0, "indexed": len(indexed), "pruned": 0}
    summary["episodes"] = _bulk_upsert(_episode_records(progress, shows))
    if cancel_check():
        from maint_jobs import JobCancelled
        raise JobCancelled(items_done=summary["episodes"])
    summary["show_folder"] = _bulk_upsert(
        _show_folder_records(progress, indexed, shows))
    if cancel_check():
        from maint_jobs import JobCancelled
        raise JobCancelled(items_done=summary["show_folder"])
    summary["download"] = _bulk_upsert(_download_records(progress))
    if allow_network:
        summary["batch_lookup"] = _backfill_batch_lookup(
            progress, cancel_check, indexed, batch_limit=batch_limit,
            throttle=throttle)
    # Drop identities whose file isn't in the index (e.g. an episode row written
    # for a stale file_path). No-op when the index isn't populated.
    summary["pruned"] = prune_orphans()
    summary["coverage"] = coverage_report()
    return summary


def backfill_paths(paths: list[str]) -> dict:
    """Offline (no-network) identity resolution for a SPECIFIC set of newly-seen
    files — the folder watcher's per-batch pass (Task K item 2). Runs the same
    episode / show_folder / download phase builders the full backfill uses and
    writes through the same trust-gated _bulk_upsert, so provenance and trust
    order are identical; it just keys to `paths` instead of the whole library
    and skips the network provider lookup (that stays a full-backfill concern).

    The episode and moved-download phases enumerate shows/requests (bounded,
    fast) and are filtered down to the target paths here; the show-folder phase
    is fed only the target paths directly. Idempotent — re-running a settled
    subtree only ever strengthens an identity, never downgrades one."""
    import os

    if not paths:
        return {"episodes": 0, "show_folder": 0, "download": 0}
    initialize_library_identity_db()
    import shows_store
    shows = shows_store.list_shows()

    want = {os.path.normcase(os.path.normpath(p)) for p in paths}

    def _keep(records: list[dict]) -> list[dict]:
        return [r for r in records
                if os.path.normcase(os.path.normpath(r["path"])) in want]

    def _noop(*_a, **_k):
        return None

    indexed = [(p, os.path.basename(p)) for p in paths]
    summary = {
        "episodes": _bulk_upsert(_keep(_episode_records(_noop, shows))),
        "show_folder": _bulk_upsert(_show_folder_records(_noop, indexed, shows)),
        "download": _bulk_upsert(_keep(_download_records(_noop))),
    }
    return summary
