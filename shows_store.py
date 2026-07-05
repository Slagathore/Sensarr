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
        UNIQUE(show_id, season, episode)
    )
    """,
]


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


def initialize_shows_db() -> None:
    with _SHOWS_LOCK, db.connect() as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
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
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.season > 0),
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.season > 0 AND e.has_file = 1),
                   (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id AND e.season > 0
                        AND e.has_file = 0 AND e.air_date IS NOT NULL AND e.air_date <= ?),
                   (SELECT MIN(e.air_date) FROM episodes e WHERE e.show_id = s.id
                        AND e.air_date IS NOT NULL AND e.air_date > ?)
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
            next_air_date=r[13],
        )
        for r in rows
    ]


def replace_episodes(show_id: int, episodes: list) -> None:
    """Upsert the authoritative episode list from the tracker (keeps has_file)."""
    with _SHOWS_LOCK, db.connect() as conn:
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


def set_show_status(show_id: int, status: str) -> None:
    if not status:
        return
    with _SHOWS_LOCK, db.connect() as conn:
        conn.execute("UPDATE tracked_shows SET status = ? WHERE id = ?", (status, show_id))
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
            SELECT id, show_id, season, episode, title, air_date, has_file, file_path
            FROM episodes WHERE show_id = ? ORDER BY season, episode
            """,
            (show_id,),
        ).fetchall()
    return [EpisodeRow(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7]) for r in rows]


def missing_episodes(show_id: int) -> list[EpisodeRow]:
    """Aired, regular-season episodes with no file on disk."""
    today = date.today().isoformat()
    with _SHOWS_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, show_id, season, episode, title, air_date, has_file, file_path
            FROM episodes
            WHERE show_id = ? AND season > 0 AND has_file = 0
              AND air_date IS NOT NULL AND air_date <= ?
            ORDER BY season, episode
            """,
            (show_id, today),
        ).fetchall()
    return [EpisodeRow(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7]) for r in rows]


def upcoming_episodes(*, days: int = 14) -> list[tuple[TrackedShow, EpisodeRow]]:
    """Episodes airing between today and today+days across all tracked shows."""
    initialize_shows_db()
    start = date.today().isoformat()
    end = (date.today() + timedelta(days=days)).isoformat()
    with _SHOWS_LOCK, db.connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.show_id, e.season, e.episode, e.title, e.air_date,
                   e.has_file, e.file_path
            FROM episodes e
            WHERE e.air_date IS NOT NULL AND e.air_date >= ? AND e.air_date <= ?
            ORDER BY e.air_date, e.show_id, e.season, e.episode
            """,
            (start, end),
        ).fetchall()
    shows_by_id = {s.show_id: s for s in list_shows()}
    out: list[tuple[TrackedShow, EpisodeRow]] = []
    for r in rows:
        show = shows_by_id.get(r[1])
        if show is not None:
            out.append((show, EpisodeRow(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7])))
    return out
