# =============================================================================
# anime_db.py
# =============================================================================
# Local-first anime metadata: the same data zenshin-API aggregates, built
# from the sources directly so we depend on nobody's free-tier server.
#
#   1. manami-project/anime-offline-database  (weekly JSON, ~41k anime):
#      titles + synonyms, type, EPISODE COUNTS, status, season/year, score,
#      tags, and cross-links to 10 sites. License: ODbL — attribution in the
#      README.
#   2. Fribb/anime-lists (JSON): anidb ↔ anilist ↔ mal ↔ tvdb ↔ tmdb ↔ imdb
#      id mapping — the dataset Plex's HAMA agent and the Sonarr ecosystem
#      run on.
#   3. Anime-Lists/anime-lists master XML: curated per-show TVDB season +
#      episode offsets (kept for future curated remapping; ids are the main
#      win today).
#
# Everything lands in ONE SQLite file (anime_meta.sqlite) with an FTS5 index
# over every title/synonym: identification queries run in ~1 ms with zero
# network. ensure_fresh() refreshes weekly (the overnight idle pass calls
# it); a failed refresh keeps the previous database.
# =============================================================================

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DB_FILE = "anime_meta.sqlite"
_MAX_AGE_S = 7 * 24 * 3600  # weekly, matching manami's release cadence

_MANAMI_URLS = [
    # Release asset (current layout) first, then legacy in-repo path.
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database-minified.json",
    "https://raw.githubusercontent.com/manami-project/anime-offline-database/master/anime-offline-database-minified.json",
]
_FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
_ANIME_LISTS_XML_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"

_REFRESH_LOCK = threading.Lock()

# id extractors for manami "sources" URLs
_SOURCE_ID_RES = {
    "anidb": re.compile(r"anidb\.net/anime/(\d+)"),
    "anilist": re.compile(r"anilist\.co/anime/(\d+)"),
    "mal": re.compile(r"myanimelist\.net/anime/(\d+)"),
    "kitsu": re.compile(r"kitsu\.app/anime/(\d+)|kitsu\.io/anime/(\d+)"),
}


def _db_path() -> Path:
    return Path(config.APP_DIR) / DB_FILE


def available() -> bool:
    return _db_path().is_file()


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or _db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Download + build
# ---------------------------------------------------------------------------

def _download(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{config.APP_PRODUCT_NAME}/{config.APP_VERSION}",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _extract_ids(sources: list[str]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for url in sources or []:
        for key, pattern in _SOURCE_ID_RES.items():
            if key in ids:
                continue
            m = pattern.search(url)
            if m:
                ids[key] = int(next(g for g in m.groups() if g))
    return ids


def _build_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE anime (
            id          INTEGER PRIMARY KEY,
            title       TEXT NOT NULL,
            type        TEXT,
            episodes    INTEGER,
            status      TEXT,
            year        INTEGER,
            season      TEXT,
            score       REAL,
            is_adult    INTEGER NOT NULL DEFAULT 0,
            anidb_id    INTEGER,
            anilist_id  INTEGER,
            mal_id      INTEGER,
            kitsu_id    INTEGER
        );
        CREATE INDEX idx_anime_anidb ON anime(anidb_id);
        CREATE INDEX idx_anime_anilist ON anime(anilist_id);
        CREATE INDEX idx_anime_mal ON anime(mal_id);
        CREATE TABLE titles (
            anime_id INTEGER NOT NULL,
            title    TEXT NOT NULL
        );
        CREATE INDEX idx_titles_exact ON titles(title COLLATE NOCASE);
        CREATE TABLE tags (
            anime_id INTEGER NOT NULL,
            tag      TEXT NOT NULL
        );
        CREATE INDEX idx_tags_anime ON tags(anime_id);
        CREATE TABLE mappings (
            anidb_id          INTEGER PRIMARY KEY,
            anilist_id        INTEGER,
            mal_id            INTEGER,
            tvdb_id           INTEGER,
            tmdb_id           INTEGER,
            imdb_id           TEXT,
            default_tvdb_season TEXT,
            episode_offset    INTEGER
        );
    """)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE titles_fts USING fts5(title, anime_id UNINDEXED)")
    except sqlite3.OperationalError:
        logger.warning("SQLite FTS5 unavailable — anime search falls back to LIKE.")


def _build_database(manami: dict, fribb: list, xml_root, dest: Path) -> None:
    """Parse the three dumps into a fresh SQLite file (atomic swap at the end)."""
    tmp = dest.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = _connect(tmp)
    try:
        _build_schema(conn)
        has_fts = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='titles_fts'").fetchone())

        def _int(v):
            try:
                return int(v) if v is not None and not isinstance(v, (dict, list)) else None
            except (TypeError, ValueError):
                return None

        def _float(v):
            try:
                return float(v) if v is not None and not isinstance(v, (dict, list)) else None
            except (TypeError, ValueError):
                return None

        def _str(v):
            return str(v) if isinstance(v, (str, int)) else None

        rows = manami.get("data", [])
        for idx, entry in enumerate(rows):
            ids = _extract_ids(entry.get("sources", []))
            season_info = entry.get("animeSeason") or {}
            if not isinstance(season_info, dict):
                season_info = {}
            tags = [str(t).lower() for t in entry.get("tags", []) or []]
            is_adult = int(any(t in ("hentai", "erotica") for t in tags))
            score_info = entry.get("score")
            score = _float(score_info.get("arithmeticMean")
                           if isinstance(score_info, dict) else score_info)
            conn.execute(
                "INSERT INTO anime (id, title, type, episodes, status, year, season,"
                " score, is_adult, anidb_id, anilist_id, mal_id, kitsu_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (idx, str(entry.get("title") or ""), _str(entry.get("type")),
                 _int(entry.get("episodes")), _str(entry.get("status")),
                 _int(season_info.get("year")), _str(season_info.get("season")),
                 score, is_adult, ids.get("anidb"), ids.get("anilist"),
                 ids.get("mal"), ids.get("kitsu")),
            )
            titles = {entry.get("title") or ""}
            titles.update(str(s) for s in entry.get("synonyms", []))
            title_rows = [(idx, t) for t in titles if t.strip()]
            conn.executemany("INSERT INTO titles (anime_id, title) VALUES (?, ?)",
                             title_rows)
            if has_fts:
                conn.executemany(
                    "INSERT INTO titles_fts (title, anime_id) VALUES (?, ?)",
                    [(t, i) for i, t in title_rows])
            if tags:
                conn.executemany("INSERT INTO tags (anime_id, tag) VALUES (?, ?)",
                                 [(idx, t) for t in tags])

        # Fribb id bridge (anidb-keyed). Current schema: "tvdb_id" int,
        # "themoviedb_id" is {"tv": id} or {"movie": id}, "imdb_id" is a
        # list, and the curated TVDB season + episode offset ride along as
        # "season": {"tvdb": n} and "episode_offset".
        for entry in fribb:
            if not isinstance(entry, dict):
                continue
            anidb = _int(entry.get("anidb_id"))
            if not anidb:
                continue
            tmdb_raw = entry.get("themoviedb_id")
            if isinstance(tmdb_raw, dict):
                tmdb = _int(tmdb_raw.get("tv") or tmdb_raw.get("movie"))
            else:
                tmdb = _int(tmdb_raw)
            imdb_raw = entry.get("imdb_id")
            imdb = _str(imdb_raw[0] if isinstance(imdb_raw, list) and imdb_raw
                        else imdb_raw)
            season_raw = entry.get("season")
            tvdb_season = (_str(season_raw.get("tvdb"))
                           if isinstance(season_raw, dict) else None)
            conn.execute(
                "INSERT OR REPLACE INTO mappings (anidb_id, anilist_id, mal_id,"
                " tvdb_id, tmdb_id, imdb_id, default_tvdb_season, episode_offset)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (anidb, _int(entry.get("anilist_id")), _int(entry.get("mal_id")),
                 _int(entry.get("tvdb_id") or entry.get("thetvdb_id")),
                 tmdb, imdb, tvdb_season, _int(entry.get("episode_offset"))),
            )

        # Anime-Lists XML: curated TVDB season/episode offsets.
        if xml_root is not None:
            for node in xml_root.iter("anime"):
                try:
                    anidb = int(node.get("anidbid") or 0)
                except ValueError:
                    continue
                if not anidb:
                    continue
                offset_raw = node.get("episodeoffset")
                try:
                    offset = int(offset_raw) if offset_raw else None
                except ValueError:
                    offset = None
                # Supplement only — never clobber Fribb values with NULLs.
                conn.execute(
                    "UPDATE mappings SET"
                    " default_tvdb_season = COALESCE(?, default_tvdb_season),"
                    " episode_offset = COALESCE(?, episode_offset)"
                    " WHERE anidb_id = ?",
                    (node.get("defaulttvdbseason"), offset, anidb),
                )

        conn.execute("INSERT INTO meta (key, value) VALUES ('built_at', ?)",
                     (str(int(time.time())),))
        conn.execute("INSERT INTO meta (key, value) VALUES ('entries', ?)",
                     (str(len(rows)),))
        conn.commit()
    finally:
        conn.close()

    # Atomic-ish swap: the old DB stays valid until the new one is complete.
    dest.unlink(missing_ok=True)
    tmp.rename(dest)


def refresh(force: bool = False) -> str:
    """Download the dumps and rebuild the local database. Returns a summary.

    Serialized by a lock; concurrent callers wait then return fresh-enough.
    A failed download/build leaves the previous database untouched.
    """
    with _REFRESH_LOCK:
        if not force and available() and time.time() - _db_path().stat().st_mtime < _MAX_AGE_S:
            return "anime metadata already fresh"

        manami = None
        for url in _MANAMI_URLS:
            try:
                logger.info("Downloading anime-offline-database from %s …", url)
                manami = json.loads(_download(url).decode("utf-8"))
                break
            except Exception as exc:
                logger.warning("manami download failed from %s: %s", url, exc)
        if not isinstance(manami, dict) or not manami.get("data"):
            raise RuntimeError("anime-offline-database download failed — kept previous data")

        try:
            fribb = json.loads(_download(_FRIBB_URL).decode("utf-8"))
            if not isinstance(fribb, list):
                fribb = []
        except Exception as exc:
            logger.warning("Fribb anime-lists download failed: %s", exc)
            fribb = []

        xml_root = None
        try:
            xml_root = ET.fromstring(_download(_ANIME_LISTS_XML_URL).decode("utf-8", "replace"))
        except Exception as exc:
            logger.warning("Anime-Lists XML download failed: %s", exc)

        _build_database(manami, fribb, xml_root, _db_path())
        summary = (f"anime metadata rebuilt: {len(manami.get('data', []))} entries, "
                   f"{len(fribb)} id mappings")
        logger.info(summary)
        return summary


def ensure_fresh(*, background: bool = True) -> None:
    """Refresh when stale/missing. background=True never blocks the caller."""
    if available() and time.time() - _db_path().stat().st_mtime < _MAX_AGE_S:
        return
    if background:
        threading.Thread(target=lambda: _safe_refresh(), name="anime-db-refresh",
                         daemon=True).start()
    else:
        _safe_refresh()


def _safe_refresh() -> None:
    try:
        refresh()
    except Exception:
        logger.exception("Anime metadata refresh failed.")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnimeHit:
    title: str
    year: int | None
    episodes: int | None
    anime_type: str | None
    is_adult: bool
    anidb_id: int | None
    anilist_id: int | None
    mal_id: int | None
    all_titles: tuple[str, ...]
    score: float


_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def search(query: str, *, limit: int = 5) -> list[AnimeHit]:
    """Title/synonym search over the local dump: exact → FTS candidates →
    rapidfuzz ranking. Empty list when the database isn't built yet."""
    if not available() or not query.strip():
        return []
    q = query.strip()

    conn = _connect()
    try:
        candidate_ids: list[int] = [
            row[0] for row in conn.execute(
                "SELECT DISTINCT anime_id FROM titles WHERE title = ? COLLATE NOCASE"
                " LIMIT 25", (q,))
        ]
        if len(candidate_ids) < 25:
            tokens = _FTS_TOKEN_RE.findall(q)
            has_fts = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='titles_fts'").fetchone())
            if tokens and has_fts:
                match = " AND ".join(f'"{t}"*' for t in tokens)
                try:
                    for row in conn.execute(
                        "SELECT DISTINCT anime_id FROM titles_fts WHERE titles_fts"
                        " MATCH ? LIMIT 400", (match,)):
                        if row[0] not in candidate_ids:
                            candidate_ids.append(row[0])
                except sqlite3.OperationalError:
                    pass
            if not candidate_ids and tokens:
                like = "%" + "%".join(tokens) + "%"
                candidate_ids = [row[0] for row in conn.execute(
                    "SELECT DISTINCT anime_id FROM titles WHERE title LIKE ? LIMIT 200",
                    (like,))]
        if not candidate_ids:
            return []

        try:
            from rapidfuzz import fuzz
            def similarity(a: str, b: str) -> float:
                return fuzz.WRatio(a, b) / 100.0
        except ImportError:
            def similarity(a: str, b: str) -> float:
                return 1.0 if a.casefold() == b.casefold() else 0.0

        hits: list[AnimeHit] = []
        placeholders = ",".join("?" * len(candidate_ids))
        rows = conn.execute(
            f"SELECT id, title, type, episodes, year, is_adult, anidb_id,"
            f" anilist_id, mal_id FROM anime WHERE id IN ({placeholders})",
            candidate_ids).fetchall()
        for (aid, title, atype, episodes, year, is_adult, anidb_id,
             anilist_id, mal_id) in rows:
            all_titles = [r[0] for r in conn.execute(
                "SELECT title FROM titles WHERE anime_id = ?", (aid,))]
            best = max((similarity(q, t) for t in all_titles), default=0.0)
            hits.append(AnimeHit(
                title=title, year=year, episodes=episodes, anime_type=atype,
                is_adult=bool(is_adult), anidb_id=anidb_id,
                anilist_id=anilist_id, mal_id=mal_id,
                all_titles=tuple(t for t in all_titles if t != title),
                score=best,
            ))
        hits.sort(key=lambda h: -h.score)
        return hits[:limit]
    finally:
        conn.close()


def mapping_for_anidb(anidb_id: int | str) -> dict | None:
    """Cross-site ids + curated TVDB season/offset for one AniDB entry."""
    if not available():
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT anilist_id, mal_id, tvdb_id, tmdb_id, imdb_id,"
            " default_tvdb_season, episode_offset FROM mappings WHERE anidb_id = ?",
            (int(anidb_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"anilist_id": row[0], "mal_id": row[1], "tvdb_id": row[2],
            "tmdb_id": row[3], "imdb_id": row[4],
            "default_tvdb_season": row[5], "episode_offset": row[6]}


def titles_for_anidb(anidb_id: int | str) -> list[str]:
    """Every known title/synonym for one AniDB entry (empty when unknown)."""
    if not available():
        return []
    conn = _connect()
    try:
        row = conn.execute("SELECT id FROM anime WHERE anidb_id = ?",
                           (int(anidb_id),)).fetchone()
        if row is None:
            return []
        return [r[0] for r in conn.execute(
            "SELECT title FROM titles WHERE anime_id = ?", (row[0],))]
    finally:
        conn.close()


def episode_count_for_anidb(anidb_id: int | str) -> int | None:
    if not available():
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT episodes FROM anime WHERE anidb_id = ?", (int(anidb_id),)).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] else None


def status() -> str:
    """One-line freshness summary for the health check."""
    if not available():
        return "not built yet (downloads on first refresh)"
    conn = _connect()
    try:
        entries = conn.execute(
            "SELECT value FROM meta WHERE key='entries'").fetchone()
        maps = conn.execute("SELECT COUNT(*) FROM mappings").fetchone()
    finally:
        conn.close()
    age_days = (time.time() - _db_path().stat().st_mtime) / 86400
    return (f"{entries[0] if entries else '?'} anime, "
            f"{maps[0] if maps else 0} id mappings, {age_days:.1f} days old")
