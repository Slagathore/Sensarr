# =============================================================================
# movie_migration.py  —  Task D2 item 7 (RESOLVED DECISION 7)
# =============================================================================
# Existing FLAT movies are never renamed automatically. This is the ONLY place
# that touches pre-existing user media, and it does so deliberately, in stages:
#
#   scan  ->  resolve/verify each identity  ->  dry-run old/new plan  ->
#   explicit confirm  ->  operation journal  ->  reversible/resumable execution
#
# Ambiguous or unmatched movies are SKIPPED (never guessed). The dry-run plan
# changes nothing on disk; only begin_run + execute_run move files, and every
# move is journalled so an interrupted run resumes safely and a completed run
# can be reverted.
#
# The engine is Tk-free and fully testable below the UI: scan roots and the
# identity resolver are both injectable, so a test drives the whole flow on tmp
# roots with a stub resolver.
# =============================================================================

from __future__ import annotations

import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import db
import torrent_routing

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = set(torrent_routing.VIDEO_EXTENSIONS)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS movie_migration_journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    old_path   TEXT NOT NULL,
    new_path   TEXT NOT NULL,
    title      TEXT,
    year       INTEGER,
    tmdb_id    TEXT,
    state      TEXT NOT NULL DEFAULT 'planned',  -- planned|done|failed|reverted
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
)
"""

_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


@dataclass(frozen=True)
class MoviePlanItem:
    """One planned rename+folder move (dry-run row)."""
    old_path: str
    new_path: str
    title: str | None
    year: int | None
    tmdb_id: str | None

    @property
    def has_tmdb(self) -> bool:
        return bool(self.tmdb_id)


def _init(conn) -> None:
    conn.execute(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Scan + plan (dry-run — never touches disk)
# ---------------------------------------------------------------------------

def scan_flat_movies(roots) -> list[Path]:
    """Video files sitting FLAT directly under a movie root (not already inside
    a per-movie subfolder). These are the migration candidates."""
    out: list[Path] = []
    for root in roots:
        rp = Path(root)
        if not rp.is_dir():
            continue
        try:
            entries = list(rp.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
                out.append(entry)
    return out


def parse_title_year(path: Path) -> tuple[str | None, int | None]:
    """Best-effort title + year from a flat movie filename stem."""
    stem = path.stem
    m = _YEAR_RE.search(stem)
    year = int(m.group(1)) if m else None
    title = stem[:m.start()] if m else stem
    title = re.sub(r"[._]+", " ", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -")
    return (title or None), year


def plan_migration(roots, *, resolver=None) -> tuple[list[MoviePlanItem], list[Path]]:
    """Dry-run. Returns (planned items, skipped files). Changes NOTHING on disk.

    `resolver(title, year) -> (canonical_title, canonical_year, tmdb_id) | None`.
    A None (or empty-title) result marks the file AMBIGUOUS and skips it — the
    migration never guesses an identity. The default resolver echoes the parsed
    title/year with no tmdb id (offline-safe); production passes a TMDB-backed
    resolver."""
    items: list[MoviePlanItem] = []
    skipped: list[Path] = []
    for f in scan_flat_movies(roots):
        title, year = parse_title_year(f)
        resolved = resolver(title, year) if resolver else (title, year, None)
        if not resolved or not resolved[0]:
            skipped.append(f)
            continue
        ctitle, cyear, tmdb = resolved
        tmdb = str(tmdb).strip() if tmdb not in (None, "") else None
        folder = torrent_routing.movie_folder_name(ctitle, cyear, tmdb)
        stem = torrent_routing.movie_display_base(ctitle, cyear)
        new_path = f.parent / folder / f"{stem}{f.suffix}"
        if new_path == f:
            continue  # already correctly placed
        items.append(MoviePlanItem(str(f), str(new_path), ctitle, cyear, tmdb))
    return items, skipped


def tmdb_resolver(title: str | None, year: int | None):
    """Production identity resolver: TMDB search VERIFIED before use.

    Returns (canonical_title, canonical_year, tmdb_id) only when the best TMDB
    candidate matches with high confidence; anything ambiguous returns None so
    the file is skipped for manual resolution — the migration never guesses.
    A filename with no year needs a near-exact title match (sequels and remakes
    share titles), and a year in the filename must agree within one year."""
    if not title:
        return None
    import media_identity
    import media_lookup
    try:
        results = media_lookup.search_tmdb_movies(title, year)
    except Exception as exc:  # network trouble reads as ambiguous, never a guess
        logger.warning("TMDB lookup failed for %r: %s", title, exc)
        return None
    if not results:
        return None
    best = max(results, key=lambda r: media_lookup.best_title_similarity(title, r))
    sim = media_lookup.best_title_similarity(title, best)
    threshold = 0.85 if year is not None else 0.95
    if sim < threshold:
        return None
    if media_identity.sequel_mismatch(best.title, title):
        return None
    if year is not None and best.year is not None and abs(best.year - year) > 1:
        return None
    if best.year is None and year is None:
        return None  # not enough evidence to verify the identity
    return (best.title, best.year if best.year is not None else year,
            str(best.external_id))


# ---------------------------------------------------------------------------
# Journal-backed, reversible/resumable execution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JournalOp:
    op_id: int
    run_id: str
    old_path: str
    new_path: str
    state: str
    detail: str | None


def _row_to_op(r) -> JournalOp:
    return JournalOp(r[0], r[1], r[2], r[3], r[4], r[5])


def begin_run(items: list[MoviePlanItem], *, run_id: str | None = None) -> str:
    """Record a confirmed plan as a journal run (state 'planned'). Still moves
    nothing — execute_run does the work. Returns the run id."""
    run_id = run_id or uuid.uuid4().hex
    with db.connect() as conn:
        _init(conn)
        for it in items:
            conn.execute(
                "INSERT INTO movie_migration_journal"
                " (run_id, old_path, new_path, title, year, tmdb_id, state)"
                " VALUES (?, ?, ?, ?, ?, ?, 'planned')",
                (run_id, it.old_path, it.new_path, it.title, it.year, it.tmdb_id))
        conn.commit()
    return run_id


def list_ops(run_id: str, *, state: str | None = None) -> list[JournalOp]:
    with db.connect() as conn:
        _init(conn)
        if state is None:
            rows = conn.execute(
                "SELECT id, run_id, old_path, new_path, state, detail"
                " FROM movie_migration_journal WHERE run_id = ? ORDER BY id",
                (run_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, run_id, old_path, new_path, state, detail"
                " FROM movie_migration_journal WHERE run_id = ? AND state = ?"
                " ORDER BY id", (run_id, state)).fetchall()
    return [_row_to_op(r) for r in rows]


def _set_state(op_id: int, state: str, detail: str | None = None) -> None:
    with db.connect() as conn:
        _init(conn)
        conn.execute(
            "UPDATE movie_migration_journal SET state = ?, detail = ?,"
            " updated_at = ? WHERE id = ?", (state, detail, _now(), op_id))
        conn.commit()


def _perform_move(old: Path, new: Path) -> None:
    """Reversible move: old -> new. Never overwrites an existing DIFFERENT file
    at the destination (collisions are surfaced as failures, never clobbered)."""
    if not old.exists():
        # Crash between move and journal update: the file is already at new.
        if new.exists():
            return
        raise FileNotFoundError(f"source vanished: {old}")
    if new.exists():
        raise FileExistsError(f"destination already exists: {new}")
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    # Task F: quality labels follow the file through the ONE central helper.
    try:
        import media_quality
        media_quality.update_file_path(str(old), str(new))
    except Exception:
        logger.exception("media_quality file_path update failed: %s", old)


def execute_run(run_id: str, *, on_progress=None) -> dict:
    """Execute (or RESUME) a journalled run. Idempotent: ops already 'done' are
    skipped, so an interrupted run picks up exactly where it stopped without
    re-moving completed files. Returns a summary dict."""
    ops = list_ops(run_id)
    moved = failed = already = 0
    total = len(ops)
    for i, op in enumerate(ops):
        if op.state == "done":
            already += 1
            continue
        try:
            _perform_move(Path(op.old_path), Path(op.new_path))
            _set_state(op.op_id, "done")
            moved += 1
        except Exception as exc:  # collision / missing / permission — never fatal
            logger.warning("Migration op %s failed: %s", op.op_id, exc)
            _set_state(op.op_id, "failed", str(exc))
            failed += 1
        if on_progress is not None:
            on_progress(i + 1, total, op)
    return {"moved": moved, "failed": failed, "already_done": already,
            "total": total}


def resume_run(run_id: str, *, on_progress=None) -> dict:
    """Alias for execute_run — resuming is just re-running (done ops skip)."""
    return execute_run(run_id, on_progress=on_progress)


def revert_run(run_id: str) -> dict:
    """Undo a completed run: move each 'done' file back new -> old. Reversibility
    is the whole point of the journal (RESOLVED DECISION 7)."""
    reverted = failed = 0
    for op in list_ops(run_id, state="done"):
        old, new = Path(op.old_path), Path(op.new_path)
        try:
            _perform_move(new, old)
            _set_state(op.op_id, "reverted")
            # Tidy the now-empty per-movie folder we created.
            try:
                if new.parent.is_dir() and not any(new.parent.iterdir()):
                    new.parent.rmdir()
            except OSError:
                pass
            reverted += 1
        except Exception as exc:
            logger.warning("Migration revert %s failed: %s", op.op_id, exc)
            _set_state(op.op_id, "failed", f"revert failed: {exc}")
            failed += 1
    return {"reverted": reverted, "failed": failed}
