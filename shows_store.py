# =============================================================================
# shows_store.py
# =============================================================================
# SQLite persistence for tracked shows (Radarr/Sonarr-style inventory):
#   tracked_shows — one row per show, keyed by (source, external_id) using the
#                   per-type tracker the request pipeline uses (TVDB/TMDB for
#                   TV, Jikan/MAL for anime + xanime, AniDB ids for xanime
#                   identification).
#   show_folders  — folders on disk mapped to a show. A show may map to
#                   MULTIPLE folders on different drives — seasons split
#                   across drives are first-class here, unlike Sonarr.
#   episodes      — per-episode air dates + on-disk state; missing/upcoming
#                   views derive from this.
# =============================================================================

import threading
from dataclasses import dataclass
from datetime import date, timedelta

import db

_SHOWS_LOCK = threading.Lock()

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS tracked_shows (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        media_type   TEXT NOT NULL,
        source       TEXT NOT NULL,
        external_id  TEXT NOT NULL,
        external_url TEXT,
        status       TEXT NOT NULL DEFAULT '',
        year         INTEGER,
        added_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_synced  TEXT,
        UNIQUE(source, external_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS show_folders (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id  INTEGER NOT NULL,
        path     TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episodes (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        show_id   INTEGER NOT NULL,
        season    INTEGER NOT NULL,
        episode   INTEGER NOT NULL,
        title     TEXT NOT NULL DEFAULT '',
        air_date  TEXT,
        has_file  INTEGER NOT NULL DEFAULT 0,
        file_path TEXT,
        grab_download_id INTEGER,
        UNIQUE(show_id, season, episode)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS season_targets (
        show_id INTEGER NOT NULL,
        season  INTEGER NOT NULL,
        path    TEXT NOT NULL,
        PRIMARY KEY (show_id, season)
    )
    """,
]

# Columns added after the tables first shipped — applied via ALTER on upgrade.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("episodes", "grab_download_id", "INTEGER"),
    # Airing schedule — next_episode_to_air captured from TMDB (see show_tracker).
    ("tracked_shows", "tmdb_id", "TEXT"),
    ("tracked_shows", "next_air_date", "TEXT"),
    ("tracked_shows", "next_season", "INTEGER"),
    ("tracked_shows", "next_episode", "INTEGER"),
    # Per-show release controls: silenced hides the show from Upcoming;
    # auto_grab downloads its episodes on release even when the global
    # Shows auto-grab toggle is off.
    ("tracked_shows", "silenced", "INTEGER NOT NULL DEFAULT 0"),
    ("tracked_shows", "auto_grab", "INTEGER NOT NULL DEFAULT 0"),
]


def _apply_migrations(conn) -> None:
    for table, column, col_def in _MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


@dataclass(frozen=True)
class TrackedShow:
    show_id: int
    title: str
    media_type: str
    source: str
    external_id: str
    external_url: str | None
    status: str
    year: int | None
    added_at: str
    last_synced: str | None
    folders: tuple[str, ...] = ()
    episode_count: int = 0
    have_count: int = 0
    missing_count: int = 0
    next_air_date: str | None = None
    next_season: int | None = None
    next_episode: int | None = None
    tmdb_id: str | None = None
    silenced: bool = False
    auto_grab: bool = False


@dataclass(frozen=True)
class EpisodeRow:
    episode_id: int
    show_id: int
    season: int
    episode: int
    title: str
    air_date: str | None
    has_file: bool
    file_path: str | None
    grab_download_id: int | None = None


def initialize_shows_db() -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        _apply_migrations(conn)
        conn.commit()


def upsert_show(
    *, title: str, media_type: str, source: str, external_id: str,
    external_url: str | None = None, status: str = "", year: int | None = None,
) -> int:
    """Insert or refresh a tracked show; returns its row id."""
    initialize_shows_db()
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            """
            INSERT INTO tracked_shows (title, media_type, source, external_id, external_url, status, year)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                title = excluded.title,
                status = CASE WHEN excluded.status != '' THEN excluded.status ELSE status END,
                external_url = COALESCE(excluded.external_url, external_url),
                year = COALESCE(excluded.year, year)
            """,
            (title, media_type, source, external_id, external_url, status, year),
        )
        row = conn.execute(
            "SELECT id FROM tracked_shows WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        conn.commit()
    return int(row[0])


def add_show_folder(show_id: int, path: str) -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            "INSERT INTO show_folders (show_id, path) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET show_id = excluded.show_id",
            (show_id, path),
        )
        conn.commit()


def folder_mapped(path: str) -> bool:
    initialize_shows_db()
    with _SHOWS_LOCK, db.connect() as conn:
        row = conn.execute("SELECT 1 FROM show_folders WHERE path = ?", (path,)).fetchone()
    return row is not None


def remove_show(show_id: int) -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("DELETE FROM episodes WHERE show_id = ?", (show_id,))
        conn.execute("DELETE FROM show_folders WHERE show_id = ?", (show_id,))
        conn.execute("DELETE FROM season_targets WHERE show_id = ?", (show_id,))
        conn.execute("DELETE FROM tracked_shows WHERE id = ?", (show_id,))
        conn.commit()


def get_show(show_id: int) -> TrackedShow | None:
    shows = [s for s in list_shows() if s.show_id == show_id]
    return shows[0] if shows else None


def list_shows() -> list[TrackedShow]:
    """All tracked shows with folder lists and have/missing rollups."""
    initialize_shows_db()
    today = date.today().isoformat()
    with _SHOWS_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.media_type, s.source, s.external_id,
                   s.external_url, s.status, s.year, s.added_at, s.last_synced,
                   -- Undated no-file rows are unaired "TBA" placeholders
                   -- (e.g. an announced next season) — not real inventory,
                   -- so they don't inflate the known-episode count.
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.season > 0
                        AND (e.air_date IS NOT NULL OR e.has_file = 1)),
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.season > 0 AND e.has_file = 1),
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.season > 0
                        AND e.has_file = 0 AND e.air_date IS NOT NULL AND e.air_date <= ?),
                   -- Prefer the TMDB-sourced next_air_date; fall back to the
                   -- earliest future episode row if one happens to exist.
                   COALESCE(s.next_air_date,
                            (SELECT MIN(e.air_date) FROM episodes e WHERE e.show_id = s.id
                                 AND e.air_date IS NOT NULL AND e.air_date > ?)),
                   s.next_season, s.next_episode, s.tmdb_id, s.silenced, s.auto_grab
            FROM tracked_shows s ORDER BY s.title COLLATE NOCASE
            """,
            (today, today),
        ).fetchall()
        folder_rows = conn.execute("SELECT show_id, path FROM show_folders").fetchall()

    folders: dict[int, list[str]] = {}
    for show_id, path in folder_rows:
        folders.setdefault(show_id, []).append(path)

    return [
        TrackedShow(
            show_id=r[0], title=r[1], media_type=r[2], source=r[3],
            external_id=r[4], external_url=r[5], status=r[6], year=r[7],
            added_at=r[8], last_synced=r[9],
            folders=tuple(folders.get(r[0], [])),
            episode_count=r[10], have_count=r[11], missing_count=r[12],
            next_air_date=r[13], next_season=r[14], next_episode=r[15],
            tmdb_id=r[16], silenced=bool(r[17]), auto_grab=bool(r[18]),
        )
        for r in rows
    ]


def clear_episodes(show_id: int) -> None:
    """Drop a show's episode rows — used when switching numbering schemes
    (absolute ↔ seasons), where stale rows would double-count."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("DELETE FROM episodes WHERE show_id = ?", (show_id,))
        conn.commit()


def replace_episodes(show_id: int, episodes: list) -> None:
    """Make the tracker's episode list authoritative (keeps has_file).

    Rows NOT in the new list are DELETED — when a show flips from absolute
    ordering (one 36-episode "season 1") to real seasons, the stale absolute
    rows used to survive forever and read as phantom missing episodes.
    The on-disk scan (update_file_state) runs right after every sync, so
    legitimately-on-disk extras are re-inserted immediately.
    """
    with _SHOWS_LOCK, db.connect() as conn:
        keep = {(ep.season, ep.episode) for ep in episodes}
        for season, episode in [
            (r[0], r[1]) for r in conn.execute(
                "SELECT season, episode FROM episodes WHERE show_id = ?", (show_id,)
            ).fetchall()
        ]:
            if (season, episode) not in keep:
                conn.execute(
                    "DELETE FROM episodes WHERE show_id = ? AND season = ? AND episode = ?",
                    (show_id, season, episode),
                )
        for ep in episodes:
            conn.execute(
                """
                INSERT INTO episodes (show_id, season, episode, title, air_date)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(show_id, season, episode) DO UPDATE SET
                    title = excluded.title,
                    air_date = excluded.air_date
                """,
                (show_id, ep.season, ep.episode, ep.title, ep.air_date),
            )
        conn.execute(
            "UPDATE tracked_shows SET last_synced = CURRENT_TIMESTAMP WHERE id = ?",
            (show_id,),
        )
        conn.commit()


def set_show_silenced(show_id: int, silenced: bool) -> None:
    """Silence/unsilence a show's releases (hidden from Upcoming when on)."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET silenced = ? WHERE id = ?",
                     (1 if silenced else 0, show_id))
        conn.commit()


def set_show_auto_grab(show_id: int, auto_grab: bool) -> None:
    """Mark a show for automatic download when its episodes release."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET auto_grab = ? WHERE id = ?",
                     (1 if auto_grab else 0, show_id))
        conn.commit()


def rename_show(show_id: int, title: str) -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET title = ? WHERE id = ?", (title, show_id))
        conn.commit()


def reidentify_show(
    show_id: int, *, title: str, source: str, external_id: str,
    external_url: str | None, year: int | None, media_type: str | None = None,
) -> tuple[str, int]:
    """Point a tracked show at a different tracker entry ("fix match").

    Episode rows are cleared (the new identity's numbering may differ) and
    airing/status/tmdb caches reset — the caller should re-sync afterwards.
    If ANOTHER row already has that (source, external_id), the two rows are
    merged instead (folders move there; this row is deleted).

    Returns ("updated", show_id) or ("merged", surviving_id).
    """
    with _SHOWS_LOCK, db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM tracked_shows WHERE source = ? AND external_id = ? AND id != ?",
            (source, external_id, show_id),
        ).fetchone()
    if existing is not None:
        surviving = int(existing[0])
        merge_shows(surviving, [show_id])
        return ("merged", surviving)

    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            """
            UPDATE tracked_shows SET
                title = ?, source = ?, external_id = ?, external_url = ?,
                year = ?, media_type = COALESCE(?, media_type),
                status = '', tmdb_id = NULL, last_synced = NULL,
                next_air_date = NULL, next_season = NULL, next_episode = NULL
            WHERE id = ?
            """,
            (title, source, external_id, external_url, year, media_type, show_id),
        )
        conn.execute("DELETE FROM episodes WHERE show_id = ?", (show_id,))
        conn.commit()
    return ("updated", show_id)


def merge_shows(primary_id: int, duplicate_ids: list[int]) -> int:
    """Merge duplicate tracked-show rows into one ("these shows are the same").

    Folders, season targets, and on-disk episode state move to the primary;
    the duplicate rows are deleted. Episode lists are NOT merged wholesale
    (each source numbers seasons differently) — only has_file markers carry
    over where (season, episode) lines up. Returns how many rows were merged.
    """
    merged = 0
    with _SHOWS_LOCK, db.connect() as conn:
        for dup_id in duplicate_ids:
            if dup_id == primary_id:
                continue
            conn.execute("UPDATE show_folders SET show_id = ? WHERE show_id = ?",
                         (primary_id, dup_id))
            conn.execute(
                "INSERT OR IGNORE INTO season_targets (show_id, season, path) "
                "SELECT ?, season, path FROM season_targets WHERE show_id = ?",
                (primary_id, dup_id))
            # Carry over on-disk markers for matching episode numbers.
            for season, episode, path in conn.execute(
                "SELECT season, episode, file_path FROM episodes "
                "WHERE show_id = ? AND has_file = 1", (dup_id,)
            ).fetchall():
                conn.execute(
                    """
                    INSERT INTO episodes (show_id, season, episode, has_file, file_path)
                    VALUES (?, ?, ?, 1, ?)
                    ON CONFLICT(show_id, season, episode) DO UPDATE SET
                        has_file = 1, file_path = COALESCE(episodes.file_path, excluded.file_path)
                    """,
                    (primary_id, season, episode, path))
            conn.execute("DELETE FROM episodes WHERE show_id = ?", (dup_id,))
            conn.execute("DELETE FROM season_targets WHERE show_id = ?", (dup_id,))
            conn.execute("DELETE FROM tracked_shows WHERE id = ?", (dup_id,))
            merged += 1
        conn.commit()
    return merged


def set_show_status(show_id: int, status: str) -> None:
    if not status:
        return
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET status = ? WHERE id = ?", (status, show_id))
        conn.commit()


def set_show_tmdb_id(show_id: int, tmdb_id: str) -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET tmdb_id = ? WHERE id = ?", (tmdb_id, show_id))
        conn.commit()


def set_show_airing(show_id: int, *, next_air_date: str | None,
                    next_season: int | None, next_episode: int | None) -> None:
    """Store the next-episode-to-air captured from TMDB (all None clears it)."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE tracked_shows SET next_air_date = ?, next_season = ?, next_episode = ? WHERE id = ?",
            (next_air_date, next_season, next_episode, show_id),
        )
        conn.commit()


def update_file_state(show_id: int, found: dict[tuple[int, int], str]) -> None:
    """Overwrite on-disk state: found maps (season, episode) -> file path."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET has_file = 0, file_path = NULL WHERE show_id = ?",
            (show_id,),
        )
        for (season, episode), path in found.items():
            conn.execute(
                """
                INSERT INTO episodes (show_id, season, episode, has_file, file_path)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(show_id, season, episode) DO UPDATE SET
                    has_file = 1, file_path = excluded.file_path
                """,
                (show_id, season, episode, path),
            )
        conn.commit()


def list_episodes(show_id: int) -> list[EpisodeRow]:
    with _SHOWS_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, show_id, season, episode, title, air_date, has_file,
                   file_path, grab_download_id
            FROM episodes WHERE show_id = ? ORDER BY season, episode
            """,
            (show_id,),
        ).fetchall()
    return [EpisodeRow(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8]) for r in rows]


def missing_episodes(show_id: int) -> list[EpisodeRow]:
    """Aired, regular-season episodes with no file on disk."""
    today = date.today().isoformat()
    with _SHOWS_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, show_id, season, episode, title, air_date, has_file,
                   file_path, grab_download_id
            FROM episodes
            WHERE show_id = ? AND season > 0 AND has_file = 0
              AND air_date IS NOT NULL AND air_date <= ?
            ORDER BY season, episode
            """,
            (show_id, today),
        ).fetchall()
    return [EpisodeRow(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7], r[8]) for r in rows]


def set_episode_grab(show_id: int, season: int, episode: int,
                     download_id: int | None) -> None:
    """Link (or clear) the download that is fetching this episode."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET grab_download_id = ? "
            "WHERE show_id = ? AND season = ? AND episode = ?",
            (download_id, show_id, season, episode),
        )
        conn.commit()


def set_episode_file(show_id: int, season: int, episode: int, path: str) -> None:
    """Mark one episode as on-disk (called after a routed move completes)."""
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            """
            INSERT INTO episodes (show_id, season, episode, has_file, file_path)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(show_id, season, episode) DO UPDATE SET
                has_file = 1, file_path = excluded.file_path
            """,
            (show_id, season, episode, path),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Per-season target folders — the policy layer Sonarr doesn't offer
# ---------------------------------------------------------------------------

def set_season_target(show_id: int, season: int, path: str) -> None:
    initialize_shows_db()
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            "INSERT INTO season_targets (show_id, season, path) VALUES (?, ?, ?) "
            "ON CONFLICT(show_id, season) DO UPDATE SET path = excluded.path",
            (show_id, season, path),
        )
        conn.commit()


def get_season_target(show_id: int, season: int) -> str | None:
    initialize_shows_db()
    with _SHOWS_LOCK, db.connect() as conn:
        row = conn.execute(
            "SELECT path FROM season_targets WHERE show_id = ? AND season = ?",
            (show_id, season),
        ).fetchone()
    return row[0] if row else None


def clear_season_target(show_id: int, season: int) -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute(
            "DELETE FROM season_targets WHERE show_id = ? AND season = ?",
            (show_id, season),
        )
        conn.commit()


def upcoming_episodes(*, days: int = 14,
                      include_silenced: bool = False) -> list[tuple[TrackedShow, EpisodeRow]]:
    """Episodes airing between today and today+days across all tracked shows.

    Primary source is each show's stored next-episode-to-air (TMDB); we also
    fold in any genuinely future-dated episode rows (TVDB provides these) so a
    week with multiple episodes isn't collapsed to one. Deduped by
    (show, season, episode).
    """
    initialize_shows_db()
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    with _SHOWS_LOCK, db.connect() as conn:
        ep_rows = conn.execute(
            """
            SELECT e.show_id, e.season, e.episode, e.title, e.air_date,
                   e.has_file, e.file_path
            FROM episodes e
            WHERE e.air_date IS NOT NULL AND e.air_date >= ? AND e.air_date <= ?
            """,
            (start, end),
        ).fetchall()

    shows_by_id = {
        s.show_id: s for s in list_shows()
        if include_silenced or not s.silenced
    }
    merged: dict[tuple[int, int, int], tuple[TrackedShow, EpisodeRow]] = {}
    # (show_id, air_date) already covered — prevents listing the same episode
    # twice when the stored next-air (AniList absolute numbering, e.g. S01E1169)
    # and a TMDB future-episode row (season numbering, S23E1169) describe the
    # same broadcast on the same day.
    covered_dates: set[tuple[int, str]] = set()

    # Stored next-air per show (the reliable signal) takes precedence.
    for show in shows_by_id.values():
        if (show.next_air_date and start <= show.next_air_date <= end
                and show.next_season is not None and show.next_episode is not None):
            key = (show.show_id, show.next_season, show.next_episode)
            merged[key] = (show, EpisodeRow(
                0, show.show_id, show.next_season, show.next_episode, "",
                show.next_air_date, False, None,
            ))
            covered_dates.add((show.show_id, show.next_air_date))

    # Future-dated episode rows (belt-and-suspenders for TVDB shows), unless the
    # show already has a stored next-air on that date.
    for r in ep_rows:
        show = shows_by_id.get(r[0])
        if show is None or (r[0], r[4]) in covered_dates:
            continue
        key = (r[0], r[1], r[2])
        merged.setdefault(key, (show, EpisodeRow(0, r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6])))

    return sorted(merged.values(), key=lambda pair: (pair[1].air_date or "", pair[0].title))
