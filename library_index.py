import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import config
import db

_DB_LOCK = threading.Lock()


@dataclass(frozen=True)
class LibraryEntry:
    name: str
    path: str
    root_path: str
    size_bytes: int
    modified_at: float


@dataclass(frozen=True)
class LibraryIndexSummary:
    indexed_files: int
    configured_paths: list[str]
    missing_paths: list[str]


@dataclass(frozen=True)
class ReindexResult:
    indexed_files: int
    scanned_roots: list[str]
    missing_roots: list[str]


@dataclass(frozen=True)
class RootLibraryMetrics:
    root_path: str
    file_count: int
    total_size_bytes: int
    recent_7d: int
    recent_30d: int


@dataclass(frozen=True)
class LibraryMetrics:
    total_files: int
    total_size_bytes: int
    recent_7d: int
    recent_30d: int
    roots: list[RootLibraryMetrics]
    missing_paths: list[str]


def _db_path() -> Path:
    path = Path(config.APP_DB_PATH)
    if path.is_absolute():
        return path
    return config.APP_DIR / path


def _configured_library_paths() -> tuple[list[Path], list[str]]:
    valid: list[Path] = []
    missing: list[str] = []

    for raw_path in config.PLEX_LIBRARY_PATHS:
        path = Path(raw_path).expanduser()
        if path.is_dir():
            valid.append(path)
        else:
            missing.append(str(path))

    return valid, missing


def initialize_library_index_db() -> None:
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with _DB_LOCK, db.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS library_files (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                search_name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at REAL NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_library_files_search_name
            ON library_files (search_name)
            """
        )
        # Permanent ledger of everything appearing/disappearing on disk —
        # "my Futurama vanished and I have no idea when or why" insurance.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_events (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                event  TEXT NOT NULL,      -- added|removed|changed|renamed|replaced
                path   TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_events_path ON file_events (path)"
        )
        conn.commit()


def log_file_event(event: str, path: str, detail: str = "") -> None:
    """Append one ledger row. Called by every code path that adds, moves,
    renames, replaces, or deletes a library file — plus the index refresh,
    which catches changes made OUTSIDE the app."""
    initialize_library_index_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.execute(
            "INSERT INTO file_events (event, path, detail) VALUES (?, ?, ?)",
            (event, path, detail),
        )
        conn.commit()


@dataclass(frozen=True)
class FileEvent:
    at: str
    event: str
    path: str
    detail: str


def list_file_events(limit: int = 500) -> list[FileEvent]:
    initialize_library_index_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        rows = conn.execute(
            "SELECT at, event, path, COALESCE(detail, '') FROM file_events"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [FileEvent(*row) for row in rows]


def list_missing_files(limit: int = 500) -> list[FileEvent]:
    """Files that USED to be indexed but are gone — excluding ones the
    ledger knows were renamed or replaced, and ones that later came back."""
    initialize_library_index_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT e.at, e.event, e.path, COALESCE(e.detail, '')
            FROM file_events e
            WHERE e.event = 'removed'
              AND e.path NOT IN (SELECT path FROM library_files)
              AND e.id = (SELECT MAX(id) FROM file_events e2 WHERE e2.path = e.path)
            ORDER BY e.id DESC LIMIT ?
            """, (limit,)).fetchall()
    return [FileEvent(*row) for row in rows]


def _diff_and_log_events(conn, old: dict[str, tuple[str, int]],
                         new: dict[str, tuple[str, int]]) -> None:
    """Write ledger rows for an index diff, pairing renames/replacements so
    they don't read as scary deletions.

    - rename: same size, same folder OR same filename elsewhere
    - replaced: same folder + same episode/name stem prefix, different size
    - everything else: added / removed (removed = 'outside the app' unless
      an app code path already logged its own event for that path)
    """
    removed = {p: v for p, v in old.items() if p not in new}
    added = {p: v for p, v in new.items() if p not in old}
    changed = [p for p in new if p in old and old[p][1] != new[p][1]]

    consumed_added: set[str] = set()
    for r_path, (r_name, r_size) in list(removed.items()):
        match = None
        kind = ""
        r_dir = str(Path(r_path).parent)
        for a_path, (a_name, a_size) in added.items():
            if a_path in consumed_added:
                continue
            a_dir = str(Path(a_path).parent)
            if a_size == r_size and (a_dir == r_dir or a_name == r_name):
                match, kind = a_path, "renamed"
                break
            if a_dir == r_dir and Path(a_name).stem[:12].casefold() == Path(r_name).stem[:12].casefold():
                match, kind = a_path, "replaced"
                break
        if match:
            consumed_added.add(match)
            conn.execute(
                "INSERT INTO file_events (event, path, detail) VALUES (?, ?, ?)",
                (kind, r_path, f"→ {match}"))
            del removed[r_path]

    for p, (_n, size) in removed.items():
        already = conn.execute(
            "SELECT event FROM file_events WHERE path = ? ORDER BY id DESC LIMIT 1",
            (p,)).fetchone()
        if already and already[0] in ("removed", "replaced"):
            continue  # an app action already explained this one
        conn.execute(
            "INSERT INTO file_events (event, path, detail) VALUES ('removed', ?, ?)",
            (p, f"disappeared from disk ({_format_bytes(size)}) — deleted outside "
                "the app, or before this ledger could see why"))
    for p, (_n, size) in added.items():
        if p in consumed_added:
            continue
        conn.execute(
            "INSERT INTO file_events (event, path, detail) VALUES ('added', ?, ?)",
            (p, f"new on disk ({_format_bytes(size)})"))
    for p in changed:
        conn.execute(
            "INSERT INTO file_events (event, path, detail) VALUES ('changed', ?, ?)",
            (p, f"size {_format_bytes(old[p][1])} → {_format_bytes(new[p][1])}"))


def rebuild_library_index() -> ReindexResult:
    initialize_library_index_db()
    valid_paths, missing_paths = _configured_library_paths()
    extensions = set(config.LIBRARY_INDEX_EXTENSIONS)

    with _DB_LOCK, db.connect(_db_path()) as conn:
        old_snapshot = {
            row[0]: (row[1], int(row[2])) for row in conn.execute(
                "SELECT path, name, size_bytes FROM library_files")
        }
        conn.execute("DELETE FROM library_files")

        batch: list[tuple[str, str, str, str, int, float]] = []
        indexed_count = 0

        for root_path in valid_paths:
            for current_root, _dirs, files in os.walk(root_path):
                for name in files:
                    suffix = Path(name).suffix.lower()
                    if extensions and suffix not in extensions:
                        continue

                    file_path = Path(current_root) / name
                    try:
                        stat = file_path.stat()
                    except OSError:
                        continue

                    batch.append(
                        (
                            str(file_path),
                            name,
                            str(root_path),
                            name.casefold(),
                            int(stat.st_size),
                            float(stat.st_mtime),
                        )
                    )
                    indexed_count += 1

                    if len(batch) >= 500:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO library_files
                            (path, name, root_path, search_name, size_bytes, modified_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            batch,
                        )
                        batch.clear()

        if batch:
            conn.executemany(
                """
                INSERT OR REPLACE INTO library_files
                (path, name, root_path, search_name, size_bytes, modified_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                batch,
            )

        new_snapshot = {
            row[0]: (row[1], int(row[2])) for row in conn.execute(
                "SELECT path, name, size_bytes FROM library_files")
        }
        if old_snapshot:  # first-ever build isn't 35k "added" rows of noise
            _diff_and_log_events(conn, old_snapshot, new_snapshot)
        conn.commit()

    return ReindexResult(
        indexed_files=indexed_count,
        scanned_roots=[str(path) for path in valid_paths],
        missing_roots=missing_paths,
    )


def list_all_files(*, name_filter: str = "") -> list[LibraryEntry]:
    """Every indexed file (optionally substring-filtered) — the Library tab's
    full listing. The index persists in SQLite between sessions; use
    refresh_library_index() for a cheap delta pass or rebuild for a full one."""
    initialize_library_index_db()
    query = "SELECT name, path, root_path, size_bytes, modified_at FROM library_files"
    params: tuple = ()
    if name_filter.strip():
        query += " WHERE search_name LIKE ?"
        params = (f"%{name_filter.strip().casefold()}%",)
    query += " ORDER BY name COLLATE NOCASE"
    with _DB_LOCK, db.connect(_db_path()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [LibraryEntry(name=r[0], path=r[1], root_path=r[2],
                         size_bytes=r[3], modified_at=r[4]) for r in rows]


@dataclass(frozen=True)
class RefreshResult:
    added: int
    removed: int
    updated: int
    total: int


def refresh_library_index() -> RefreshResult:
    """Delta pass: reconcile the persisted index against the filesystem
    without a full rebuild — adds new files, drops vanished ones, updates
    changed sizes. Much faster than rebuild on large libraries."""
    initialize_library_index_db()
    valid_paths, _missing = _configured_library_paths()
    extensions = set(config.LIBRARY_INDEX_EXTENSIONS)

    on_disk: dict[str, tuple[str, str, int, float]] = {}
    for root_path in valid_paths:
        for current_root, _dirs, files in os.walk(root_path):
            for name in files:
                if extensions and Path(name).suffix.lower() not in extensions:
                    continue
                file_path = Path(current_root) / name
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                on_disk[str(file_path)] = (name, str(root_path),
                                           int(stat.st_size), float(stat.st_mtime))

    with _DB_LOCK, db.connect(_db_path()) as conn:
        indexed = {row[0]: (int(row[1]), float(row[2])) for row in
                   conn.execute("SELECT path, size_bytes, modified_at FROM library_files")}
        old_snapshot = {
            row[0]: (row[1], int(row[2])) for row in conn.execute(
                "SELECT path, name, size_bytes FROM library_files")
        }

        removed = [p for p in indexed if p not in on_disk]
        added = [p for p in on_disk if p not in indexed]
        updated = [p for p, (name, root, size, mtime) in on_disk.items()
                   if p in indexed and indexed[p] != (size, mtime)]

        conn.executemany("DELETE FROM library_files WHERE path = ?",
                         [(p,) for p in removed])
        conn.executemany(
            """
            INSERT OR REPLACE INTO library_files
            (path, name, root_path, search_name, size_bytes, modified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(p, on_disk[p][0], on_disk[p][1], on_disk[p][0].casefold(),
              on_disk[p][2], on_disk[p][3]) for p in added + updated],
        )
        new_snapshot = {p: (v[0], v[2]) for p, v in on_disk.items()}
        if old_snapshot:
            _diff_and_log_events(conn, old_snapshot, new_snapshot)
        conn.commit()

    return RefreshResult(added=len(added), removed=len(removed),
                         updated=len(updated), total=len(on_disk))


def remove_from_index(paths: list[str]) -> None:
    """Drop deleted files from the index immediately (no rescan needed)."""
    if not paths:
        return
    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.executemany("DELETE FROM library_files WHERE path = ?",
                         [(p,) for p in paths])
        conn.commit()


def indexed_file_count() -> int:
    initialize_library_index_db()
    with _DB_LOCK, db.connect(_db_path()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM library_files").fetchone()
    return int(row[0]) if row is not None else 0


def library_index_summary() -> LibraryIndexSummary:
    valid_paths, missing_paths = _configured_library_paths()
    return LibraryIndexSummary(
        indexed_files=indexed_file_count(),
        configured_paths=[str(path) for path in valid_paths],
        missing_paths=missing_paths,
    )


def format_library_summary_message() -> str:
    try:
        from plex_api import get_plex_library_sections

        plex_sections = get_plex_library_sections()
    except RuntimeError:
        plex_sections = []

    if plex_sections:
        visible_sections = [section for section in plex_sections if not section.hidden]
        hidden_sections = [section for section in plex_sections if section.hidden]
        lines = [
            f"Plex library sections: {len(visible_sections)} visible"
            + (f", {len(hidden_sections)} hidden" if hidden_sections else ""),
            "",
        ]
        for section in visible_sections:
            count_label = "unavailable" if section.item_count < 0 else f"{section.item_count} item(s)"
            lines.append(f"- {section.title} [{section.section_type}]: {count_label}")
            for location in section.locations:
                lines.append(f"  {location}")

        if hidden_sections:
            lines.extend(["", "Hidden sections:"])
            for section in hidden_sections:
                count_label = "unavailable" if section.item_count < 0 else f"{section.item_count} item(s)"
                lines.append(f"- {section.title} [{section.section_type}]: {count_label}")

        if config.PLEX_LIBRARY_PATHS:
            lines.extend(
                [
                    "",
                    "Filesystem index is also configured.",
                    "Use /reindex only for offline/fallback folder search.",
                ]
            )
        return "\n".join(lines)

    summary = library_index_summary()
    if not summary.configured_paths and not summary.missing_paths:
        return (
            "No library paths are configured yet.\n"
            "Open the Settings tab and add one or more library paths "
            "(tagged Movie / TV / Anime / xAnime), then click Reindex.\n"
            "Or set PLEX_TOKEN in Settings to use Plex's own search."
        )

    # Group configured paths by media type so the user can see which roots
    # contribute to which collective library.
    by_type: dict[str, list[str]] = {}
    for entry in getattr(config, "MEDIA_LIBRARY_PATHS", []):
        by_type.setdefault(entry.media_type, []).append(entry.path)

    lines = [
        f"Indexed library files: {summary.indexed_files}",
    ]

    if summary.indexed_files == 0 and summary.configured_paths:
        lines.append(
            "  (index is empty -- click Reindex or run /reindex to populate it)"
        )

    lines.append("")
    lines.append("Configured library paths:")
    if by_type:
        for media_type in ("movie", "tv", "anime", "xanime", "mixed"):
            paths = by_type.get(media_type, [])
            if not paths:
                continue
            label = {
                "movie": "Movies", "tv": "TV Shows",
                "anime": "Anime", "xanime": "xAnime / Hentai",
                "mixed": "Mixed / Untyped",
            }.get(media_type, media_type)
            lines.append(f"  {label}:")
            lines.extend(f"    - {p}" for p in paths)
    else:
        # Fallback if MEDIA_LIBRARY_PATHS isn't loaded for some reason.
        lines.extend(f"- {p}" for p in summary.configured_paths)

    if summary.missing_paths:
        lines.append("")
        lines.append("Missing/unavailable paths (drive unplugged or path renamed?):")
        lines.extend(f"- {p}" for p in summary.missing_paths)

    return "\n".join(lines)


def search_library(query: str, *, limit: int | None = None) -> list[LibraryEntry]:
    clean_query = " ".join(query.split()).casefold()
    if not clean_query:
        return []

    try:
        from plex_api import search_plex_library

        plex_results = search_plex_library(query, limit=limit or config.LIBRARY_SEARCH_RESULT_LIMIT)
    except RuntimeError:
        plex_results = []

    if plex_results:
        return [
            LibraryEntry(
                name=result.name,
                path=result.path,
                root_path=f"{result.section_title} [{result.media_type}]",
                size_bytes=result.size_bytes,
                modified_at=float(result.added_at),
            )
            for result in plex_results
        ]

    initialize_library_index_db()
    max_results = limit or config.LIBRARY_SEARCH_RESULT_LIMIT

    with _DB_LOCK, db.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT name, path, root_path, size_bytes, modified_at
            FROM library_files
            WHERE search_name LIKE ?
            ORDER BY name ASC
            LIMIT ?
            """,
            (f"%{clean_query}%", max_results),
        ).fetchall()

    return [
        LibraryEntry(
            name=row["name"],
            path=row["path"],
            root_path=row["root_path"],
            size_bytes=row["size_bytes"],
            modified_at=row["modified_at"],
        )
        for row in rows
    ]


def format_search_results_message(query: str, *, limit: int | None = None) -> str:
    results = search_library(query, limit=limit)
    if not results:
        return _format_no_results_diagnostic(query)

    lines = [f'Search results for "{query}" ({len(results)} shown):']
    for entry in results:
        lines.append(f"- {entry.name} [{entry.root_path}]")
        lines.append(f"  {entry.path}")
    return "\n".join(lines)


def _format_no_results_diagnostic(query: str) -> str:
    """
    Build a useful 'why did nothing match?' message instead of a generic
    'no results'. The message names the actual blocker the user can fix:
    no library paths configured, an empty SQLite index, or just no Plex
    token + no fallback index.
    """
    lines = [f'No matches for "{query}".']

    valid_paths, missing_paths = _configured_library_paths()
    has_paths = bool(valid_paths) or bool(missing_paths)
    has_plex_token = bool(getattr(config, "PLEX_TOKEN", "").strip())
    indexed = indexed_file_count()

    if not has_paths and not has_plex_token:
        lines.append("")
        lines.append(
            "No library paths are configured and no Plex token is set, so "
            "there's nothing to search. Open the Settings tab and add at "
            "least one library path, then click Reindex on the Library tab."
        )
        return "\n".join(lines)

    if has_paths and indexed == 0:
        lines.append("")
        lines.append(
            f"The local file index is empty even though {len(valid_paths)} "
            "library path(s) are configured. Click Reindex on the Library "
            "tab (or run /reindex) so the index actually contains your files."
        )
        return "\n".join(lines)

    if missing_paths:
        lines.append("")
        lines.append("Some configured library paths could not be reached:")
        lines.extend(f"  - {p}" for p in missing_paths)
        lines.append(
            "Fix the paths in the Settings tab (or check that the drive is mounted)."
        )

    if has_plex_token and indexed == 0:
        lines.append("")
        lines.append(
            "Plex search returned nothing and there's no local fallback index. "
            "Either Plex's own libraries don't include your folders, or the "
            "title really isn't there. Run Reindex to build a local fallback."
        )
    elif not has_plex_token and indexed > 0:
        lines.append("")
        lines.append(
            f"(Searched the local index of {indexed} file(s). Set PLEX_TOKEN "
            "in Settings if you also want Plex's metadata-aware search.)"
        )

    return "\n".join(lines)


def format_reindex_result_message(result: ReindexResult) -> str:
    lines = [
        f"Library reindex complete. Indexed {result.indexed_files} file(s).",
    ]
    if result.scanned_roots:
        lines.append("Scanned paths:")
        lines.extend(f"- {path}" for path in result.scanned_roots)
    if result.missing_roots:
        lines.append("Missing/unavailable paths:")
        lines.extend(f"- {path}" for path in result.missing_roots)
    return "\n".join(lines)


def _format_bytes(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def library_metrics() -> LibraryMetrics:
    initialize_library_index_db()
    _valid_paths, missing_paths = _configured_library_paths()
    now = time.time()
    cutoff_7d = now - (7 * 24 * 60 * 60)
    cutoff_30d = now - (30 * 24 * 60 * 60)

    with _DB_LOCK, db.connect(_db_path()) as conn:
        summary_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_files,
                COALESCE(SUM(size_bytes), 0) AS total_size_bytes,
                COALESCE(SUM(CASE WHEN modified_at >= ? THEN 1 ELSE 0 END), 0) AS recent_7d,
                COALESCE(SUM(CASE WHEN modified_at >= ? THEN 1 ELSE 0 END), 0) AS recent_30d
            FROM library_files
            """,
            (cutoff_7d, cutoff_30d),
        ).fetchone()

        root_rows = conn.execute(
            """
            SELECT
                root_path,
                COUNT(*) AS file_count,
                COALESCE(SUM(size_bytes), 0) AS total_size_bytes,
                COALESCE(SUM(CASE WHEN modified_at >= ? THEN 1 ELSE 0 END), 0) AS recent_7d,
                COALESCE(SUM(CASE WHEN modified_at >= ? THEN 1 ELSE 0 END), 0) AS recent_30d
            FROM library_files
            GROUP BY root_path
            ORDER BY file_count DESC, root_path ASC
            """,
            (cutoff_7d, cutoff_30d),
        ).fetchall()

    roots = [
        RootLibraryMetrics(
            root_path=row[0],
            file_count=int(row[1]),
            total_size_bytes=int(row[2]),
            recent_7d=int(row[3]),
            recent_30d=int(row[4]),
        )
        for row in root_rows
    ]

    return LibraryMetrics(
        total_files=int(summary_row[0]) if summary_row is not None else 0,
        total_size_bytes=int(summary_row[1]) if summary_row is not None else 0,
        recent_7d=int(summary_row[2]) if summary_row is not None else 0,
        recent_30d=int(summary_row[3]) if summary_row is not None else 0,
        roots=roots,
        missing_paths=missing_paths,
    )


def format_library_metrics_message() -> str:
    metrics = library_metrics()
    lines = [
        "Library Metrics",
        f"- Total indexed files: {metrics.total_files}",
        f"- Total indexed size: {_format_bytes(metrics.total_size_bytes)}",
        f"- Added/updated in last 7 days: {metrics.recent_7d}",
        f"- Added/updated in last 30 days: {metrics.recent_30d}",
    ]

    if metrics.roots:
        lines.append("")
        lines.append("Per-library totals:")
        for root in metrics.roots:
            lines.append(
                f"- {root.root_path}: {root.file_count} file(s), "
                f"{_format_bytes(root.total_size_bytes)}, "
                f"{root.recent_7d} new/updated in 7d"
            )

    if metrics.missing_paths:
        lines.append("")
        lines.append("Missing/unavailable configured paths:")
        lines.extend(f"- {path}" for path in metrics.missing_paths)

    lines.append("")
    lines.append(
        "Note: duration and watch-history metrics require Plex metadata/API access; "
        "these numbers are based on indexed files only."
    )
    return "\n".join(lines)
