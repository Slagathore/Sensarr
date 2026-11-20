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

import gzip
import hashlib
import http.client
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DB_FILE = "anime_meta.sqlite"
_MAX_AGE_S = 7 * 24 * 3600  # weekly, matching manami's release cadence
_SCHEMA_VERSION = 2  # bump when columns change: forces a rebuild of old DBs

_MANAMI_URLS = [
    # Data lives in release assets. The final dated release also bypasses
    # the /releases/latest redirect when that endpoint is unavailable.
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database-minified.json",
    "https://github.com/manami-project/anime-offline-database/releases/download/2026-27/anime-offline-database-minified.json",
]
_FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
_ANIME_LISTS_XML_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"

# ---------------------------------------------------------------------------
# Weekly manifest distribution (Task I / FIX_SPRINT_BOOTSTRAP.md section I).
#
# CI (.github/workflows/anime-db.yml) builds the FULL artifact weekly (all
# three sources merged, exactly like the local build below) and publishes it
# under a dedicated ROLLING tag — never a dated tag — so the client never
# needs the GitHub API (rate-limited, and `/releases/latest` would otherwise
# collide with updater.py's own use of that exact endpoint for APP updates:
# if the anime-db publish used a dated tag, GitHub would mark it "latest" and
# updater.check_for_update() would start reading the anime-db release instead
# of the newest app version). A rolling tag gives fixed, permanent asset URLs
# and is published with --latest=false so the app's own release stays
# "latest".
#
# Licensing (checked 2026-07-18, see FIX_SPRINT_BOOTSTRAP.md section I):
# manami-project/anime-offline-database is ODbL 1.0 (share-alike,
# attribution required — see NOTICE via publish_anime_db.py). Fribb/
# anime-lists and Anime-Lists/anime-lists publish no license file at all —
# there is no permission-by-license to redistribute their id mappings.
# DECISION (Cole, 2026-07-19): bundle all three sources in the published
# artifact anyway, knowingly accepting the unlicensed-mappings risk, with
# attribution to both projects in the NOTICE. Clients that already have a
# manami-only artifact (an older publish, or a build that degraded when a
# mapping source was unreachable) still merge Fribb/Anime-Lists locally as a
# fallback — see _merge_local_id_sources() / _refresh_from_manifest().
# ---------------------------------------------------------------------------
_REPO = "Slagathore/Sensarr"
_MANIFEST_TAG = "anime-db-latest"
_RELEASE_ASSET_BASE = f"https://github.com/{_REPO}/releases/download/{_MANIFEST_TAG}"
MANIFEST_URL = f"{_RELEASE_ASSET_BASE}/latest.json"

_REFRESH_LOCK = threading.Lock()

# id extractors for manami "sources" URLs
_SOURCE_ID_RES = {
    "anidb": re.compile(r"anidb\.net/anime/(\d+)"),
    "anilist": re.compile(r"anilist\.co/anime/(\d+)"),
    "mal": re.compile(r"myanimelist\.net/anime/(\d+)"),
    "kitsu": re.compile(r"kitsu\.app/anime/(\d+)|kitsu\.io/anime/(\d+)"),
}


def _db_path() -> Path:
    # DATA dir of the path contract — the executable's folder on Windows
    # (unchanged layout), ~/.local/share/sensarr on Linux.
    import app_paths
    return app_paths.PATHS.data_dir / DB_FILE


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


def fetch_manami(timeout: int = 180) -> dict:
    """Fetch a usable Manami dataset with a bounded retry per release URL."""
    for url in _MANAMI_URLS:
        for attempt in range(2):
            try:
                logger.info("Downloading anime-offline-database from %s", url)
                manami = json.loads(_download(url, timeout=timeout))
                entries = manami.get("data") if isinstance(manami, dict) else None
                if not isinstance(entries, list) or not entries:
                    raise ValueError("dataset has no anime entries")
                return manami
            except urllib.error.HTTPError as exc:
                retry = exc.code in (408, 429) or 500 <= exc.code < 600
                logger.warning("Manami download failed from %s: %s", url, exc)
            except (OSError, http.client.HTTPException) as exc:
                retry = True
                logger.warning("Manami download failed from %s: %s", url, exc)
            except ValueError as exc:
                logger.warning("Invalid Manami dataset from %s: %s", url, exc)
                break
            if not retry or attempt == 1:
                break
            time.sleep(2)
    raise RuntimeError("anime-offline-database download failed from every release URL")


def _fetch_fribb(timeout: int = 180) -> list:
    """Fribb id-mapping dump. Best-effort: unreachable/malformed degrades to
    an empty list rather than raising — shared by refresh()'s full local
    build, the manifest-artifact fallback merge, and publish_anime_db.py's
    CI build, none of which should ever fail outright just because this one
    optional mapping source is down."""
    try:
        fribb = json.loads(_download(_FRIBB_URL, timeout=timeout).decode("utf-8"))
        return fribb if isinstance(fribb, list) else []
    except Exception as exc:
        logger.warning("Fribb anime-lists download failed: %s", exc)
        return []


def _fetch_anime_lists_xml(timeout: int = 180):
    """Anime-Lists curated XML. Best-effort: unreachable/malformed degrades
    to None (see _fetch_fribb's docstring for why)."""
    try:
        return ET.fromstring(_download(_ANIME_LISTS_XML_URL, timeout=timeout)
                             .decode("utf-8", "replace"))
    except Exception as exc:
        logger.warning("Anime-Lists XML download failed: %s", exc)
        return None


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
            duration_min REAL,
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


def _coerce_int(v):
    try:
        return int(v) if v is not None and not isinstance(v, (dict, list)) else None
    except (TypeError, ValueError):
        return None


def _coerce_float(v):
    try:
        return float(v) if v is not None and not isinstance(v, (dict, list)) else None
    except (TypeError, ValueError):
        return None


def _coerce_str(v):
    return str(v) if isinstance(v, (str, int)) else None


def _insert_manami_rows(conn: sqlite3.Connection, manami: dict, has_fts: bool) -> list:
    """Populate anime/titles/titles_fts/tags from a manami dump on an
    already-schema'd connection. Returns the raw ``data`` rows so callers can
    report a count without re-reading the dump."""
    rows = manami.get("data", [])
    for idx, entry in enumerate(rows):
        ids = _extract_ids(entry.get("sources", []))
        season_info = entry.get("animeSeason") or {}
        if not isinstance(season_info, dict):
            season_info = {}
        tags = [str(t).lower() for t in entry.get("tags", []) or []]
        is_adult = int(any(t in ("hentai", "erotica") for t in tags))
        score_info = entry.get("score")
        score = _coerce_float(score_info.get("arithmeticMean")
                              if isinstance(score_info, dict) else score_info)
        duration_info = entry.get("duration")
        duration_min = None
        if isinstance(duration_info, dict):
            value = _coerce_float(duration_info.get("value"))
            if value:
                unit = str(duration_info.get("unit") or "SECONDS").upper()
                duration_min = value / 60.0 if unit == "SECONDS" else value
        conn.execute(
            "INSERT INTO anime (id, title, type, episodes, status, year, season,"
            " score, duration_min, is_adult, anidb_id, anilist_id, mal_id, kitsu_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (idx, str(entry.get("title") or ""), _coerce_str(entry.get("type")),
             _coerce_int(entry.get("episodes")), _coerce_str(entry.get("status")),
             _coerce_int(season_info.get("year")), _coerce_str(season_info.get("season")),
             score, duration_min, is_adult, ids.get("anidb"), ids.get("anilist"),
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
    return rows


def _merge_fribb_mappings(conn: sqlite3.Connection, fribb: list) -> None:
    """Fribb id bridge (anidb-keyed). Current schema: "tvdb_id" int,
    "themoviedb_id" is {"tv": id} or {"movie": id}, "imdb_id" is a list, and
    the curated TVDB season + episode offset ride along as
    "season": {"tvdb": n} and "episode_offset"."""
    for entry in fribb:
        if not isinstance(entry, dict):
            continue
        anidb = _coerce_int(entry.get("anidb_id"))
        if not anidb:
            continue
        tmdb_raw = entry.get("themoviedb_id")
        if isinstance(tmdb_raw, dict):
            tmdb = _coerce_int(tmdb_raw.get("tv") or tmdb_raw.get("movie"))
        else:
            tmdb = _coerce_int(tmdb_raw)
        imdb_raw = entry.get("imdb_id")
        imdb = _coerce_str(imdb_raw[0] if isinstance(imdb_raw, list) and imdb_raw
                           else imdb_raw)
        season_raw = entry.get("season")
        tvdb_season = (_coerce_str(season_raw.get("tvdb"))
                       if isinstance(season_raw, dict) else None)
        conn.execute(
            "INSERT OR REPLACE INTO mappings (anidb_id, anilist_id, mal_id,"
            " tvdb_id, tmdb_id, imdb_id, default_tvdb_season, episode_offset)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (anidb, _coerce_int(entry.get("anilist_id")), _coerce_int(entry.get("mal_id")),
             _coerce_int(entry.get("tvdb_id") or entry.get("thetvdb_id")),
             tmdb, imdb, tvdb_season, _coerce_int(entry.get("episode_offset"))),
        )


def _merge_anime_lists_xml(conn: sqlite3.Connection, xml_root) -> None:
    """Anime-Lists XML: curated TVDB season/episode offsets. Supplement
    only — never clobbers a Fribb value with NULL."""
    if xml_root is None:
        return
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
        conn.execute(
            "UPDATE mappings SET"
            " default_tvdb_season = COALESCE(?, default_tvdb_season),"
            " episode_offset = COALESCE(?, episode_offset)"
            " WHERE anidb_id = ?",
            (node.get("defaulttvdbseason"), offset, anidb),
        )


def _write_build_meta(conn: sqlite3.Connection, entries: int) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES ('built_at', ?)",
                 (str(int(time.time())),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(_SCHEMA_VERSION),))
    conn.execute("INSERT INTO meta (key, value) VALUES ('entries', ?)",
                 (str(entries),))


def _build_database(manami: dict, fribb: list, xml_root, dest: Path) -> None:
    """Parse all three dumps into a fresh SQLite file (atomic-ish swap at the
    end). This is the full local-build fallback path: used when the weekly
    manifest artifact is disabled, unreachable, or schema-incompatible."""
    tmp = dest.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = _connect(tmp)
    try:
        _build_schema(conn)
        has_fts = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='titles_fts'").fetchone())
        rows = _insert_manami_rows(conn, manami, has_fts)
        _merge_fribb_mappings(conn, fribb)
        _merge_anime_lists_xml(conn, xml_root)
        _write_build_meta(conn, len(rows))
        conn.commit()
    finally:
        conn.close()

    # Atomic-ish swap: the old DB stays valid until the new one is complete.
    dest.unlink(missing_ok=True)
    tmp.rename(dest)


def build_manami_artifact(manami: dict, dest: Path) -> None:
    """Manami-ONLY build (no Fribb/Anime-Lists merge): a thin convenience
    wrapper over _build_database for tests/tools that want a minimal
    artifact without fetching mapping sources, and for the degraded-publish
    path when both mapping sources are unreachable at build time. The real
    weekly artifact bundles all three sources (Cole's 2026-07-19 decision,
    see the module-level comment above _REPO) via publish_anime_db.py
    calling _build_database directly.
    """
    _build_database(manami, [], None, dest)


def _schema_current() -> bool:
    if not available():
        return False
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        return bool(row) and int(row[0]) >= _SCHEMA_VERSION
    except (sqlite3.Error, ValueError):
        return False


def _read_local_meta() -> tuple[int | None, int | None]:
    """(schema_version, built_at epoch seconds) from the local DB's meta
    table, or (None, None) when unavailable/unreadable."""
    if not available():
        return None, None
    try:
        conn = _connect()
        try:
            values = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()
        schema_version = int(values["schema_version"]) if "schema_version" in values else None
        built_at = int(values["built_at"]) if "built_at" in values else None
        return schema_version, built_at
    except (sqlite3.Error, ValueError, KeyError):
        return None, None


def _parse_manifest_built(built: str) -> int | None:
    """Manifest "built" is an ISO-8601 UTC timestamp ("...Z"); returns epoch
    seconds, or None when unparsable."""
    try:
        return int(datetime.strptime(built, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def _fetch_manifest(timeout: int = 15) -> dict | None:
    """Cheap (<1 KB) fetch of latest.json. None on any network/parse failure
    or a manifest missing a required field — callers treat that exactly like
    "no update available", never like an error to propagate."""
    try:
        raw = _download(MANIFEST_URL, timeout=timeout)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.debug("Anime DB manifest fetch failed: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    required = {"schema_version", "built", "sha256", "url", "bytes"}
    if not required.issubset(data.keys()):
        logger.debug("Anime DB manifest missing required fields: %s", data)
        return None
    return data


_MANIFEST_INVALID = "invalid"          # unparsable schema_version/built
_MANIFEST_SCHEMA_MISMATCH = "schema_mismatch"  # refuse artifact, build locally
_MANIFEST_UP_TO_DATE = "up_to_date"    # schema matches, nothing newer to pull
_MANIFEST_NEWER = "newer"              # schema matches, artifact is newer


def _manifest_state(manifest: dict) -> tuple[str, int | None]:
    """(state, artifact_schema_version). Schema must match this build's
    _SCHEMA_VERSION EXACTLY — a client running a newer schema than the
    published artifact refuses it (build locally instead); an older client
    refuses a newer artifact too, since it may not understand new columns.
    Either direction is _MANIFEST_SCHEMA_MISMATCH, deliberately kept distinct
    from _MANIFEST_UP_TO_DATE so callers never confuse "refuse this artifact
    and build locally" with "nothing to do, we're already current"."""
    try:
        artifact_schema = int(manifest["schema_version"])
    except (TypeError, ValueError, KeyError):
        return _MANIFEST_INVALID, None
    if artifact_schema != _SCHEMA_VERSION:
        logger.info("Anime DB artifact schema %s != local %s — refusing artifact.",
                    artifact_schema, _SCHEMA_VERSION)
        return _MANIFEST_SCHEMA_MISMATCH, artifact_schema
    artifact_built = _parse_manifest_built(str(manifest.get("built", "")))
    if artifact_built is None:
        return _MANIFEST_INVALID, artifact_schema
    local_schema, local_built = _read_local_meta()
    if (local_schema == artifact_schema and local_built is not None
            and local_built >= artifact_built):
        return _MANIFEST_UP_TO_DATE, artifact_schema
    return _MANIFEST_NEWER, artifact_schema


def check_for_manifest_update() -> bool:
    """Cheap check ONLY: fetch latest.json and compare against local meta.
    Never downloads the (multi-MB) artifact itself. True means a newer,
    schema-compatible artifact is published — the caller can then trigger a
    real refresh immediately instead of waiting for ensure_fresh()'s normal
    7-day staleness gate (Task I deliverable 4: wired next to the updater's
    own check_for_update() call)."""
    if not config.ANIME_DB_MANIFEST_ENABLED:
        return False
    manifest = _fetch_manifest()
    if manifest is None:
        return False
    state, _schema = _manifest_state(manifest)
    return state == _MANIFEST_NEWER


def _mappings_count(dest: Path) -> int:
    """Row count of the swapped-in DB's `mappings` table. A bundled artifact
    (the normal case since Cole's 2026-07-19 decision) arrives with this
    already populated; a manami-only artifact (an older publish, or a
    degraded CI build — see build_manami_artifact) arrives with it empty.
    Any read failure counts as 0 — safer to attempt the fallback merge than
    silently skip it."""
    try:
        conn = sqlite3.connect(str(dest))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0])
        finally:
            conn.close()
    except (sqlite3.Error, TypeError):
        return 0


def _atomic_swap(tmp: Path, dest: Path) -> None:
    """os.replace is atomic on both platforms, but stale -wal/-shm sidecars
    left over from the PREVIOUS database would otherwise apply to the new
    file's contents on next open — drop them first."""
    for suffix in ("-wal", "-shm"):
        dest.with_name(dest.name + suffix).unlink(missing_ok=True)
    os.replace(tmp, dest)


def _merge_local_id_sources(dest: Path) -> None:
    """Fallback client-side merge of Fribb + Anime-Lists onto an
    already-swapped artifact that came down WITHOUT bundled mappings — an
    older publish, or a CI run that degraded when a mapping source was
    unreachable at build time (the real weekly artifact bundles all three
    sources since Cole's 2026-07-19 decision; see _refresh_from_manifest,
    which only calls this when the swapped-in mappings table is empty).
    Failure here does NOT undo the swap: the manami tables are already a
    strict improvement over whatever was there before, and the next refresh
    (weekly tick or the next manifest poll) retries the merge."""
    fribb = _fetch_fribb()
    xml_root = _fetch_anime_lists_xml()
    if not fribb and xml_root is None:
        return
    conn = _connect(dest)
    try:
        if fribb:
            _merge_fribb_mappings(conn, fribb)
        _merge_anime_lists_xml(conn, xml_root)
        conn.commit()
    finally:
        conn.close()


def _refresh_from_manifest() -> str | None:
    """Try the published weekly artifact. Returns a summary string on
    success, or None when the manifest path can't be used at all — the
    caller then falls back to the existing full local build. EVERY failure
    up to and including the swap leaves the current DB completely untouched
    (nothing is written to the live path until sha256 + a post-decompress
    schema sanity check both pass)."""
    manifest = _fetch_manifest()
    if manifest is None:
        return None
    state, artifact_schema = _manifest_state(manifest)
    if state in (_MANIFEST_INVALID, _MANIFEST_SCHEMA_MISMATCH):
        # Malformed manifest: treat like unreachable. Schema mismatch: the
        # caller (refresh()) must fall through to a full local build, NOT
        # report "already fresh" — those are very different outcomes.
        return None
    if state == _MANIFEST_UP_TO_DATE:
        return "anime metadata already fresh (artifact)"

    try:
        gz_bytes = _download(str(manifest["url"]), timeout=180)
    except Exception as exc:
        logger.warning("Anime DB artifact download failed: %s", exc)
        return None

    expected_sha = str(manifest.get("sha256", "")).strip().lower()
    actual_sha = hashlib.sha256(gz_bytes).hexdigest()
    if not expected_sha or actual_sha != expected_sha:
        logger.warning("Anime DB artifact sha256 mismatch (expected %s, got %s) "
                       "— discarding download.", expected_sha, actual_sha)
        return None

    try:
        db_bytes = gzip.decompress(gz_bytes)
    except OSError as exc:
        logger.warning("Anime DB artifact failed to decompress: %s", exc)
        return None

    dest = _db_path()
    tmp = dest.with_suffix(".manifest-tmp")
    try:
        tmp.write_bytes(db_bytes)
        # Belt-and-braces: the sha256 covers the gz, this confirms the
        # decompressed payload actually IS a DB on the schema we verified
        # against the manifest, before it ever touches the live path.
        conn = sqlite3.connect(str(tmp))
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        if not row or int(row[0]) != _SCHEMA_VERSION:
            raise ValueError(f"decompressed artifact schema mismatch: {row}")
    except Exception as exc:
        logger.warning("Anime DB artifact failed post-download validation: %s", exc)
        tmp.unlink(missing_ok=True)
        return None

    _atomic_swap(tmp, dest)
    summary = f"anime metadata replaced from published artifact (schema {artifact_schema})"
    logger.info(summary)

    # Bundled artifacts (the normal case) already carry Fribb/Anime-Lists
    # mappings — fetching and merging them again would be pure redundant
    # network traffic. Only a manami-only artifact (empty mappings table)
    # needs the client-side fallback merge.
    if _mappings_count(dest) == 0:
        try:
            _merge_local_id_sources(dest)
        except Exception:
            logger.exception("Anime DB local id-mapping merge failed (non-fatal — "
                             "manami tables are already live).")
    else:
        logger.debug("Anime DB artifact already bundles id mappings — "
                     "skipping the redundant local merge.")
    return summary


def refresh(force: bool = False) -> str:
    """Download the dumps and rebuild the local database. Returns a summary.

    Serialized by a lock; concurrent callers wait then return fresh-enough.
    A failed download/build leaves the previous database untouched.

    Manifest-first (Task I): when config.ANIME_DB_MANIFEST_ENABLED, try the
    weekly-published artifact before hitting all three upstream sources
    ourselves. Falls back to the full local build when the manifest is
    unreachable, malformed, or schema-incompatible — self-hosters who
    distrust the artifact can disable the manifest path entirely.
    """
    with _REFRESH_LOCK:
        if (not force and available() and _schema_current()
                and time.time() - _db_path().stat().st_mtime < _MAX_AGE_S):
            return "anime metadata already fresh"

        if config.ANIME_DB_MANIFEST_ENABLED:
            try:
                summary = _refresh_from_manifest()
            except Exception:
                logger.exception("Anime DB artifact refresh raised unexpectedly "
                                 "— falling back to a local build.")
                summary = None
            if summary is not None:
                return summary
            logger.info("Anime DB artifact unavailable or refused — building locally.")

        manami = fetch_manami()
        fribb = _fetch_fribb()
        xml_root = _fetch_anime_lists_xml()

        _build_database(manami, fribb, xml_root, _db_path())
        summary = (f"anime metadata rebuilt: {len(manami.get('data', []))} entries, "
                   f"{len(fribb)} id mappings")
        logger.info(summary)
        return summary


def ensure_fresh(*, background: bool = True, force: bool = False) -> None:
    """Refresh when stale/missing. background=True never blocks the caller.

    force=True skips the local staleness gate and always attempts a refresh
    (still safe: refresh() itself never touches the live DB on failure) —
    used by the cheap manifest-only check (Task I deliverable 4) once it has
    already confirmed a newer artifact is published, instead of waiting for
    the normal 7-day age gate below to catch up.
    """
    if not force and (available() and _schema_current()
            and time.time() - _db_path().stat().st_mtime < _MAX_AGE_S):
        return
    if background:
        threading.Thread(target=lambda: _safe_refresh(force=force),
                         name="anime-db-refresh", daemon=True).start()
    else:
        _safe_refresh(force=force)


def _safe_refresh(force: bool = False) -> None:
    try:
        refresh(force=force)
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


def duration_for_anidb(anidb_id: int | str) -> float | None:
    """Typical episode runtime in minutes (from the manami dump), or None.
    Tolerates databases built before the duration column existed."""
    if not available():
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT duration_min FROM anime WHERE anidb_id = ?",
            (int(anidb_id),)).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-duration schema — next weekly refresh adds it
    finally:
        conn.close()
    return float(row[0]) if row and row[0] else None


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
