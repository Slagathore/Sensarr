# =============================================================================
# download_manager.py
# =============================================================================
# Orchestrates torrent downloads end-to-end:
#
#   grab(result) → downloads row → Node webtorrent runner (subprocess, JSONL
#   protocol) → staging dir → post-process (optional rename + move into the
#   routed library folder) → history rows for every download/rename/move.
#
# Seeding: the runner destroys its client the moment the torrent completes,
# so seeding stops automatically.
#
# Safety rules (per Cole):
#   - Everything downloads into ONE staging directory first.
#   - Files only move when the route is confident AND move is enabled (either
#     the auto_move flag set at grab time, or the admin's Apply Route click).
#   - Rename only applies to parsed-episode files with a confident show match.
#   - Every rename/move writes a before/after history row.
# =============================================================================

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable

import config
import downloads_store
import media_identity
import media_quality
import queue_store
import show_tracker
import shows_store
import size_match
import torrent_routing
import torrent_select
import verification
from media_identity import MediaIdentity
from queue_store import get_request, list_requests
from torrent_search import TorrentResult, search_collect, search_torrents
from torrent_select import SelectWant

logger = logging.getLogger(__name__)


def runner_install_dir() -> Path:
    """Where a WRITABLE torrent_runner (script + node_modules) belongs.

    Windows keeps the historical location beside the exe/source. On Linux a
    packaged install sits under a read-only prefix, so the writable copy
    lives in the DATA dir instead (npm install runs there)."""
    import app_paths
    if sys.platform == "win32":
        return app_paths.PATHS.install_dir / "torrent_runner"
    if getattr(sys, "frozen", False):
        return app_paths.PATHS.data_dir / "torrent_runner"
    return app_paths.PATHS.install_dir / "torrent_runner"


def _resolve_runner_path() -> Path:
    """Locate download.mjs across the layouts it can live in.

    - From source: <repo>/torrent_runner/download.mjs (install dir == repo).
    - Frozen build: node_modules can only sit beside the script, so we prefer
      the writable runner dir (beside the exe on Windows, DATA dir on Linux);
      PyInstaller also bundles a read-only copy under _internal, and older
      builds used BUNDLE_DIR — check those as fallbacks so the path always
      resolves even if the writable folder wasn't seeded.
    """
    import app_paths
    candidates = [
        runner_install_dir() / "download.mjs",
        app_paths.PATHS.install_dir / "_internal" / "torrent_runner" / "download.mjs",
        app_paths.PATHS.bundle_dir / "torrent_runner" / "download.mjs",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]  # default; surfaced as a clear error at grab time


_RUNNER_PATH = _resolve_runner_path()

# Runner readiness is checked once per process, then cached: a grab must not
# shell out to npm on every attempt.
_RUNNER_READY: bool | None = None
_RUNNER_READY_LOCK = threading.Lock()


def runner_missing_deps(runner_dir: Path | None = None) -> bool:
    """True when the runner script is there but its node_modules are not.

    This is the state a fresh install lands in: the packaged app ships
    download.mjs but never node_modules (they are platform-specific native
    builds), so the first grab used to die with a raw ERR_MODULE_NOT_FOUND and
    'runner exit code 1'.
    """
    runner_dir = runner_dir or runner_install_dir()
    script = runner_dir / "download.mjs"
    if not script.is_file():
        # A packaged install may not have seeded the writable copy yet — that
        # is still "deps missing" as far as the caller is concerned.
        return True
    return not (runner_dir / "node_modules" / "webtorrent").is_dir()


def seed_runner_dir(runner_dir: Path | None = None) -> Path:
    """Copy the runner scripts out of the read-only bundle into the writable
    runner dir (packaged installs: the bundle may sit under a read-only prefix,
    and npm needs somewhere it can write node_modules)."""
    runner_dir = runner_dir or runner_install_dir()
    if (runner_dir / "download.mjs").is_file():
        return runner_dir
    import app_paths
    sources = [
        app_paths.PATHS.install_dir / "_internal" / "torrent_runner",
        app_paths.PATHS.bundle_dir / "torrent_runner",
        app_paths.PATHS.install_dir / "torrent_runner",
    ]
    bundled = next((s for s in sources if (s / "download.mjs").is_file()), None)
    if bundled is None:
        return runner_dir
    runner_dir.mkdir(parents=True, exist_ok=True)
    for name in ("download.mjs", "diag.mjs", "package.json", "package-lock.json"):
        src = bundled / name
        if src.is_file():
            shutil.copy2(src, runner_dir / name)
    logger.info("Seeded torrent runner into %s from %s", runner_dir, bundled)
    return runner_dir


def ensure_runner_ready(*, timeout: int = 600) -> tuple[bool, str]:
    """Make the Node runner usable, installing its dependencies if needed.

    Called automatically before the first grab of a process (and by the setup
    wizard's button, which shares this one implementation). Returns
    (ok, message); a False result carries a message written for a human, not a
    stack trace, because it lands on the download row the user is staring at.
    """
    global _RUNNER_READY, _RUNNER_PATH
    with _RUNNER_READY_LOCK:
        if _RUNNER_READY:
            return True, "runner ready"

        runner_dir = seed_runner_dir()
        if not (runner_dir / "download.mjs").is_file():
            return False, (
                "the torrent runner script is missing and no bundled copy was "
                f"found to restore it (looked in {runner_dir})")

        if not runner_missing_deps(runner_dir):
            _RUNNER_PATH = runner_dir / "download.mjs"
            _RUNNER_READY = True
            return True, "runner ready"

        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm is None:
            return False, (
                "Node.js 20+ is required for downloads but npm was not found on "
                "PATH. Install Node.js, then either restart Sensarr or use "
                "Settings > Setup wizard > 'npm install torrent runner'.")

        logger.info("Torrent runner dependencies missing — running npm install "
                    "in %s (first run only).", runner_dir)
        try:
            # Task S item 2: real binary, no shell in the middle.
            result = subprocess.run(
                [npm, "install", "--no-audit", "--no-fund"],
                cwd=str(runner_dir), shell=False, capture_output=True,
                text=True, timeout=timeout, creationflags=_CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False, (f"npm install timed out after {timeout}s in "
                           f"{runner_dir} — run it by hand and try again.")
        except OSError as exc:
            return False, f"couldn't run npm install: {exc}"

        if result.returncode != 0 or runner_missing_deps(runner_dir):
            tail = (result.stderr or result.stdout or "")[-300:].strip()
            return False, (f"npm install failed in {runner_dir}: {tail}")

        _RUNNER_PATH = runner_dir / "download.mjs"
        _RUNNER_READY = True
        logger.info("Torrent runner dependencies installed.")
        return True, "runner dependencies installed"


def _size_prefs(media_type: str) -> tuple[float, float, int]:
    """(preferred MB/min, max MB/min, fallback runtime minutes) per type.
    The fallback minutes only apply when no real runtime is known."""
    prefs = {
        "movie": (config.SIZE_PREF_MB_PER_MIN_MOVIE, config.SIZE_MAX_MB_PER_MIN_MOVIE, 120),
        "tv": (config.SIZE_PREF_MB_PER_MIN_TV, config.SIZE_MAX_MB_PER_MIN_TV, 24),
        "anime": (config.SIZE_PREF_MB_PER_MIN_ANIME, config.SIZE_MAX_MB_PER_MIN_ANIME, 24),
        "xanime": (config.SIZE_PREF_MB_PER_MIN_XANIME, config.SIZE_MAX_MB_PER_MIN_XANIME, 24),
    }
    return prefs.get(media_type, (0.0, 0.0, 24))


# A release that already failed for a given episode/request is retried only
# after this long — unless it's the only option, in which case the least-
# recently-failed one goes first (rotating through copies across passes).
FAILED_GRAB_RETRY_AFTER_S = 7 * 24 * 3600

# Multipart movie disc markers ("Movie.cd1.mkv", "Film Part 2", "disc-01").
_MULTIPART_RE = re.compile(
    r"(?:^|[\W_])(?:cd|dvd|disc|disk|part|pt)[\s._-]*(\d{1,2})(?:[\W_]|$)",
    re.IGNORECASE)


def _magnet_hash(magnet: str) -> str:
    m = re.search(r"btih:([A-Za-z0-9]{32,40})", magnet or "")
    return m.group(1).lower() if m else ""


def _prefer_unfailed(results: list[TorrentResult], context_key: str) -> list[TorrentResult]:
    """Drop releases that recently failed for this context; if EVERY option
    has failed, fall back to the least-recently-failed one."""
    fails = downloads_store.failed_grab_times(context_key)
    if not fails:
        return results
    now = time.time()
    fresh = [
        r for r in results
        if fails.get(_magnet_hash(r.magnet), 0.0) < now - FAILED_GRAB_RETRY_AFTER_S
    ]
    if fresh:
        return fresh
    logger.info("Every candidate for %s failed before — retrying the "
                "least-recently-failed copy.", context_key)
    return sorted(results,
                  key=lambda r: fails.get(_magnet_hash(r.magnet), 0.0))[:1]


def _runtime_minutes(show) -> float | None:
    """A tracked show's real per-episode runtime (minutes), when known."""
    runtime = getattr(show, "runtime_min", None) if show is not None else None
    return float(runtime) if runtime and runtime > 0 else None


def _request_movie_minutes(req) -> float | None:
    """Real runtime for a movie request resolved against TMDB, else None."""
    try:
        if (req.media_type == "movie" and req.external_id
                and "themoviedb.org/movie" in (req.external_url or "")):
            from media_lookup import get_tmdb_movie_runtime
            return get_tmdb_movie_runtime(str(req.external_id))
    except Exception:
        logger.debug("Movie runtime lookup failed for request #%s",
                     getattr(req, "request_id", "?"), exc_info=True)
    return None


def _looks_like_episode_release(title: str) -> bool:
    """True when a release title carries show markers (SxxEyy, 3x07,
    'Season 2', 'Episode 5', …) — a MOVIE grab must never take these."""
    parsed = torrent_routing.parse_torrent_name(title)
    return parsed.season is not None or parsed.episode is not None


# NOTE: filter_viable_results + pick_best_result + _prefer_unfailed are
# SUPERSEDED by the pure torrent_select.select_torrent engine (Task B), which
# runs eight reason-coded gates and a versioned score instead of these
# two-signal vetoes. As of Phase 3 NO automatic decision path calls them — a
# grep proves it. They are RETAINED only because their own unit tests exercise
# them directly (tests/test_video_quality.py, tests/test_maintenance_dupes.py)
# and they remain available to any manual/legacy caller. Legacy manual-only.
def filter_viable_results(results: list[TorrentResult], media_type: str,
                          *, block_cams: bool | None = None,
                          minutes: float | None = None) -> list[TorrentResult]:
    """Auto-grab hard filters (vetoes, not preferences):
    - size 0 is never downloaded (unverifiable garbage)
    - cam/telesync releases are dropped for movies when BLOCK_CAMS is on
    - releases with season/episode markers are dropped for MOVIE grabs —
      "Movie Title S01E03" is a show, not the movie someone asked for
    - results whose implied MB/min exceeds the max slider are dropped
    """
    from video_quality import is_cam_release

    block = config.BLOCK_CAMS if block_cams is None else block_cams
    _pref, max_rate, default_minutes = _size_prefs(media_type)
    minutes = minutes if minutes and minutes > 0 else default_minutes

    viable = [r for r in results if r.size_bytes > 0]
    if media_type == "movie":
        if block:
            viable = [r for r in viable if not is_cam_release(r.title)]
        viable = [r for r in viable if not _looks_like_episode_release(r.title)]
    if max_rate > 0:
        cap = max_rate * minutes * 1024 * 1024
        viable = [r for r in viable if r.size_bytes <= cap]
    return viable


def _ascii_preferring_title(resolved: str | None, content: str | None) -> str:
    """Prefer an ASCII title for searching and folder naming. TVDB/TMDB can
    resolve to a native-script primary title (the request for "Pursuit of
    jade" resolved to a kanji title) — searching indexers with that returns
    garbage, and naming folders with it puts kanji directories in a TV root."""
    def ratio(s: str) -> float:
        letters = [c for c in s if not c.isspace()]
        return (sum(1 for c in letters if ord(c) < 128) / len(letters)) if letters else 0.0

    resolved = (resolved or "").strip()
    content = (content or "").strip()
    if resolved and ratio(resolved) >= 0.7:
        return resolved
    if content and ratio(content) >= 0.7:
        return content
    return resolved or content


def _row_search_alias(req) -> str:
    """The SEARCH alias for a request row (Task A item 6 / bootstrap item 0.5).

    Prefers the stored aliases_json[0] — that is the ASCII-preferring alias the
    intake path computed, and for a same-title country edition it is the
    disambiguating form ("Married at First Sight US"), which is exactly what
    auto-grab must query with. Falls back to the live ASCII-preferring
    derivation for legacy rows that predate aliases_json.
    """
    aliases = getattr(req, "aliases", None) or []
    if aliases and str(aliases[0]).strip():
        return str(aliases[0]).strip()
    return _ascii_preferring_title(
        getattr(req, "resolved_title", None), getattr(req, "content", None)).strip()


def _auto_grab_query(req, *, season: int | None = None) -> str:
    """Build the indexer query for a request row from its stored identity.

    Never raw user text, never a native-script canonical title. Movies append
    the canonical year ("Alias YYYY"); a tv season row appends "S{NN}" (the
    "Alias Season N" fallback is the season-pack search's second attempt). The
    tv premiere year is a scoring signal only and is deliberately never a query
    term (release names routinely omit it).
    """
    alias = _row_search_alias(req)
    if not alias:
        return ""
    media_type = getattr(req, "media_type", None)
    if season is not None:
        return f"{alias} S{int(season):02d}"
    if media_type == "movie":
        year = getattr(req, "canonical_year", None)
        if year:
            return f"{alias} {year}"
    return alias


def _request_identity_key(req) -> str:
    """Durable per-pass dedupe key for a request. Qualified rows key on the
    provider identity + season (so two rows resolved to the same tmdb id collapse
    but S01/S02 stay distinct); unqualified rows fall back to the normalised
    title. Used by the grab gate so two duplicate open requests never both
    grab in one pass."""
    if getattr(req, "is_qualified", False):
        return (f"{req.media_type}:{req.identity_source}:"
                f"{req.external_id}:{req.season}")
    import media_identity
    title = getattr(req, "resolved_title", None) or getattr(req, "content", "")
    return f"{req.media_type}:{media_identity.normalize_title(title)}"


def _min_seeders_for(mode: str) -> int:
    """The seeder floor a want should enforce. Every routine mode uses
    config.MIN_SEEDERS; the zero-seeder race disables the gate (0) because
    gambling on unseeded releases is its entire purpose."""
    if mode == torrent_select.MODE_ZERO_SEEDER_RACE:
        return 0
    return config.MIN_SEEDERS


class _AdoptResult:
    """Minimal TorrentResult stand-in for re-freezing a want from an existing
    download row during quarantine adoption (Task C item 8)."""
    def __init__(self, row) -> None:
        self.title = row.title
        self.media_type = row.media_type
        self.magnet = row.magnet
        self.source = row.source
        self.size_bytes = 0
        self.seeders = 0


def _build_want_snapshot(
    result: TorrentResult, req, *, request_title: str | None,
    show_id: int | None, season: int | None, episode: int | None,
    minutes: float | None, identity: MediaIdentity | None = None,
) -> dict:
    """Freeze what this grab is meant to satisfy: the MediaIdentity + the
    season/episode target + the size prefs, at grab time. Stored immutably in
    downloads.want_json; verification/reconciliation/restart READ this instead
    of re-deriving intent from the mutable request row (or the torrent title).

    `identity` supplies the MediaIdentity for grabs with NO request row (the
    identity-aware replacement path, Task F item 4) — the request row wins
    when both exist, because it is what the user actually asked for.
    """
    pref, max_rate, _default_minutes = _size_prefs(result.media_type)
    identity_source = getattr(req, "identity_source", None) if req else None
    external_id = getattr(req, "external_id", None) if req else None
    canonical_title = getattr(req, "resolved_title", None) if req else None
    canonical_year = getattr(req, "canonical_year", None) if req else None
    origin_countries = list(getattr(req, "origin_countries", []) or []) if req else []
    aliases = list(getattr(req, "aliases", []) or []) if req else []
    if req is None and identity is not None:
        identity_source = identity.identity_source
        external_id = identity.external_id
        canonical_title = identity.canonical_title
        canonical_year = identity.canonical_year
        origin_countries = list(identity.origin_countries or ())
        aliases = list(identity.aliases or ())
    # The season target: an explicit episode_context wins, else the request's
    # season, else none.
    want_season = season if season is not None else (
        getattr(req, "season", None) if req else None)
    search = request_title or media_identity.search_alias(canonical_title, None) or result.title
    return {
        "schema": 1,
        "request_id": getattr(req, "request_id", None) if req else None,
        "media_type": result.media_type,
        "identity_source": identity_source,
        "external_id": external_id,
        "canonical_title": canonical_title,
        "canonical_year": canonical_year,
        "origin_countries": origin_countries,
        "aliases": aliases,
        "search_alias": search,
        "show_id": show_id,
        "season": want_season,
        "episode": episode,
        "size_pref_mb_min": pref,
        "size_max_rate": max_rate,
        "runtime_minutes": minutes,
    }


# Verified per-movie folder tag (Task D2) — the offline identity evidence the
# replacement path reads back (Task F item 4).
_TMDB_TAG_RE = re.compile(r"\{tmdb-(\d+)\}")
_FOLDER_YEAR_RE = re.compile(r"\((\d{4})\)")


def identity_from_movie_path(path: str) -> MediaIdentity | None:
    """Resolve a movie's MediaIdentity from its managed per-movie folder name
    (`Title (Year) {tmdb-12345}` — RESOLVED DECISION 4 guarantees the tag was
    verified when written). Checks the parent folder first, then the file stem.
    Returns None when no tag is present — never a guessed identity."""
    p = Path(path)
    for candidate in (p.parent.name, p.stem):
        m = _TMDB_TAG_RE.search(candidate or "")
        if not m:
            continue
        base = _TMDB_TAG_RE.sub("", candidate).strip()
        ym = _FOLDER_YEAR_RE.search(base)
        year = int(ym.group(1)) if ym else None
        title = _FOLDER_YEAR_RE.sub("", base).strip(" .-_") or None
        return MediaIdentity(
            media_type="movie", identity_source="tmdb",
            external_id=m.group(1), canonical_title=title,
            canonical_year=year)
    return None


def _movie_route_identity(req) -> tuple[str | None, int | None, str | None]:
    """(canonical_title, canonical_year, tmdb_id) for per-movie folder routing
    (Task D2). tmdb_id is only returned for a VERIFIED tmdb source — never an
    id from another provider recast as tmdb (RESOLVED DECISION 4)."""
    if req is None:
        return None, None, None
    title = getattr(req, "resolved_title", None)
    year = getattr(req, "canonical_year", None)
    source = getattr(req, "identity_source", None)
    ext = getattr(req, "external_id", None)
    tmdb = str(ext) if (source == "tmdb" and ext) else None
    return title, year, tmdb


def _movie_route_identity_from_want(want: dict | None
                                    ) -> tuple[str | None, int | None, str | None]:
    """Same as _movie_route_identity but from an immutable want_json snapshot —
    the authoritative source at _post_process time."""
    if not want:
        return None, None, None
    title = want.get("canonical_title")
    year = want.get("canonical_year")
    source = want.get("identity_source")
    ext = want.get("external_id")
    tmdb = str(ext) if (source == "tmdb" and ext) else None
    return title, year, tmdb


def _request_title_from_row(row) -> str | None:
    """The single source of the title used for routing/naming a download.

    Reads the immutable want snapshot first (collapsing the three historical
    derivations: restart used the torrent title, _start_row rebuilt from the
    request, apply_route did a third thing). Falls back to the _start_row
    reconstruction for rows grabbed before want_json existed, and finally to
    the torrent row title.
    """
    want_json = getattr(row, "want_json", None)
    if want_json:
        try:
            want = json.loads(want_json)
            title = want.get("search_alias") or want.get("canonical_title")
            if title:
                return title
        except (ValueError, TypeError):
            pass
    if row.request_id is not None:
        req = get_request(row.request_id)
        if req is not None:
            return _ascii_preferring_title(req.resolved_title, req.content)
    return row.title


def pick_best_result(results: list[TorrentResult], media_type: str,
                     *, minutes: float | None = None) -> TorrentResult | None:
    """Best auto-grab candidate honouring the admin's size preference.

    Assumes hard filters (filter_viable_results) already ran. With no
    preference set (0), the top-seeded result wins. With a MB/min target,
    prefer the result whose size lands closest to the target on a log
    scale, seeders as tie-breaker. `minutes` is the real runtime when
    known (tracked-show runtime, TMDB movie runtime, ffprobe of a file
    being replaced); the fallback is 2 h for movies, 24 min for episodes.
    """
    if not results:
        return None
    pref, _max_rate, default_minutes = _size_prefs(media_type)
    if pref <= 0:
        return results[0]

    minutes = minutes if minutes and minutes > 0 else default_minutes
    target_bytes = pref * minutes * 1024 * 1024

    import math

    def sort_key(r: TorrentResult):
        if not r.size_bytes:
            return (99.0, -r.seeders)
        distance = abs(math.log2(r.size_bytes / target_bytes))
        return (distance, -r.seeders)

    return sorted(results, key=sort_key)[0]

# Windows: suppress the console window for the Node subprocess. On POSIX
# creationflags must be 0 (any other value raises in subprocess.Popen).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------
# Public tracker list — appended to every magnet before download. Poorly
# announced magnets (one dead tracker) often go from 0 peers to dozens with
# these. Refreshed daily from ngosang/trackerslist; the baked-in list is the
# fallback when offline.
# ---------------------------------------------------------------------------

_TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
def _trackers_cache_path() -> Path:
    import app_paths
    return app_paths.PATHS.cache_dir / "trackers_cache.txt"


_TRACKERS_CACHE = _trackers_cache_path()
_TRACKERS_MAX_AGE_S = 24 * 3600

_BUILTIN_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.theoks.net:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://opentracker.io:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
]


def _public_trackers() -> list[str]:
    """Current tracker list — cached download of trackers_best, else builtin."""
    try:
        if (not _TRACKERS_CACHE.is_file()
                or time.time() - _TRACKERS_CACHE.stat().st_mtime > _TRACKERS_MAX_AGE_S):
            req = urllib.request.Request(_TRACKERS_URL, headers={"User-Agent": "Sensarr"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                _TRACKERS_CACHE.write_bytes(resp.read())
    except Exception as exc:
        logger.debug("Tracker list refresh failed (using cache/builtin): %s", exc)
    try:
        if _TRACKERS_CACHE.is_file():
            lines = [ln.strip() for ln in _TRACKERS_CACHE.read_text().splitlines()]
            trackers = [ln for ln in lines if ln.startswith(("udp://", "http://", "https://"))]
            if trackers:
                return trackers
    except OSError:
        pass
    return _BUILTIN_TRACKERS


def add_public_trackers(magnet: str) -> str:
    """Append the public tracker list to a magnet URI (skipping ones present)."""
    if not magnet.startswith("magnet:"):
        return magnet
    existing = set(re.findall(r"tr=([^&]+)", magnet))
    extra = ""
    for tr in _public_trackers():
        quoted = urllib.parse.quote(tr, safe="")
        if quoted not in existing and tr not in existing:
            extra += f"&tr={quoted}"
    return magnet + extra


class _QBitClient:
    """Tiny qBittorrent Web API client (urllib only, cookie auth).

    Used when QBITTORRENT_ENABLED is on — downloads are delegated to a
    running qBittorrent instance instead of the built-in webtorrent runner.
    """

    def __init__(self) -> None:
        self._base = config.QBITTORRENT_URL.rstrip("/")
        jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    def _post(self, path: str, data: dict[str, str]) -> str:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self._base + path, data=body)
        with self._opener.open(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _get_json(self, path: str) -> Any:
        with self._opener.open(self._base + path, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def login(self) -> None:
        out = self._post("/api/v2/auth/login", {
            "username": config.QBITTORRENT_USERNAME,
            "password": config.QBITTORRENT_PASSWORD,
        })
        if "Ok" not in out:
            raise RuntimeError("qBittorrent login failed — check URL/username/password in Settings.")

    def add_magnet(self, magnet: str, save_path: str, tag: str) -> None:
        self._post("/api/v2/torrents/add", {
            "urls": magnet, "savepath": save_path, "tags": tag,
            "sequentialDownload": "false",
        })

    def info_by_tag(self, tag: str) -> dict | None:
        torrents = self._get_json(f"/api/v2/torrents/info?tag={urllib.parse.quote(tag)}")
        return torrents[0] if torrents else None

    def files(self, torrent_hash: str) -> list[dict]:
        return self._get_json(f"/api/v2/torrents/files?hash={torrent_hash}") or []

    def set_file_priority(self, torrent_hash: str, file_ids: list[int], priority: int) -> None:
        if not file_ids:
            return
        self._post("/api/v2/torrents/filePrio", {
            "hash": torrent_hash,
            "id": "|".join(str(i) for i in file_ids),
            "priority": str(priority),
        })

    def simple_by_tag(self, action: str, tag: str) -> None:
        """pause / resume (stop/start in qBit 5.x) / recheck by tag."""
        info = self.info_by_tag(tag)
        if info is None or not info.get("hash"):
            return
        endpoints = {"pause": ("/api/v2/torrents/stop", "/api/v2/torrents/pause"),
                     "resume": ("/api/v2/torrents/start", "/api/v2/torrents/resume"),
                     "recheck": ("/api/v2/torrents/recheck",)}
        for path in endpoints.get(action, ()):
            try:
                self._post(path, {"hashes": str(info["hash"])})
                return
            except Exception:
                continue

    def delete_by_tag(self, tag: str, *, delete_files: bool) -> None:
        info = self.info_by_tag(tag)
        if info is None:
            return
        self._post("/api/v2/torrents/delete", {
            "hashes": info.get("hash", ""),
            "deleteFiles": "true" if delete_files else "false",
        })


@dataclass(frozen=True)
class ManualGrabOutcome:
    """The result of a manual-pick preflight (DownloadManager.manual_grab).

    ok            : the grab started (either it passed every gate, or the caller
                    supplied a typed override).
    needs_override: a gate rejected the pick and the caller must confirm a typed
                    override to proceed; nothing was grabbed.
    reason_code   : the rejecting gate ('ok' when it passed cleanly).
    selection_run_id: the persisted decision (always recorded).
    """
    ok: bool
    download_id: int | None
    reason_code: str
    detail: str
    selection_run_id: int | None
    needs_override: bool


class DownloadManager:
    """Owns runner subprocesses and post-processing. One instance per app."""

    def __init__(self, *, on_update: Callable[[int], None] | None = None) -> None:
        # on_update(download_id) is called (from worker threads!) whenever a
        # download's row changed — the desktop app marshals it to the UI.
        self._on_update = on_update
        self._processes: dict[int, subprocess.Popen] = {}
        # download_id → qBittorrent tag, for cancel when delegating to qBit.
        self._qbit_tags: dict[int, str] = {}
        self._lock = threading.Lock()
        # Queue bookkeeping: last observed (progress, when) per active row,
        # and when a slow download was last rotated (sorts it to the back).
        self._progress_seen: dict[int, tuple[float, float]] = {}
        self._rotate_cooldown: dict[int, float] = {}
        # Consecutive no-progress rotations per download; at DOWNLOAD_MAX_ROTATIONS
        # the row is declared stalled ('error') instead of looping forever.
        self._rotation_count: dict[int, int] = {}
        # Transient live phase for the queue UI (fetching_metadata / no_peers),
        # fed from the runner's own events; lost on restart by design.
        self._phase: dict[int, str] = {}
        # Downloads currently in a zero-seeder race, shown as 'probing'.
        self._probing_ids: set[int] = set()
        # Task G: one derived SizePick per show PER PASS (the library sample is
        # re-read at the start of each auto-grab pass, not on every candidate).
        self._size_pick_cache: dict[int, size_match.SizePick] = {}
        downloads_store.initialize_downloads_db()
        self._recover_previous_session()
        threading.Thread(target=self._queue_monitor, name="dl-queue-monitor",
                         daemon=True).start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def grab(
        self,
        result: TorrentResult,
        *,
        request_id: int | None = None,
        request_title: str | None = None,
        auto_rename: bool | None = None,
        auto_move: bool | None = None,
        episode_context: tuple[int, int, int] | None = None,  # (show_id, season, episode)
        season_context: tuple[int, int] | None = None,  # (show_id, season) for a pack
        replace_path: str | None = None,  # old low-quality file to delete after move
        failure_key: str | None = None,   # failure-memory context for pack grabs
        quality_label: str | None = None,  # Task F: from the decision's parse
        identity_override: MediaIdentity | None = None,  # want identity w/o a request
    ) -> int:
        """Start downloading a search result. Returns the download row id."""
        auto_rename = config.TORRENT_AUTO_RENAME if auto_rename is None else auto_rename
        auto_move = config.TORRENT_AUTO_MOVE if auto_move is None else auto_move
        # Task F: the automatic paths hand in the label the selection engine
        # already parsed on the chosen candidate; manual/legacy entry points
        # fall back to one RTN parse of the release title here.
        if quality_label is None:
            quality_label = torrent_select.parse_quality_label(result.title)

        show_id = season = episode = None
        req = get_request(request_id) if request_id is not None else None
        if episode_context is not None:
            show_id, season, episode = episode_context
            show = shows_store.get_show(show_id)
            plan = (
                show_tracker.plan_for_episode(show, season, episode)
                if show is not None
                else torrent_routing.plan_route(result.title, result.media_type)
            )
        elif season_context is not None:
            # Request-linked season pack — route by show_id (Task D item 1),
            # episode left None (the pack carries many, named per-file at move).
            show_id, season = season_context
            show = shows_store.get_show(show_id)
            plan = (
                show_tracker.plan_for_season(show, season)
                if show is not None
                else torrent_routing.plan_route(
                    result.title, result.media_type, request_title=request_title)
            )
        elif result.media_type == "movie":
            mt, my, mtmdb = _movie_route_identity(req)
            if req is None and identity_override is not None:
                # Identity-aware replacement (Task F item 4): the resolved
                # identity drives the per-movie folder exactly like a request.
                mt = identity_override.canonical_title
                my = identity_override.canonical_year
                mtmdb = (str(identity_override.external_id)
                         if (identity_override.identity_source == "tmdb"
                             and identity_override.external_id) else None)
            plan = torrent_routing.plan_route(
                result.title, result.media_type, request_title=request_title,
                movie_title=mt, movie_year=my, movie_tmdb_id=mtmdb)
        else:
            plan = torrent_routing.plan_route(
                result.title, result.media_type, request_title=request_title
            )
        staging = Path(config.TORRENT_DOWNLOAD_DIR)
        staging.mkdir(parents=True, exist_ok=True)

        # Freeze the want snapshot from the request row as it stands NOW
        # (req was already fetched above for movie/season routing).
        minutes = _request_movie_minutes(req) if (
            req is not None and result.media_type == "movie") else None
        want = _build_want_snapshot(
            result, req, request_title=request_title,
            show_id=show_id, season=season, episode=episode, minutes=minutes,
            identity=identity_override)

        download_id = downloads_store.create_download(
            title=result.title, magnet=result.magnet, source=result.source,
            media_type=result.media_type, request_id=request_id,
            staging_dir=str(staging),
            planned_dest=plan.dest_dir if plan.confident else None,
            planned_name=plan.new_filename,
            route_reason=plan.reason,
            auto_rename=auto_rename, auto_move=auto_move,
            show_id=show_id, season=season, episode=episode,
            replace_path=replace_path, failure_key=failure_key,
            want_json=json.dumps(want), quality_label=quality_label,
        )
        if episode_context is not None:
            shows_store.set_episode_grab(
                episode_context[0], episode_context[1], episode_context[2], download_id,
            )
        # Provenance junction from the moment of grab (Task C item 3/4) so a
        # season request's several episode downloads all trace back to it.
        if request_id is not None:
            role = ("episode" if episode is not None
                    else "season_pack" if season is not None else "movie")
            downloads_store.link_request_download(request_id, download_id, role)
            # The request now has a grab in flight — mark it so, so it drops out
            # of the auto-grab 'open' pool and the UI shows it as grabbing rather
            # than idle-open. Only advance from a grabbable state; never yank a
            # request that is already placed/fulfilled/needs_attention.
            if req is not None and req.status in (
                    queue_store.STATUS_OPEN, queue_store.STATUS_DEFERRED):
                queue_store.set_status(request_id, queue_store.STATUS_GRABBING)

        if config.QBITTORRENT_ENABLED:
            # qBittorrent has its own queue/active limits — hand over directly.
            threading.Thread(
                target=self._run_download,
                args=(download_id, result.magnet, str(staging), request_title),
                name=f"torrent-dl-{download_id}", daemon=True,
            ).start()
        else:
            # Built-in engine: rows start as 'queued'; the pump runs at most
            # MAX_ACTIVE_DOWNLOADS at once and rotates stale ones out.
            self._maybe_start_next()
        return download_id

    def manual_grab(
        self, result: TorrentResult, *,
        request_id: int | None = None, request_title: str | None = None,
        auto_rename: bool | None = None, auto_move: bool | None = None,
        episode_context: tuple[int, int, int] | None = None,
        override_reason: str | None = None,
    ) -> "ManualGrabOutcome":
        """Manual user pick (mode manual-user-pick): run the hard gates as a
        PREFLIGHT before grabbing.

        The user's explicit choice is never re-scored away, but a gate rejection
        (a sequel/identity/country mismatch, a CAM, an oversize pick, …) is
        surfaced. The pick proceeds only when it passes every gate OR the caller
        supplies a typed `override_reason`, which is recorded in the selection
        run's pool_stats so the override is auditable. Selection provenance is
        persisted either way.
        """
        media_type = result.media_type
        req = get_request(request_id) if request_id is not None else None
        if req is not None:
            want = self._want_from_request(
                req, media_type, mode=torrent_select.MODE_MANUAL_USER_PICK)
        elif episode_context is not None:
            show = shows_store.get_show(episode_context[0])
            if show is not None:
                want = self._want_for_show_episode(
                    show, episode_context[1], episode_context[2],
                    _runtime_minutes(show),
                    torrent_select.MODE_MANUAL_USER_PICK)
            else:
                want = self._race_fallback_want(media_type, None)
        else:
            pref, max_rate, default_minutes = _size_prefs(media_type)
            want = SelectWant(
                identity=MediaIdentity(media_type=media_type,
                                       canonical_title=request_title or None),
                size_pref_mb_min=pref, size_max_rate=max_rate,
                fallback_minutes=default_minutes,
                allow_cam=not config.BLOCK_CAMS,
                mode=torrent_select.MODE_MANUAL_USER_PICK)

        decision, by_hash = self._run_selection([result], want)
        chosen = by_hash.get(decision.chosen_infohash) if decision.chosen else None
        verdict = decision.verdicts[0] if decision.verdicts else None
        reason = verdict.reason_code if verdict is not None else "no_candidate"
        detail = verdict.detail if verdict is not None else ""

        if chosen is None and not override_reason:
            # A gate rejected the pick and the user has not confirmed an
            # override: refuse, persist the refusal so it is visible.
            run_id = downloads_store.record_selection_run(
                decision, request_id=request_id)
            return ManualGrabOutcome(
                ok=False, download_id=None, reason_code=reason, detail=detail,
                selection_run_id=run_id, needs_override=True)

        if chosen is None and override_reason:
            # Record the typed override on the decision before persisting.
            decision.pool_stats["manual_override"] = override_reason
            decision.pool_stats["overridden_reason_code"] = reason

        download_id = self.grab(
            result, request_id=request_id, request_title=request_title,
            auto_rename=auto_rename, auto_move=auto_move,
            episode_context=episode_context)
        run_id = downloads_store.record_selection_run(
            decision, request_id=request_id, download_id=download_id)
        return ManualGrabOutcome(
            ok=True, download_id=download_id,
            reason_code=("ok" if chosen is not None else reason), detail=detail,
            selection_run_id=run_id, needs_override=False)

    # ------------------------------------------------------------------
    # Crash/restart recovery
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Graceful close: kill our runner processes and requeue their rows
        (progress kept — webtorrent re-verifies on resume). Without this,
        runners survive as headless orphans that download into staging and
        hold file locks until the next launch's recovery sweep."""
        with self._lock:
            procs = dict(self._processes)
            self._processes.clear()
        for download_id, proc in procs.items():
            try:
                if proc.poll() is None:
                    proc.kill()
                    downloads_store.set_status(download_id, "queued")
                    downloads_store.add_history(
                        download_id, "requeued",
                        before="downloading",
                        after="app closing — will resume next launch")
            except Exception:
                logger.exception("Shutdown: failed to stop runner #%s", download_id)
        if procs:
            logger.info("Shutdown: stopped %d active runner(s); rows requeued.",
                        len(procs))

    def _recover_previous_session(self) -> None:
        """Clean up after a previous app session.

        Node runners are detached children — they SURVIVE the app closing,
        keep downloading into staging, and hold file locks, while their rows
        sit frozen at 'downloading' in the new session (the 95%%-zombie
        symptom). Kill any leftover runner of ours and requeue those rows;
        existing data is verified and kept on restart.
        """
        killed = 0
        try:
            import psutil
            runner_marker = _RUNNER_PATH.name  # download.mjs
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    if (proc.info["name"] or "").lower() not in ("node.exe", "node"):
                        continue
                    cmdline = " ".join(proc.info["cmdline"] or [])
                    if runner_marker in cmdline and str(
                            Path(config.TORRENT_DOWNLOAD_DIR)) in cmdline:
                        proc.kill()
                        killed += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            logger.exception("Orphan runner sweep failed.")
        if killed:
            logger.info("Recovery: killed %d orphaned torrent runner(s) from a "
                        "previous session.", killed)

        requeued = 0
        finish_moves: list[int] = []
        for row in downloads_store.list_downloads(limit=300):
            if row.status == "downloading":
                downloads_store.set_status(row.download_id, "queued")
                downloads_store.add_history(
                    row.download_id, "recovered", before=None,
                    after="app restarted — requeued (existing data is kept)")
                requeued += 1
            elif row.status == "downloaded" and (row.auto_move or row.auto_rename):
                # Post-process was interrupted (a cross-drive move can leave
                # a PARTIAL copy at the destination) — finish it now.
                finish_moves.append(row.download_id)
        if requeued:
            logger.info("Recovery: requeued %d download(s) from the previous "
                        "session.", requeued)
        if finish_moves:
            def finisher() -> None:
                for did in finish_moves:
                    try:
                        outcome = self.apply_route(did)
                        logger.info("Recovery: finished post-process for #%s: %s",
                                    did, outcome)
                    except Exception:
                        logger.exception("Recovery post-process failed for #%s", did)
            threading.Thread(target=finisher, name="dl-recovery-finish",
                             daemon=True).start()
        self._maybe_start_next()

    # ------------------------------------------------------------------
    # Built-in engine queue: cap concurrent downloads, rotate stalled ones
    # ------------------------------------------------------------------

    def _active_node_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._processes.values() if p.poll() is None)

    def _maybe_start_next(self) -> None:
        """Start queued downloads while there's headroom (node engine only)."""
        if config.QBITTORRENT_ENABLED:
            return
        budget = max(1, config.MAX_ACTIVE_DOWNLOADS) - self._active_node_count()
        if budget <= 0:
            return
        queued = sorted(
            (r for r in downloads_store.list_downloads(limit=300)
             if r.status == "queued"),
            # Recently-rotated slowpokes go to the back of the line.
            key=lambda r: (self._rotate_cooldown.get(r.download_id, 0), r.download_id),
        )
        for row in queued[:budget]:
            with self._lock:
                if row.download_id in self._processes:
                    continue
            self._start_row(row)

    def _start_row(self, row: downloads_store.DownloadRow) -> None:
        downloads_store.set_status(row.download_id, "downloading")
        request_title = _request_title_from_row(row)
        threading.Thread(
            target=self._run_download,
            args=(row.download_id, row.magnet,
                  row.staging_dir or config.TORRENT_DOWNLOAD_DIR, request_title),
            name=f"torrent-dl-{row.download_id}", daemon=True,
        ).start()
        self._notify(row.download_id)

    def _queue_monitor(self) -> None:
        """Every minute: pump the queue, and rotate out any active download
        that hasn't moved in DOWNLOAD_SLOW_ROTATE_MINUTES while others wait."""
        while True:
            time.sleep(60)
            try:
                self._queue_monitor_pass()
            except Exception:
                logger.exception("Download queue monitor pass failed.")

    def _queue_monitor_pass(self) -> None:
        """One monitor tick: find no-progress downloads and rotate them, or —
        after DOWNLOAD_MAX_ROTATIONS — declare them stalled. Extracted from the
        sleep loop so the stall/rotate decision is unit-testable."""
        if config.QBITTORRENT_ENABLED:
            return
        rows = downloads_store.list_downloads(limit=300)
        queued_waiting = any(r.status == "queued" for r in rows)
        now = time.time()
        for row in rows:
            if row.status != "downloading":
                self._progress_seen.pop(row.download_id, None)
                self._rotation_count.pop(row.download_id, None)
                continue
            seen = self._progress_seen.get(row.download_id)
            if seen is None or seen[0] != row.progress:
                # Real progress since last look — reset the stall bookkeeping.
                self._progress_seen[row.download_id] = (row.progress, now)
                self._rotation_count.pop(row.download_id, None)
                continue
            stale_s = now - seen[1]
            # Never rotate before the Node runner's own stall timeout could have
            # fired: for a truly dead swarm the runner errors first (at
            # TORRENT_STALL_TIMEOUT_SECONDS), and only a download the runner has
            # NOT already killed reaches a monitor rotation.
            window = max(config.DOWNLOAD_SLOW_ROTATE_MINUTES * 60,
                         config.TORRENT_STALL_TIMEOUT_SECONDS + 60)
            if stale_s > (window if queued_waiting else window * 3):
                self._rotate_or_stall(row, stale_s, now)
        self._maybe_start_next()

    def _rotate_or_stall(self, row: downloads_store.DownloadRow,
                         stale_s: float, now: float) -> None:
        """A download made no progress for a full window. Requeue it, or — once
        it has burned through DOWNLOAD_MAX_ROTATIONS — mark it 'error' with a
        stalled reason. The 'error' status flows into downloads_store.set_status,
        which records the failure and (workstream A) resolves the request so the
        next auto-grab pass picks a DIFFERENT release instead of the dead one."""
        count = self._rotation_count.get(row.download_id, 0) + 1
        with self._lock:
            proc = self._processes.get(row.download_id)
        if proc is not None and proc.poll() is None:
            proc.kill()
        if count >= config.DOWNLOAD_MAX_ROTATIONS:
            reason = (f"stalled: no progress after {count} rotations "
                      f"({stale_s / 60:.0f} min idle)")
            logger.info("Download #%s %s — marking error.", row.download_id, reason)
            downloads_store.set_status(
                row.download_id, "error", error=reason, completed=True)
            downloads_store.add_history(
                row.download_id, "error", before=None, after=reason)
            self._rotation_count.pop(row.download_id, None)
            self._rotate_cooldown.pop(row.download_id, None)
        else:
            logger.info(
                "Download #%s made no progress for %.0f min — rotating back to "
                "the queue (rotation %d/%d).", row.download_id, stale_s / 60,
                count, config.DOWNLOAD_MAX_ROTATIONS)
            downloads_store.set_status(row.download_id, "queued")
            downloads_store.add_history(
                row.download_id, "rotated", before=None,
                after=f"no progress for {stale_s / 60:.0f} min — "
                      f"requeued (rotation {count})")
            self._rotation_count[row.download_id] = count
            self._rotate_cooldown[row.download_id] = now
        self._progress_seen.pop(row.download_id, None)
        self._phase.pop(row.download_id, None)
        self._notify(row.download_id)

    def _update_phase(self, download_id: int, progress: float, peers: int,
                      metadata_seen: bool) -> None:
        """Translate a runner progress event into the queue-UI phase: real
        progress clears it, no metadata yet keeps 'fetching_metadata', and a
        connected-but-peerless swarm shows 'no peers'."""
        if progress > 0:
            self._phase.pop(download_id, None)
        elif not metadata_seen:
            self._phase[download_id] = "fetching_metadata"
        elif peers <= 0:
            self._phase[download_id] = "no_peers"
        else:
            self._phase.pop(download_id, None)

    def phase_for(self, download_id: int) -> str | None:
        """The transient queue phase for a download, for the UI's honest status
        label. A racing candidate reads 'probing'; otherwise the live
        metadata/peers phase (or None when nothing special is happening)."""
        if download_id in self._probing_ids:
            return "probing"
        return self._phase.get(download_id)

    def cancel(self, download_id: int) -> bool:
        with self._lock:
            proc = self._processes.get(download_id)
            qbit_tag = self._qbit_tags.get(download_id)
        if qbit_tag is not None:
            try:
                client = _QBitClient()
                client.login()
                client.delete_by_tag(qbit_tag, delete_files=True)
            except Exception:
                logger.exception("qBittorrent cancel failed for #%s", download_id)
                return False
        elif proc is None or proc.poll() is not None:
            row = downloads_store.get_download(download_id)
            if row is None or row.status != "queued":
                return False
            # Still waiting in the queue — cancelling is just a status flip.
        else:
            proc.kill()
        downloads_store.set_status(download_id, "cancelled", completed=True)
        downloads_store.add_history(download_id, "cancelled", before=None, after=None)
        self._notify(download_id)
        self._maybe_start_next()
        return True

    # ------------------------------------------------------------------
    # Torrent-client-style operations (Downloads tab right-click menu)
    # ------------------------------------------------------------------

    def stop(self, download_id: int) -> str:
        """Pause/stop a running download (restart resumes — both engines
        verify existing data, so no progress is lost)."""
        with self._lock:
            proc = self._processes.get(download_id)
            qbit_tag = self._qbit_tags.get(download_id)
        if qbit_tag is not None:
            try:
                client = _QBitClient()
                client.login()
                client.simple_by_tag("pause", qbit_tag)
            except Exception as exc:
                return f"qBittorrent pause failed: {exc}"
        elif proc is not None and proc.poll() is None:
            proc.kill()
        else:
            return "not running"
        downloads_store.set_status(download_id, "stopped")
        downloads_store.add_history(download_id, "stopped", before=None, after=None)
        self._notify(download_id)
        return "stopped"

    def restart(self, download_id: int) -> str:
        """Re-run a download (resume/recheck: existing data in staging is
        verified and kept by both engines)."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return "download not found"
        with self._lock:
            proc = self._processes.get(download_id)
            qbit_tag = self._qbit_tags.get(download_id)
        if qbit_tag is not None:
            try:
                client = _QBitClient()
                client.login()
                client.simple_by_tag("resume", qbit_tag)
                downloads_store.set_status(download_id, "downloading")
                self._notify(download_id)
                return "resumed in qBittorrent"
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            return "already running"
        downloads_store.set_status(download_id, "queued", error=None)
        # Read the frozen want snapshot instead of the torrent row's own title
        # (the old restart bug: a sequel-named torrent title re-drove routing).
        request_title = _request_title_from_row(row)
        threading.Thread(
            target=self._run_download,
            args=(download_id, row.magnet, row.staging_dir or config.TORRENT_DOWNLOAD_DIR,
                  request_title),
            name=f"torrent-dl-{download_id}", daemon=True,
        ).start()
        self._notify(download_id)
        return "restarted (existing data is verified and kept)"

    def recheck(self, download_id: int) -> str:
        with self._lock:
            qbit_tag = self._qbit_tags.get(download_id)
        if qbit_tag is not None:
            try:
                client = _QBitClient()
                client.login()
                client.simple_by_tag("recheck", qbit_tag)
                return "rechecking in qBittorrent"
            except Exception as exc:
                return f"qBittorrent recheck failed: {exc}"
        # webtorrent verifies on-disk pieces on start — restart IS a recheck.
        return self.restart(download_id)

    def remove(self, download_id: int, *, delete_files: bool) -> str:
        """Drop the download row (torrent-client 'remove'); optionally
        recycle its staged files too. Library files already moved into place
        are NOT touched. History rows are kept."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return "download not found"
        self.cancel(download_id)
        removed_files = 0
        if delete_files and row.status != "moved":
            try:
                from send2trash import send2trash
                for f in self._media_files_in_staging(row):
                    send2trash(str(f))
                    removed_files += 1
            except Exception as exc:
                logger.exception("Staged-file delete failed for #%s", download_id)
                return f"removed row, but file delete failed: {exc}"
        # Archive (tombstone) instead of hard-delete: the row + its
        # download_files / request_downloads provenance survive, and the UI
        # hides archived rows by default (Task C item 3).
        downloads_store.tombstone_download(
            download_id,
            reason=("removed:files_recycled" if removed_files else "removed"))
        self._notify(download_id)
        note = f" + {removed_files} staged file(s) recycled" if removed_files else ""
        return f"removed (archived — provenance kept){note}"

    def apply_route(self, download_id: int) -> str:
        """Manually rename+move a completed download per its (re-computed)
        route plan. Returns a human-readable outcome message."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return "Download not found."
        if row.status not in ("downloaded",):
            return f"Can't apply route while status is '{row.status}'."
        request_title = _request_title_from_row(row)
        return self._post_process(
            row.download_id, force_move=True, force_rename=True,
            request_title=request_title,
        )

    @staticmethod
    def _oversize_gate(seeded: list[TorrentResult], media_type: str, key: str,
                       *, minutes: float | None = None,
                       pref_override: float | None = None) -> bool:
        """Routine-grab size discipline: when EVERY viable option is >20%
        over the preferred size, wait a day (keyed cooldown) and only then
        allow it. Returns True when grabbing may proceed now.

        `pref_override` is the Task G match-library preference (MB/min) — the
        1.2x OVERSIZE DEFERRAL then anchors on the show's own derived target,
        distinct from the 1.8x hard max the selection gate enforces."""
        pref, _mx, default_minutes = _size_prefs(media_type)
        if pref_override and pref_override > 0:
            pref = pref_override
        if pref <= 0 or not seeded:
            downloads_store.clear_grab_deferral(key)
            return True
        target = pref * (minutes or default_minutes) * 1024 * 1024
        if any(r.size_bytes <= target * 1.2 for r in seeded):
            downloads_store.clear_grab_deferral(key)
            return True
        # note: deferred - the oversize deferral records its reason but not
        # candidate_stats/selection_run_id (the run row does not exist yet at
        # gate time); the blocked-candidate deferral path carries both.
        if downloads_store.check_grab_deferral(
                key, reason="oversize: every result is >120% of the preferred size"):
            logger.info("Oversize deferral expired for %s — proceeding.", key)
            return True
        logger.info("Only oversized results for %s (>120%% of preference) — "
                    "waiting a day before taking one.", key)
        return False

    # -- Grab-safety guards -------------------------------------------------
    # Root cause of the "kanji renames" incident: a search could return
    # UNRELATED torrents (especially for non-ASCII titles), the grabber took
    # the top result, and the episode-context renamer then stamped it with
    # the tracked show's name and moved it into that show's folder.

    @staticmethod
    def _known_names(show: shows_store.TrackedShow) -> set[str]:
        names = {show.title}
        for folder in show.folders:
            cleaned, _year = show_tracker.clean_show_folder_name(Path(folder).name)
            if cleaned:
                names.add(cleaned)
        if show.source == "anidb":
            try:
                import anime_db
                names.update(anime_db.titles_for_anidb(show.external_id))
            except Exception:
                pass
        return {n for n in names if n.strip()}

    @classmethod
    def _search_title_for(cls, show: shows_store.TrackedShow) -> str:
        """Title to put in torrent searches — never a mostly non-ASCII one
        (kanji queries return garbage matches on most indexers)."""
        def ascii_ratio(s: str) -> float:
            letters = [c for c in s if not c.isspace()]
            if not letters:
                return 0.0
            return sum(1 for c in letters if ord(c) < 128) / len(letters)

        if ascii_ratio(show.title) >= 0.7:
            return show.title
        candidates = sorted(cls._known_names(show), key=lambda n: -ascii_ratio(n))
        if candidates and ascii_ratio(candidates[0]) >= 0.7:
            logger.info("Using ASCII alias '%s' to search for '%s'.",
                        candidates[0], show.title)
            return candidates[0]
        return show.title

    def _result_matches_show(
        self, result_title: str, show: shows_store.TrackedShow,
        *, season: int | None = None, episode: int | None = None,
    ) -> bool:
        """A routine grab may only take results that (a) don't contradict the
        wanted season/episode and (b) actually resemble one of the show's
        known names. 'House of the Dragon S03E03' can never again be grabbed
        for some anime's S03E03 slot."""
        parsed = torrent_routing.parse_torrent_name(result_title)
        if episode is not None and parsed.episode is not None and parsed.episode != episode:
            return False
        if season is not None and parsed.season is not None and parsed.season != season:
            return False
        names = self._known_names(show)
        haystack = result_title.casefold()
        if any(n.casefold() in haystack for n in names if len(n) >= 4):
            return True
        cand = parsed.show_title or result_title
        best = max((torrent_routing._folder_similarity(cand, n) for n in names),
                   default=0.0)
        if best < 0.5:
            logger.warning("Rejected unrelated result for '%s': %r (best name "
                           "similarity %.0f%%)", show.title, result_title, best * 100)
            return False
        return True

    # _result_matches_query was DELETED in Phase 3: its sole caller (the movie
    # auto-grab branch) now runs torrent_select, whose title-identity gate
    # (RTN.title_match + sequel/numeric guards) supersedes the old
    # containment/fuzzy check. Nothing else referenced it.

    def _match_tracked_show(self, title: str) -> shows_store.TrackedShow | None:
        best: shows_store.TrackedShow | None = None
        best_score = 0.0
        for show in shows_store.list_shows():
            score = torrent_routing._folder_similarity(title, show.title)
            if score > best_score:
                best, best_score = show, score
        return best if best_score >= 0.85 else None

    @staticmethod
    def _tracked_show_for_request(req) -> shows_store.TrackedShow | None:
        """Resolve (upserting if new) the tracked show a request-linked TV grab
        routes through — by IDENTITY (source, external_id), never fuzzy title
        matching (Task D item 1). Returns None for movies / unqualified rows,
        which keep the legacy fuzzy paths."""
        if req is None or req.media_type not in ("tv", "anime", "xanime"):
            return None
        source = getattr(req, "identity_source", None)
        ext = getattr(req, "external_id", None)
        if not (source and ext):
            return None
        ext = str(ext)
        show = shows_store.get_show_by_identity(source, ext)
        if show is not None:
            return show
        show_id = shows_store.upsert_show(
            title=req.resolved_title or req.content, media_type=req.media_type,
            source=source, external_id=ext,
            external_url=getattr(req, "external_url", None),
            year=getattr(req, "canonical_year", None))
        return shows_store.get_show(show_id)

    # ------------------------------------------------------------------
    # Selection engine wiring (Phase 3) — every automatic decision path runs
    # torrent_select.select_torrent over a search_collect pool, injects the
    # CAM check, and persists the decision via record_selection_run. The legacy
    # filter_viable_results/pick_best_result/_prefer_unfailed picker is retained
    # only for its unit tests and any manual/legacy caller; NO automatic path
    # below reaches it.
    # ------------------------------------------------------------------

    @staticmethod
    def _recent_failure_hashes(context_key: str) -> set[str]:
        """Info-hashes that failed for this context within the retry window.

        Drives torrent_select's scored -25 recent_failure component (the
        explainable replacement for _prefer_unfailed's order-dependent
        pre-sort). Unlike the old pre-sort these are NOT excluded — merely
        penalised — so a release that is the only remaining option can still
        win.
        """
        fails = downloads_store.failed_grab_times(context_key)
        if not fails:
            return set()
        cutoff = time.time() - FAILED_GRAB_RETRY_AFTER_S
        return {h for h, t in fails.items() if t >= cutoff}

    def _blocklist_for(self, want: SelectWant):
        """Subject-scoped blocklist input for the selector (Task C item 2).

        Reads the permanent, subject-scoped blocklist entries keyed on
        want.identity.subject_key (plus any global_bad_release rows) and returns
        them as torrent_select.BlocklistEntry objects. The engine's gate matches
        on infohash OR (normalized title, size within 2%, group) and drops
        non-blocking reasons itself, so a race_loser / transient row can never
        block. Returns None when there is nothing to block (gate is a no-op).
        """
        subject_key = want.identity.subject_key
        rows = downloads_store.blocklist_entries_for_subject(subject_key)
        if not rows:
            return None
        return [
            torrent_select.BlocklistEntry(
                reason_code=r.reason_code, infohash=r.infohash,
                parsed_title=r.parsed_title, size_bytes=r.size_bytes,
                release_group=r.release_group)
            for r in rows
        ]

    @staticmethod
    def _subject_type_for(identity: MediaIdentity) -> str:
        """Blocklist subject_type for a want identity."""
        if identity.media_type != "movie" and identity.season is not None:
            return "show_episode" if identity.episode is not None else "show_season"
        return "request_identity"

    @staticmethod
    def _identity_for_download(row: downloads_store.DownloadRow) -> MediaIdentity:
        """The MediaIdentity a download was meant to satisfy: the frozen
        want_json first, then the linked request row, finally the bare title."""
        want_dict = downloads_store.get_want(row.download_id)
        if want_dict:
            return verification.identity_from_want(want_dict)
        if row.request_id is not None:
            req = get_request(row.request_id)
            if req is not None:
                return MediaIdentity(
                    media_type=req.media_type,
                    identity_source=getattr(req, "identity_source", None),
                    external_id=(str(req.external_id)
                                 if req.external_id is not None else None),
                    canonical_title=req.resolved_title or row.title,
                    canonical_year=getattr(req, "canonical_year", None),
                    origin_countries=tuple(getattr(req, "origin_countries", []) or ()),
                    season=getattr(req, "season", None))
        return MediaIdentity(media_type=row.media_type, canonical_title=row.title)

    @staticmethod
    def _identity_blocklist_key(identity: MediaIdentity,
                                row: downloads_store.DownloadRow) -> str | None:
        """The subject_key a wrong/failed grab is blocked under. Prefers the
        qualified identity key; falls back to a request-scoped key so an
        unqualified want still stops the exact re-grab."""
        return identity.subject_key or (
            f"req:{row.request_id}" if row.request_id is not None else None)

    def _quarantine_payload(self, row: downloads_store.DownloadRow,
                            want_identity: MediaIdentity, reason_code: str,
                            detail: str, *, offending=None,
                            block_reason: str | None = None,
                            created_by: str = "auto-verify") -> str:
        """Verify-before-move FAILED: no move, quarantine in staging, record a
        subject-scoped blocklist entry, reopen the request so the next pass
        picks differently (Task C item 1). Files are KEPT (quarantine is
        reversible / adoptable); the row is archived + flagged, not deleted."""
        did = row.download_id
        downloads_store.set_verification(did, "quarantined",
                                         reason=f"{reason_code}: {detail}")
        downloads_store.add_history(
            did, "quarantined", before=None,
            after=f"verify failed ({reason_code}) — kept in staging: {detail}")

        subject_key = self._identity_blocklist_key(want_identity, row)
        offending_size = None
        parsed_title = row.title
        if offending is not None:
            if getattr(offending, "parsed", None) is not None:
                parsed_title = offending.parsed.parsed_title
            try:
                offending_size = offending.path.stat().st_size
            except OSError:
                offending_size = None
        if subject_key:
            downloads_store.add_blocklist_entry(
                subject_type=self._subject_type_for(want_identity),
                subject_key=subject_key,
                reason_code=block_reason or downloads_store.BLOCK_REASON_IDENTITY_MISMATCH,
                infohash=_magnet_hash(row.magnet) or None,
                parsed_title=parsed_title, size_bytes=offending_size,
                reason_detail=f"{reason_code}: {detail}", created_by=created_by)

        # Archive the row so request_ids_with_downloads ignores it (the request
        # becomes re-grabbable) while the row + provenance survive for evidence
        # and possible later adoption by a compatible request (item 8).
        downloads_store.tombstone_download(did, reason=f"quarantine:{reason_code}")
        if row.request_id is not None:
            # Reopen for a different release, or escalate to needs_attention once
            # this request has burned through the attempt cap (deterministic
            # lifecycle — never silently back to an unbounded open loop).
            downloads_store.resolve_request_after_failed_grab(row.request_id)
        self._notify(did)
        return (f"quarantined (verify failed: {reason_code}) — blocked for "
                f"{subject_key}, request reopened")

    def mark_wrong_grab(self, download_id: int, *, recycle: bool = False,
                        widen_global: bool = False,
                        created_by: str = "user") -> str:
        """User action (Task C item 7): the grab was the wrong pick. Block it
        subject-scoped (user_wrong_pick), reopen the request, and QUARANTINE by
        default (RESOLVED DECISION 2). `recycle=True` (the "Recycle now" button)
        also send2trashes the staged bytes; `widen_global=True` escalates the
        block to global_bad_release. Logic lives here, below the Tk glue."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return "download not found"
        want_identity = self._identity_for_download(row)
        if row.status in ("queued", "downloading"):
            self.cancel(download_id)

        subject_key = self._identity_blocklist_key(want_identity, row)
        subject_type = self._subject_type_for(want_identity)
        reason = downloads_store.BLOCK_REASON_USER_WRONG_PICK
        if widen_global:
            subject_key = downloads_store.SUBJECT_GLOBAL
            subject_type = "global_bad_release"
            reason = downloads_store.BLOCK_REASON_GLOBAL_BAD_RELEASE
        if subject_key:
            downloads_store.add_blocklist_entry(
                subject_type=subject_type, subject_key=subject_key,
                reason_code=reason, infohash=_magnet_hash(row.magnet) or None,
                parsed_title=row.title, reason_detail="user marked wrong pick",
                created_by=created_by)

        recycled = 0
        if recycle:
            try:
                from send2trash import send2trash
                for f in self._media_files_in_staging(row):
                    send2trash(str(f))
                    recycled += 1
            except Exception:
                logger.exception("Wrong-grab recycle failed for #%s", download_id)
            downloads_store.set_verification(download_id, "failed",
                                             reason="user_wrong_pick (recycled)")
            downloads_store.tombstone_download(
                download_id, reason="user_wrong_pick:recycled")
        else:
            downloads_store.set_verification(download_id, "quarantined",
                                             reason="user_wrong_pick")
            downloads_store.tombstone_download(
                download_id, reason="quarantine:user_wrong_pick")

        if row.request_id is not None:
            queue_store.set_status(row.request_id, queue_store.STATUS_OPEN)
        self._notify(download_id)
        note = f" + {recycled} staged file(s) recycled" if recycled else " (quarantined)"
        scope = "globally" if widen_global else f"for {subject_key}"
        return f"marked wrong pick — blocked {scope}, request reopened{note}"

    def find_adoptable_quarantine(self, want: SelectWant):
        """Task C item 8: before starting a NEW search, look for a compatible
        quarantined payload. A `Tremors.II...` blocked for Tremors 1 is still a
        valid Tremors 2 payload — if its staged files pass fresh identity
        verification for THIS want, it can be adopted without a second download.
        Returns (DownloadRow, VerificationResult) or None. Recycled quarantines
        (no bytes) are naturally skipped (no media files on disk)."""
        for row in downloads_store.list_quarantined_downloads():
            files = self._media_files_in_staging(row)
            if not files:
                continue
            result = verification.verify_staging(
                files, want.identity,
                video_exts=torrent_routing.VIDEO_EXTENSIONS,
                subtitle_exts=torrent_routing.SUBTITLE_EXTENSIONS)
            if result.ok:
                return row, result
        return None

    def adopt_quarantine(self, download_id: int, request_id: int) -> str:
        """Adopt a quarantined payload for a NEW request after FRESH
        verification (Task C item 8). The staged bytes are re-verified against
        the new request's identity, the tombstone is cleared, the payload is
        re-purposed (want re-frozen to the new request, selection_mode
        reused_quarantine recorded) and moved. The subject-scoped block on the
        ORIGINAL identity is untouched — that release stays wrong for the old
        request. Confirmation is the caller's (UI) responsibility."""
        row = downloads_store.get_download(download_id)
        new_req = get_request(request_id)
        if row is None or new_req is None:
            return "adopt failed: row or request missing"

        want = _build_want_snapshot(
            _AdoptResult(row), new_req,
            request_title=_row_search_alias(new_req),
            show_id=None, season=getattr(new_req, "season", None),
            episode=None, minutes=None)
        files = self._media_files_in_staging(row)
        result = verification.verify_staging(
            files, verification.identity_from_want(want),
            video_exts=torrent_routing.VIDEO_EXTENSIONS,
            subtitle_exts=torrent_routing.SUBTITLE_EXTENSIONS)
        if not result.ok:
            return f"cannot adopt: fails verification ({result.reason_code})"

        downloads_store.restore_download(download_id)
        downloads_store.set_want(download_id, want)
        downloads_store.set_request_id(download_id, request_id)
        downloads_store.set_status(download_id, "downloaded")
        # Record the adoption as its own selection decision (mode reused_quarantine).
        from torrent_select import SelectionDecision
        import datetime as _dt
        decision = SelectionDecision(
            chosen_infohash=_magnet_hash(row.magnet) or None,
            chosen_title=row.title, mode=torrent_select.MODE_REUSED_QUARANTINE,
            profile=torrent_select.PROFILE,
            rtn_version=torrent_select.rtn_version(),
            verdicts=(), scores=(),
            pool_stats={"adopted_from_download": download_id},
            created_at=_dt.datetime.now(_dt.timezone.utc).isoformat())
        run_id = downloads_store.record_selection_run(
            decision, request_id=request_id, download_id=download_id)
        downloads_store.link_download_to_run(download_id, run_id)
        downloads_store.add_history(
            download_id, "adopted", before=None,
            after=f"adopted from quarantine for request #{request_id} "
                  f"(reused_quarantine)")
        outcome = self._post_process(
            download_id, force_move=True, force_rename=True,
            request_title=_row_search_alias(new_req))
        return f"adopted: {outcome}"

    def _on_download_placed(self, row: downloads_store.DownloadRow,
                            moved_final_paths: list[str]) -> None:
        """A download's verified files are in place. Link provenance and let the
        AGGREGATE decide whether the REQUEST is fulfilled (item 4)."""
        if row.request_id is None:
            return
        if row.season is not None and row.episode is None:
            role = "season_pack"
        elif row.episode is not None:
            role = "episode"
        else:
            role = "movie"
        downloads_store.link_request_download(row.request_id, row.download_id, role)
        queue_store.set_status(row.request_id, queue_store.STATUS_PLACED)
        self._finalize_fulfillment(row.request_id)

    def _finalize_fulfillment(self, request_id: int) -> None:
        """AGGREGATE fulfillment (Task C item 4): moved_any is dead. A movie /
        one-off completes on a single placed file; a season request completes
        ONLY when its expected episode set is present (or a verified pack
        supplied it). A first episode updates progress, never the season."""
        req = get_request(request_id)
        if req is None:
            return
        if req.media_type in ("movie", "other", "unknown"):
            queue_store.complete_request(request_id)
            return

        from datetime import date
        today = date.today().isoformat()
        # Identity-first (Task D): resolve the show the request is linked to by
        # its (source, external_id), falling back to fuzzy title only for
        # unqualified legacy rows.
        show = (self._tracked_show_for_request(req)
                or self._match_tracked_show(_row_search_alias(req)))
        want_season = req.season
        if show is not None and want_season is not None:
            eps = [e for e in shows_store.list_episodes(show.show_id)
                   if e.season == want_season and e.air_date
                   and e.air_date <= today]
            have = [e for e in eps if e.has_file]
            if eps and len(have) >= len(eps):
                queue_store.complete_request(request_id)
            # else: partial — request stays PLACED (visible progress), not done.
            return
        # No tracked-show set to measure against: a verified pack (>= 2 distinct
        # episodes placed) fulfils; a lone episode never does.
        if self._request_covers_multiple_episodes(request_id):
            queue_store.complete_request(request_id)

    @staticmethod
    def _request_covers_multiple_episodes(request_id: int) -> bool:
        episodes: set[int] = set()
        for dl in downloads_store.downloads_for_request(request_id):
            for f in downloads_store.list_download_files(dl.download_id):
                if (f.verification_state in ("verified", "duplicate")
                        and f.parsed_episode is not None):
                    episodes.add(int(f.parsed_episode))
        return len(episodes) >= 2

    def _run_selection(self, results, want: SelectWant, *,
                       failure_key: str | None = None,
                       pool_stats: dict | None = None):
        """Run the pure selector over a result pool and return
        (SelectionDecision, {infohash: TorrentResult}).

        Injects cam_check=video_quality.is_cam_release at EVERY site (binding
        Phase 0/1 obligation: RTN.trash alone misses HDCAM-style names like
        WORKPRINT), the recent-failure penalty, and the (stub) blocklist input.
        """
        from video_quality import is_cam_release
        candidates = []
        by_hash: dict[str, TorrentResult] = {}
        for r in results:
            cand = torrent_select.to_candidate(r)
            candidates.append(cand)
            if cand.norm_infohash:
                by_hash.setdefault(cand.norm_infohash, r)
        recent = self._recent_failure_hashes(failure_key) if failure_key else None
        decision = torrent_select.select_torrent(
            candidates, want,
            blocklist=self._blocklist_for(want),
            recent_failures=recent,
            cam_check=is_cam_release,
            pool_stats=pool_stats,
        )
        return decision, by_hash

    @staticmethod
    def _quality_label_for(decision, result) -> str | None:
        """The quality label the engine already parsed on this candidate
        (Task F: reuse the decision's parse instead of re-parsing at grab)."""
        if result is None or decision is None:
            return None
        ih = torrent_select.infohash_from_magnet(getattr(result, "magnet", ""))
        for s in getattr(decision, "scores", ()) or ():
            if ih and s.infohash == ih:
                return s.quality_label
        return None

    def _want_from_request(self, req, media_type: str, *, mode: str,
                           season: int | None = None,
                           episode: int | None = None,
                           minutes: float | None = None,
                           runtime_override: float | None = None) -> SelectWant:
        """Build the immutable SelectWant a request-linked grab is judged
        against, from the request row's stored identity (never raw user text).
        """
        pref, max_rate, default_minutes = _size_prefs(media_type)
        # For movie/other single grabs, fall back to the search alias as the
        # canonical title so RTN.title_match still guards the pick when the row
        # carries no resolved_title (preserving the old _result_matches_query
        # containment guard through the engine's title gate).
        canonical = getattr(req, "resolved_title", None) if req else None
        if not canonical and req is not None:
            canonical = _row_search_alias(req) or None
        identity = MediaIdentity(
            media_type=media_type,
            identity_source=getattr(req, "identity_source", None) if req else None,
            external_id=(str(req.external_id)
                         if req is not None and req.external_id is not None else None),
            canonical_title=canonical,
            canonical_year=getattr(req, "canonical_year", None) if req else None,
            origin_countries=tuple(getattr(req, "origin_countries", []) or ())
            if req else (),
            aliases=tuple(getattr(req, "aliases", []) or ()) if req else (),
            season=season if season is not None else (
                getattr(req, "season", None) if req else None),
            episode=episode,
        )
        rt = runtime_override if runtime_override is not None else minutes
        return SelectWant(
            identity=identity, size_pref_mb_min=pref, size_max_rate=max_rate,
            runtime_minutes=rt, fallback_minutes=default_minutes,
            allow_cam=not config.BLOCK_CAMS, mode=mode,
            min_seeders=_min_seeders_for(mode))

    def _want_for_show_episode(self, show: shows_store.TrackedShow,
                               season: int | None, episode: int | None,
                               minutes: float | None, mode: str) -> SelectWant:
        """SelectWant for a tracked-show episode/season grab, keyed on the
        show's identity from tracked_shows (source + external_id). A show with
        size_mode='match_library' gets its derived preference applied here
        (Task G) so EVERY automatic path through this want honours it."""
        pref, max_rate, default_minutes = _size_prefs(show.media_type)
        identity = MediaIdentity(
            media_type=show.media_type,
            identity_source=getattr(show, "source", None),
            external_id=(str(show.external_id)
                         if getattr(show, "external_id", None) is not None else None),
            canonical_title=show.title,
            season=season,
            episode=episode,
        )
        want = SelectWant(
            identity=identity, size_pref_mb_min=pref, size_max_rate=max_rate,
            runtime_minutes=minutes, fallback_minutes=default_minutes,
            allow_cam=not config.BLOCK_CAMS, mode=mode,
            min_seeders=_min_seeders_for(mode))
        want, _meta = self._apply_size_override(want, show)
        return want

    # ------------------------------------------------------------------
    # Task G — per-show "match my existing sizes" override
    # ------------------------------------------------------------------

    def _size_pick_for_show(self, show) -> size_match.SizePick | None:
        """The derived SizePick for a match_library show (cached per pass);
        None when the show is absent or on the global mode."""
        if show is None or getattr(show, "size_mode",
                                   size_match.SIZE_MODE_GLOBAL) \
                != size_match.SIZE_MODE_MATCH_LIBRARY:
            return None
        cached = self._size_pick_cache.get(show.show_id)
        if cached is not None:
            return cached

        def _junk(name: str) -> bool:
            return bool(verification._SAMPLE_RE.search(name)
                        or verification._EXTRA_NAME_RE.search(name))

        sizes = size_match.eligible_episode_sizes(
            shows_store.list_episodes(show.show_id),
            video_exts=torrent_routing.VIDEO_EXTENSIONS, is_junk_name=_junk)
        _pref, _mx, default_minutes = _size_prefs(show.media_type)
        # The SAME minutes value the want uses for target conversion — the
        # derived MB/min round-trips back to the sampled median exactly.
        minutes = _runtime_minutes(show) or default_minutes
        pick = size_match.pick_from_sizes(sizes, minutes)
        self._size_pick_cache[show.show_id] = pick
        return pick

    def _apply_size_override(self, want: SelectWant,
                             show) -> tuple[SelectWant, dict | None]:
        """Apply a show's match-library size derivation to a want.

        Pref := the library mode; hard max := 1.8 x mode (House of the Dragon
        at 2.5 GB keeps grabbing 2.5 GB regardless of the global TV cap). On a
        fallback (too few files / no runtime) the global knobs stay and only
        the flagged meta is returned. Returns (want, pick_meta_or_None)."""
        pick = self._size_pick_for_show(show)
        if pick is None:
            return want, None
        if not pick.ok:
            return want, pick.meta()  # global fallback, flagged in pick_meta
        from dataclasses import replace as _dc_replace
        want = _dc_replace(want, size_pref_mb_min=pick.mb_per_min,
                           size_max_rate=pick.mb_per_min * 1.8)
        return want, pick.meta()

    def _grab_season_pack(self, title: str, media_type: str, season: int,
                          ep_count: int, *, request_id: int | None,
                          show: shows_store.TrackedShow | None = None,
                          search_title: str | None = None) -> list[int]:
        """Search and grab one season as a pack (max-size cap scaled to the
        season's episode count so packs aren't vetoed by per-episode caps).

        `search_title` is the caller-supplied QUERY text and always wins when
        given. Request-linked callers pass the row's stored search alias, which
        carries country disambiguation ("Married at First Sight US") — a fuzzy
        tracked-show match may inform routing/filtering via `show`, but must
        never override the query text or the wrong-country grab returns.
        """
        if not search_title:
            search_title = self._search_title_for(show) if show is not None else title
        results: list[TorrentResult] = []
        pool_stats: dict = {}
        for query in (f"{search_title} S{season:02d}", f"{search_title} Season {season}"):
            try:
                pool = search_collect(query, media_type)
            except Exception:
                logger.exception("Season-pack search failed for %s", query)
                continue
            if pool.results:
                results = list(pool.results)
                pool_stats = dict(pool.pool_stats)
                break
        # Pre-filter to plausible packs (seeders required — packs never race) and
        # guard against unrelated shows before the engine scores what survives.
        viable = [r for r in results if r.size_bytes > 0 and r.seeders > 0]
        if show is not None:
            viable = [r for r in viable
                      if self._result_matches_show(r.title, show, season=season)]
        else:
            # Untracked request: the result must at least contain the title.
            viable = [r for r in viable
                      if title.casefold() in r.title.casefold()
                      or torrent_routing._folder_similarity(
                          torrent_routing.parse_torrent_name(r.title).show_title or r.title,
                          title) >= 0.5]
        _pref, _max_rate, default_minutes = _size_prefs(media_type)
        per_ep = _runtime_minutes(show) or default_minutes
        season_minutes = max(1, ep_count) * per_ep
        key = f"pack:{title.casefold()}:{season}"

        # The size max cap scales to the whole season (season_minutes), so a
        # pack isn't vetoed by a per-episode cap — passed to the engine via the
        # want's runtime_minutes override.
        req = get_request(request_id) if request_id is not None else None
        want = self._want_from_request(
            req, media_type, mode=torrent_select.MODE_AUTOMATIC_SEASON_PACK,
            season=season, runtime_override=season_minutes)
        # Task G: a match-library show's derived MB/min replaces the global
        # pref/max; the season target multiplies through season_minutes
        # (ep_count x per-episode runtime), so packs scale automatically.
        want, size_meta = self._apply_size_override(want, show)
        if size_meta is not None:
            pool_stats["size_match"] = size_meta
        size_pick = self._size_pick_for_show(show)
        pref_override = size_pick.mb_per_min if (size_pick and size_pick.ok) else None
        decision, by_hash = self._run_selection(
            viable, want, failure_key=key, pool_stats=pool_stats)
        chosen = by_hash.get(decision.chosen_infohash) if decision.chosen else None
        if chosen is None:
            run_id = downloads_store.record_selection_run(
                decision, request_id=request_id)
            if self._dead_swarm(decision):
                # Packs never race; record a deferral so the grab queue shows
                # why it is held instead of hammering every pass.
                downloads_store.check_grab_deferral(
                    key, wait_hours=config.ZERO_SEEDER_DEFER_HOURS,
                    reason="no seeded season pack available",
                    candidate_stats=decision.verdict_histogram(),
                    selection_run_id=run_id)
            return []
        survivors = [by_hash[s.infohash] for s in decision.scores
                     if s.infohash in by_hash]
        if not self._oversize_gate(survivors, media_type, key,
                                   minutes=season_minutes,
                                   pref_override=pref_override):
            downloads_store.record_selection_run(decision, request_id=request_id)
            return []
        # Route the pack by show_id when we have an identity-backed show (Task D
        # item 1) — the download row carries show_id + season so _post_process
        # uses plan_for_season, never fuzzy find_show_folder.
        season_context = (show.show_id, season) if show is not None else None
        download_id = self.grab(chosen, request_id=request_id, request_title=title,
                                auto_rename=True, auto_move=True,
                                failure_key=key, season_context=season_context,
                                quality_label=self._quality_label_for(decision, chosen))
        downloads_store.record_selection_run(
            decision, request_id=request_id, download_id=download_id)
        logger.info("Season-pack grab: '%s' S%02d → download #%s (%s)",
                    title, season, download_id, chosen.title)
        return [download_id]

    def _grab_request_seasonwise(self, req) -> list[int]:
        """Season-aware plan for an episodic request (per Cole):
        - show tracked & latest OWNED season incomplete → finish that season
        - latest owned season complete → grab the next aired season
        - show not owned at all → grab season 1
        """
        from datetime import date
        title = _row_search_alias(req)
        if not title:
            return []
        today = date.today().isoformat()
        # Route by IDENTITY (source, external_id) first (Task D item 1): a
        # request-linked TV row resolves — and upserts — its exact tracked show,
        # so a same-title country edition can never route to the wrong folder.
        # Fuzzy title matching survives only for rows with no qualified identity.
        show = self._tracked_show_for_request(req) or self._match_tracked_show(title)

        # Task A explicit season rows: a request row carrying a concrete season
        # grabs exactly that season's pack via the alias query. This REPLACES the
        # legacy new-show/next-season heuristic for request-linked grabs (Task D
        # item 4) — the two must not coexist. The heuristic below runs only for
        # legacy request rows that carry no explicit season.
        req_season = getattr(req, "season", None)
        if req_season is not None:
            display = show.title if show is not None else title
            ep_count = 0
            if show is not None:
                ep_count = sum(1 for e in shows_store.list_episodes(show.show_id)
                               if e.season == req_season)
            return self._grab_season_pack(
                display, req.media_type, int(req_season),
                ep_count or 12, request_id=req.request_id, show=show,
                search_title=title)

        if show is None or show.have_count == 0:
            display = show.title if show is not None else title
            return self._grab_season_pack(display, req.media_type, 1, 12,
                                          request_id=req.request_id, show=show,
                                          search_title=title)

        eps = shows_store.list_episodes(show.show_id)
        owned_seasons = sorted({e.season for e in eps if e.has_file and e.season > 0})
        latest = owned_seasons[-1]
        latest_missing = [
            e for e in eps
            if e.season == latest and not e.has_file
            and e.air_date and e.air_date <= today
        ]
        if latest_missing:
            started: list[int] = []
            for ep in latest_missing[:config.SHOWS_GRAB_LIMIT_PER_PASS]:
                started.extend(self._grab_one_episode(
                    show, ep, request_id=req.request_id))
            return started

        # Latest owned season is complete → the next season with aired eps.
        future_seasons = sorted({
            e.season for e in eps
            if e.season > latest and e.air_date and e.air_date <= today
        })
        if not future_seasons:
            logger.info("Request '%s': latest owned season complete, nothing "
                        "newer has aired.", title)
            return []
        target = future_seasons[0]
        ep_count = sum(1 for e in eps if e.season == target
                       and e.air_date and e.air_date <= today)
        return self._grab_season_pack(show.title, show.media_type, target,
                                      ep_count, request_id=req.request_id, show=show,
                                      search_title=title)

    def _grab_one_episode(self, show: shows_store.TrackedShow,
                          ep: shows_store.EpisodeRow,
                          *, request_id: int | None = None) -> list[int]:
        """Search + grab a single tracked episode (shared by the missing-
        episode pass, follow-new/keep-at-100, and season-aware requests).

        `request_id` flows the originating request through so season-wise grabs
        stop losing their provenance (the Task C item 4 leak; the LINKAGE lands
        now, the junction table is Phase 4). Selection runs through the engine
        in automatic-episode mode.
        """
        if ep.grab_download_id is not None:
            linked = downloads_store.get_download(ep.grab_download_id)
            if linked is not None and linked.status not in ("error", "cancelled"):
                return []
        search_title = self._search_title_for(show)
        query = f"{search_title} S{ep.season:02d}E{ep.episode:02d}"
        try:
            pool = search_collect(query, show.media_type)
            results = list(pool.results)
            pool_stats = dict(pool.pool_stats)
            if not results and show.media_type in ("anime", "xanime"):
                pool = search_collect(
                    f"{search_title} {ep.episode:02d}", show.media_type)
                results = list(pool.results)
                pool_stats = dict(pool.pool_stats)
        except Exception:
            logger.exception("Episode search failed for %s", query)
            return []
        minutes = _runtime_minutes(show)
        # Episode/season contradiction + show-name guard (the engine's title
        # gate does not check episode numbers, and _result_matches_show carries
        # the kanji-incident "resembles a known name" protection).
        viable = [r for r in results if self._result_matches_show(
            r.title, show, season=ep.season, episode=ep.episode)]
        want = self._want_for_show_episode(
            show, ep.season, ep.episode, minutes,
            torrent_select.MODE_AUTOMATIC_EPISODE)
        # Task G: record the match-library derivation (or its flagged
        # fallback) on the decision so pick_meta explains the size rules.
        size_pick = self._size_pick_for_show(show)
        if size_pick is not None:
            pool_stats["size_match"] = size_pick.meta()
        pref_override = size_pick.mb_per_min if (size_pick and size_pick.ok) else None
        key = f"ep:{show.show_id}:{ep.season}:{ep.episode}"
        decision, by_hash = self._run_selection(
            viable, want, failure_key=key, pool_stats=pool_stats)
        chosen = by_hash.get(decision.chosen_infohash) if decision.chosen else None
        if chosen is None:
            run_id = downloads_store.record_selection_run(
                decision, request_id=request_id)
            if self._dead_swarm(decision):
                # Every gate-clean release for this episode is unseeded — race it
                # only when explicitly enabled, else defer/recheck.
                return self._race_or_defer(
                    key, viable, show.media_type, minutes=minutes,
                    request_id=request_id,
                    episode_context=(show.show_id, ep.season, ep.episode),
                    run_id=run_id, candidate_stats=decision.verdict_histogram())
            return []
        survivors = [by_hash[s.infohash] for s in decision.scores
                     if s.infohash in by_hash]
        if chosen.seeders <= 0:
            # Reachable only when the seeder gate is disabled (MIN_SEEDERS=0): the
            # engine's TOP pick has no seeders. A guaranteed copy beats a gamble,
            # so grab the best-scored seeded survivor when one exists; otherwise
            # race-or-defer.
            seeded = next((r for r in survivors if r.seeders > 0), None)
            if seeded is not None:
                chosen = seeded
            else:
                return self._race_or_defer(
                    key, survivors, show.media_type, minutes=minutes,
                    request_id=request_id,
                    episode_context=(show.show_id, ep.season, ep.episode))
        if not self._oversize_gate(survivors, show.media_type, key,
                                   minutes=minutes, pref_override=pref_override):
            downloads_store.record_selection_run(decision, request_id=request_id)
            return []
        download_id = self.grab(
            chosen, request_id=request_id,
            episode_context=(show.show_id, ep.season, ep.episode),
            auto_rename=True, auto_move=True,
            quality_label=self._quality_label_for(decision, chosen))
        downloads_store.record_selection_run(
            decision, request_id=request_id, download_id=download_id)
        logger.info("Auto-grabbed %s → download #%s (%s, %s seeders)",
                    query, download_id, chosen.title, chosen.seeders)
        return [download_id]

    def auto_grab_open_requests(self) -> list[int]:
        """Grab open requests that have no download yet. Movies take the
        best single result; shows/anime/hentai use the season-aware plan
        (finish the latest owned season → next season → season 1).

        needs_identity rows are never queried here (they are not 'open'); this
        pass logs them so the skip is visible, and a per-row identity guard
        double-protects against any 'open' row that lacks a qualified identity.
        """
        started: list[int] = []
        self._size_pick_cache.clear()  # Task G: fresh library sample per pass
        # Task E: user-deferred requests whose next_attempt_at has passed come
        # back to 'open' before this pass scans the open set.
        try:
            import grab_queue
            grab_queue.reopen_expired_deferrals()
        except Exception:
            logger.exception("Deferral reopen sweep failed.")
        # Visible, logged reason for skipping identity-less rows.
        pending_identity = list_requests(
            status=queue_store.STATUS_NEEDS_IDENTITY, limit=200)
        if pending_identity:
            logger.info(
                "Auto-grab: skipping %d request(s) that need an identity before "
                "they can be grabbed (ids: %s).",
                len(pending_identity),
                [r.request_id for r in pending_identity])

        already = downloads_store.request_ids_with_downloads()
        grabbed_identities: set[str] = set()
        for req in list_requests(status="open", limit=100):
            if req.request_id in already or req.found_in_library:
                continue
            # Grab-gate dedupe: if an identical identity was already handled in
            # this pass, don't grab a second copy of the same thing.
            ident_key = _request_identity_key(req)
            if ident_key and ident_key in grabbed_identities:
                logger.info(
                    "Auto-grab: request #%s duplicates an identity already "
                    "handled this pass — skipping.", req.request_id)
                continue
            # An 'open' movie/tv/anime row must carry a provider-qualified
            # identity to be auto-grabbable. 'other' is exempt by design; an
            # unqualified typed/unknown row is skipped with a logged reason
            # rather than coerced to 'other' and grabbed (the #85 path).
            if req.media_type != "other" and not req.is_qualified:
                logger.info(
                    "Auto-grab: request #%s (%s) is open but has no qualified "
                    "identity — skipping (needs_identity).",
                    req.request_id, req.media_type)
                continue
            # At-grab-time library gate, independent of the daily found flag: if
            # the exact identity is already on disk NOW, mark it found and skip.
            # This is the backstop the stale daily flag can't provide.
            try:
                import maintenance
                if maintenance.request_present_in_library(req):
                    queue_store.update_library_status(req.request_id, found=True)
                    logger.info(
                        "Auto-grab: request #%s (%s) is already in the library "
                        "at grab time — skipping.",
                        req.request_id, req.media_type)
                    continue
            except Exception:
                logger.exception(
                    "At-grab library gate failed for request #%s", req.request_id)
            # Mark this identity handled for the pass (before search): a duplicate
            # open row later in the list is skipped even if this one grabs nothing.
            if ident_key:
                grabbed_identities.add(ident_key)
            # Query from the stored identity (search alias + canonical year for
            # movies), never raw user text or a native-script canonical title.
            query = _auto_grab_query(req)
            media_type = req.media_type if req.media_type != "unknown" else "other"

            if media_type in ("tv", "anime", "xanime"):
                try:
                    grabbed = self._grab_request_seasonwise(req)
                except Exception:
                    logger.exception("Season-aware grab failed for request #%s",
                                     req.request_id)
                    grabbed = []
                started.extend(grabbed)
                continue

            try:
                pool = search_collect(query, media_type)
            except Exception:
                logger.exception("Auto-grab search failed for request #%s", req.request_id)
                continue
            minutes = _request_movie_minutes(req)
            key = f"req:{req.request_id}"
            want = self._want_from_request(
                req, media_type, mode=torrent_select.MODE_AUTOMATIC_SINGLE,
                minutes=minutes)
            decision, by_hash = self._run_selection(
                list(pool.results), want, failure_key=key,
                pool_stats=dict(pool.pool_stats))
            chosen = by_hash.get(decision.chosen_infohash) if decision.chosen else None
            if chosen is None:
                run_id = downloads_store.record_selection_run(
                    decision, request_id=req.request_id)
                # Task C item 9: if a viable release existed but was BLOCKED
                # (subject-scoped), defer a day with a reason + next-attempt +
                # the pass's candidate stats and run id (persisted for the
                # Task E grab queue) rather than hammering every 5 minutes.
                if any(v.reason_code == "blocklisted" for v in decision.verdicts):
                    downloads_store.check_grab_deferral(
                        key, reason="last viable candidate blocked",
                        candidate_stats=decision.verdict_histogram(),
                        selection_run_id=run_id)
                elif self._dead_swarm(decision):
                    # Task D: the only gate-clean releases are unseeded. Race them
                    # (if enabled) or defer/recheck — never grab a dead torrent.
                    started.extend(self._race_or_defer(
                        key, list(pool.results), media_type, minutes=minutes,
                        request_id=req.request_id, request_title=query,
                        run_id=run_id,
                        candidate_stats=decision.verdict_histogram()))
                logger.info("Request #%s (%s): no acceptable result this pass — "
                            "will retry on the next auto-grab cycle.",
                            req.request_id, query)
                continue
            survivors = [by_hash[s.infohash] for s in decision.scores
                         if s.infohash in by_hash]
            if chosen.seeders <= 0:
                # Reachable only with the seeder gate disabled (MIN_SEEDERS=0):
                # prefer a seeded survivor over a 0-seed gamble; race-or-defer
                # only when every survivor is 0-seed.
                seeded = next((r for r in survivors if r.seeders > 0), None)
                if seeded is not None:
                    chosen = seeded
                else:
                    started.extend(self._race_or_defer(
                        key, survivors, media_type, minutes=minutes,
                        request_id=req.request_id, request_title=query))
                    continue
            if not self._oversize_gate(survivors, media_type, key, minutes=minutes):
                downloads_store.record_selection_run(decision, request_id=req.request_id)
                continue
            download_id = self.grab(
                chosen, request_id=req.request_id, request_title=query,
                quality_label=self._quality_label_for(decision, chosen),
            )
            downloads_store.record_selection_run(
                decision, request_id=req.request_id, download_id=download_id)
            logger.info(
                "Auto-grabbed request #%s → download #%s (%s, %s seeders)",
                req.request_id, download_id, chosen.title, chosen.seeders,
            )
            started.append(download_id)
        return started

    def auto_grab_missing_episodes(self, *, limit: int | None = None,
                                   show_ids: list[int] | None = None) -> list[int]:
        """Grab the best torrent for each missing episode of every tracked show.

        The full Sonarr-replacement loop: shows whose episode data is stale
        get re-synced first (so freshly-aired episodes show up as missing),
        then each missing episode without a live grab is searched and the
        best-seeded result downloaded with rename+move forced ON — routing
        for tracked episodes is deterministic (plan_for_episode), so
        auto-placement is safe.

        Guarded by the same lock as scan/sync so a scheduled pass never runs
        concurrently with a manual scan or sync (which would multiply the API
        rate). Returns [] if a Shows operation is already in progress.
        """
        try:
            return show_tracker.run_exclusive(
                "Auto-grab missing",
                lambda: self._auto_grab_missing_impl(limit, show_ids),
            )
        except show_tracker.ShowsBusyError:
            logger.info("Auto-grab missing skipped — a Shows scan/sync is already running.")
            return []

    def _auto_grab_missing_impl(self, limit: int | None,
                                show_ids: list[int] | None = None) -> list[int]:
        limit = config.SHOWS_GRAB_LIMIT_PER_PASS if limit is None else limit
        started: list[int] = []
        self._size_pick_cache.clear()  # Task G: fresh library sample per pass

        for show in shows_store.list_shows():
            if len(started) >= limit:
                break
            follow_only = False
            if show_ids is not None:
                # Explicit selection (Grab Missing Now on selected rows)
                # overrides the auto-grab flags.
                if show.show_id not in show_ids:
                    continue
            elif config.SHOWS_AUTO_GRAB or show.auto_grab:
                pass  # keep-at-100%: every missing episode is fair game
            elif show.follow_new:
                follow_only = True  # new releases only, from follow_since on
            else:
                continue
            # Re-sync stale shows so "missing" reflects reality.
            stale = True
            if show.last_synced:
                try:
                    from datetime import datetime, timedelta, timezone
                    synced = datetime.fromisoformat(show.last_synced).replace(tzinfo=timezone.utc)
                    stale = datetime.now(timezone.utc) - synced > timedelta(
                        hours=config.SHOWS_SYNC_MAX_AGE_HOURS
                    )
                except ValueError:
                    pass
            if stale:
                try:
                    show_tracker.sync_show(show.show_id)
                except Exception:
                    logger.exception("Auto-grab: sync failed for '%s'", show.title)
                    continue

            for ep in shows_store.missing_episodes(show.show_id):
                if len(started) >= limit:
                    break
                if follow_only:
                    since = show.follow_since or ""
                    if not ep.air_date or (since and ep.air_date < since):
                        continue  # back-catalog — follow-new leaves it alone
                started.extend(self._grab_one_episode(show, ep))
        return started

    # ------------------------------------------------------------------
    # Zero-seeder race — when nothing has seeders, try several at once
    # ------------------------------------------------------------------

    @staticmethod
    def _dead_swarm(decision) -> bool:
        """True when nothing was chosen and at least one candidate was clean on
        every axis EXCEPT seeders — a dead-swarm pool (defer/recheck or race),
        as opposed to a wrong-identity pool (genuinely nothing to grab)."""
        return (not decision.chosen and any(
            v.reason_code == "insufficient_seeders" for v in decision.verdicts))

    def _race_or_defer(self, key: str, results, media_type: str, *,
                       minutes: float | None,
                       request_id: int | None = None,
                       request_title: str | None = None,
                       episode_context: tuple[int, int, int] | None = None,
                       run_id: int | None = None,
                       candidate_stats: dict | None = None) -> list[int]:
        """A pool whose only gate-clean releases are unseeded. Gamble on a
        bounded zero-seeder race when it is explicitly enabled; otherwise write
        a deferral and recheck later rather than grabbing dead torrents
        (Task D items 1-2)."""
        if config.TORRENT_ZERO_SEEDER_RACE:
            return self.start_zero_seeder_race(
                results, media_type, minutes=minutes, request_id=request_id,
                request_title=request_title, episode_context=episode_context)
        downloads_store.check_grab_deferral(
            key, wait_hours=config.ZERO_SEEDER_DEFER_HOURS,
            reason="no seeded release available",
            candidate_stats=candidate_stats, selection_run_id=run_id)
        # A request-level single grab is genuinely held: flip it to 'deferred' so
        # the open scan skips it until reopen_expired_deferrals brings it back.
        # Episode/pack keys are not request-status-driven, so they only record
        # the row (matching the existing chosen-None behaviour there).
        if (request_id is not None and episode_context is None
                and key == f"req:{request_id}"):
            req_now = get_request(request_id)
            if req_now is not None and req_now.status in (
                    queue_store.STATUS_OPEN, queue_store.STATUS_GRABBING):
                queue_store.set_status(request_id, queue_store.STATUS_DEFERRED)
        logger.info("%s: only unseeded candidates this pass — deferred "
                    "(zero-seeder race disabled).", key)
        return []

    def start_zero_seeder_race(
        self, results: list[TorrentResult], media_type: str, *,
        request_id: int | None = None, request_title: str | None = None,
        episode_context: tuple[int, int, int] | None = None,
        minutes: float | None = None,
    ) -> list[int]:
        """Every candidate reports 0 seeders (callers only reach here once no
        seeded survivor exists — a guaranteed copy always wins over a gamble).
        Grab up to 5 and race them.

        Redesign (Task C item 6):
        - candidates pass gates 1-8 through the engine exactly like a normal
          pick (zero-seeder-race mode), so a wrong release never enters;
        - ALL race candidates grab with auto-move DISABLED regardless of
          settings — nothing lands in a library root without a verified winner;
        - they SHARE one selection_run_id (persisted here, linked to each row);
        - the monitor verifies the first-finished candidate BEFORE any move; a
          failed-verify candidate is blocked for the identity and the race
          continues; losers cancel as race_loser and are NEVER blocklisted.
        """
        if episode_context is not None:
            show = shows_store.get_show(episode_context[0])
            if show is not None:
                want = self._want_for_show_episode(
                    show, episode_context[1], episode_context[2], minutes,
                    torrent_select.MODE_ZERO_SEEDER_RACE)
            else:
                want = self._race_fallback_want(media_type, minutes)
            key = (f"ep:{episode_context[0]}:{episode_context[1]}:"
                   f"{episode_context[2]}")
        elif request_id is not None:
            req = get_request(request_id)
            want = self._want_from_request(
                req, media_type, mode=torrent_select.MODE_ZERO_SEEDER_RACE,
                minutes=minutes)
            key = f"req:{request_id}"
        else:
            want = self._race_fallback_want(media_type, minutes)
            key = None

        decision, by_hash = self._run_selection(
            list(results), want, failure_key=key)
        # ONE shared selection_run_id across every race download (item 6.2); the
        # pre-race decision is persisted on handoff (Phase 3 verifier note).
        run_id = downloads_store.record_selection_run(decision, request_id=request_id)
        # Bounded: at most ZERO_SEEDER_RACE_MAX_CANDIDATES run at once (was a
        # hardcoded 5) — more just splits the trickle and stalls everything.
        picks = [by_hash[s.infohash] for s in decision.scores
                 if s.infohash in by_hash][
                     :max(1, config.ZERO_SEEDER_RACE_MAX_CANDIDATES)]
        if not picks:
            logger.info("Zero-seeder race: no candidate survived the gates "
                        "(run #%s)", run_id)
            return []
        ids: list[int] = []
        for r in picks:
            # auto-move + auto-rename OFF for every racer: verification decides
            # the single winner that is allowed to move.
            did = self.grab(r, request_id=request_id, request_title=request_title,
                            episode_context=episode_context,
                            auto_rename=False, auto_move=False,
                            quality_label=self._quality_label_for(decision, r))
            downloads_store.link_download_to_run(did, run_id)
            ids.append(did)
        # Show these as 'probing' in the queue UI until the race resolves.
        self._probing_ids.update(ids)
        window_s = max(1, int(config.ZERO_SEEDER_RACE_WINDOW_MINUTES)) * 60
        logger.info("Zero-seeder race started (run #%s): %d candidate(s) %s, "
                    "window %dm", run_id, len(ids), ids, window_s // 60)
        threading.Thread(
            target=self._race_monitor, args=(ids,),
            kwargs={"request_title": request_title, "duration_s": window_s},
            name="dl-zero-seeder-race", daemon=True).start()
        return ids

    def _race_fallback_want(self, media_type: str,
                            minutes: float | None) -> SelectWant:
        """A minimal, identity-less want for a race with no request/show
        context — the gates still reject CAM/oversize/zero-size candidates."""
        pref, max_rate, default_minutes = _size_prefs(media_type)
        return SelectWant(
            identity=MediaIdentity(media_type=media_type),
            size_pref_mb_min=pref, size_max_rate=max_rate,
            runtime_minutes=minutes, fallback_minutes=default_minutes,
            allow_cam=not config.BLOCK_CAMS,
            mode=torrent_select.MODE_ZERO_SEEDER_RACE,
            min_seeders=0)  # the race exists precisely to gamble on 0-seed

    def _staged_size(self, row: downloads_store.DownloadRow) -> int:
        """Total on-disk size of a download's staged media (race tie-break)."""
        total = 0
        for f in self._media_files_in_staging(row):
            try:
                total += f.stat().st_size
            except OSError:
                continue
        return total

    def _cancel_race_loser(self, download_id: int) -> None:
        """Cancel a race loser with reason race_loser — NEVER a blocklist entry
        and never a 'wrong pick' (Task C item 6.5)."""
        row = downloads_store.get_download(download_id)
        if row is not None and row.status in ("queued", "downloading", "downloaded"):
            self.cancel(download_id)
            downloads_store.add_history(
                download_id, "race_loser", before=None,
                after="zero-seeder race loser — cancelled, not blocklisted")

    def _race_finish_winner(self, download_id: int) -> bool:
        """Verify a finished race candidate BEFORE moving it. Returns True when
        it verified and moved (the winner). A failed-verify candidate is
        quarantined + blocked for the identity by _post_process itself, and the
        race continues with the rest."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return False
        request_title = _request_title_from_row(row)
        self._post_process(download_id, force_move=True, force_rename=True,
                           request_title=request_title)
        after = downloads_store.get_download(download_id)
        return after is not None and after.status == "moved"

    def _race_monitor(self, ids: list[int], *, request_title: str | None = None,
                      duration_s: int = 900, poll_s: int = 120) -> None:
        deadline = time.time() + duration_s
        remaining = list(ids)
        try:
            while time.time() < deadline and remaining:
                time.sleep(poll_s)
                rows = {d: downloads_store.get_download(d) for d in remaining}
                finished = [(d, r) for d, r in rows.items()
                            if r is not None and r.status == "downloaded"]
                if finished:
                    # Tie-break among simultaneously finished: size_bytes desc,
                    # then infohash asc (the comment's real intent; item 6.6).
                    finished.sort(key=lambda dr: (-self._staged_size(dr[1]),
                                                  _magnet_hash(dr[1].magnet)))
                    for did, _row in finished:
                        if self._race_finish_winner(did):
                            for other in remaining:
                                if other != did:
                                    self._cancel_race_loser(other)
                            logger.info("Zero-seeder race won by download #%s", did)
                            return
                        # failed verify: already quarantined+blocked; drop it.
                        remaining.remove(did)
                remaining = [d for d in remaining
                             if (r := downloads_store.get_download(d)) is not None
                             and r.status in ("queued", "downloading", "downloaded")]
                if not remaining:
                    return

            for did in remaining:
                self._cancel_race_loser(did)
        finally:
            # No candidate stays 'probing' once the race is over, whatever the
            # exit path (winner, all dropped, or the window closed).
            self._probing_ids.difference_update(ids)

    # ------------------------------------------------------------------
    # Quality replacement — swap a cam/low-bitrate movie for a proper one
    # ------------------------------------------------------------------

    def replace_low_quality_movie(self, title_query: str, old_path: str,
                                  identity: MediaIdentity | None = None) -> int | None:
        """Search for a NON-cam release of a movie and download it; the old
        file is deleted automatically once the new one lands in the library.

        Hard rules: never a cam/telesync (regardless of the global toggle —
        that's what we're replacing), never size 0, and at least the
        low-quality threshold in MB/min so we don't swap junk for junk.
        Returns the download id, or None when nothing acceptable was found.

        Identity (Task F item 4): an explicit MediaIdentity wins; otherwise it
        is resolved offline from the per-movie folder's verified {tmdb-ID} tag
        when present. A resolved identity flows into the want snapshot (so
        verification + the per-movie folder route by it) and the verified move
        writes an identity-keyed media_quality update. When it stays
        unresolved, the grab proceeds on the title alone and the quality
        write is marked source='manual-unresolved' — no identity-keyed update
        is claimed. (The library index stores no provider ids, so the folder
        tag is the only offline evidence.)
        """
        if identity is None:
            identity = identity_from_movie_path(old_path)
        try:
            pool = search_collect(title_query, "movie")
        except Exception:
            logger.exception("Replacement search failed for %s", title_query)
            return None

        # Anchor every size rule on the movie's REAL runtime — we own the old
        # file, so ffprobe it (cached) instead of assuming two hours flat.
        minutes = None
        try:
            import video_quality
            probed, _exact = video_quality.duration_minutes(
                old_path, Path(old_path).stat().st_size)
            if probed and probed > 0:
                minutes = probed
        except OSError:
            pass

        # Replacement-specific floor (don't swap junk for junk) + seeders>0
        # (replacement never races) as a pre-filter; the engine then gates
        # (CAM always blocked here) and scores what survives.
        floor_bytes = config.LOW_QUALITY_MB_PER_MIN * (minutes or 120) * 1024 * 1024
        viable = [r for r in pool.results
                  if r.size_bytes >= floor_bytes and r.seeders > 0]
        pref, max_rate, default_minutes = _size_prefs("movie")
        # Resolved identity (folder tag or caller) drives the gates; the
        # unresolved fallback keeps the title-only guard of the old shape.
        want_identity = identity if identity is not None else MediaIdentity(
            media_type="movie", canonical_title=title_query)
        want = SelectWant(
            identity=want_identity,
            size_pref_mb_min=pref, size_max_rate=max_rate,
            runtime_minutes=minutes, fallback_minutes=default_minutes,
            allow_cam=False,  # always block cams — that's what we're replacing
            mode=torrent_select.MODE_AUTOMATIC_REPLACEMENT)
        decision, by_hash = self._run_selection(
            viable, want, pool_stats=dict(pool.pool_stats))
        chosen = by_hash.get(decision.chosen_infohash) if decision.chosen else None
        if chosen is None:
            downloads_store.record_selection_run(decision)
            logger.info("No acceptable non-cam replacement found for %s", title_query)
            return None
        download_id = self.grab(
            chosen, request_title=title_query,
            auto_rename=True, auto_move=True, replace_path=old_path,
            quality_label=self._quality_label_for(decision, chosen),
            identity_override=identity,
        )
        downloads_store.record_selection_run(decision, download_id=download_id)
        logger.info("Replacement grab for %s → download #%s (%s)",
                    title_query, download_id, chosen.title)
        return download_id

    # ------------------------------------------------------------------
    # Runner subprocess
    # ------------------------------------------------------------------

    def _run_download(self, download_id: int, magnet: str, staging: str,
                      request_title: str | None) -> None:
        # Extra public trackers help poorly-announced magnets find peers.
        magnet = add_public_trackers(magnet)
        if config.QBITTORRENT_ENABLED:
            self._run_download_qbit(download_id, magnet, staging, request_title)
            return
        self._run_download_node(download_id, magnet, staging, request_title)

    def _run_download_qbit(self, download_id: int, magnet: str, staging: str,
                           request_title: str | None) -> None:
        """Delegate one download to qBittorrent and poll it to completion."""
        tag = f"prb-{download_id}"
        try:
            client = _QBitClient()
            client.login()
            client.add_magnet(magnet, staging, tag)
        except Exception as exc:
            logger.exception("qBittorrent add failed.")
            downloads_store.set_status(
                download_id, "error", error=f"qBittorrent: {exc}", completed=True)
            downloads_store.add_history(download_id, "error", before=None, after=str(exc))
            self._notify(download_id)
            return

        with self._lock:
            self._qbit_tags[download_id] = tag

        error_message: str | None = None
        last_progress_at = time.time()
        last_progress = -1.0
        files_pruned = False
        try:
            while True:
                time.sleep(3)
                row = downloads_store.get_download(download_id)
                if row is None or row.status == "cancelled":
                    return
                info = client.info_by_tag(tag)
                if info is None:
                    # Torrent may take a moment to appear after add.
                    if time.time() - last_progress_at > 60:
                        error_message = "torrent never appeared in qBittorrent"
                        break
                    continue
                # Season pack, single wanted episode: deselect every other
                # video file so only the target episode downloads.
                if (not files_pruned and row.season is not None
                        and row.episode is not None and info.get("hash")):
                    files_pruned = True
                    try:
                        self._qbit_prune_to_episode(
                            client, str(info["hash"]), row.season, row.episode)
                    except Exception:
                        logger.debug("qBittorrent file pruning failed.", exc_info=True)
                progress = float(info.get("progress") or 0.0)
                state = str(info.get("state") or "")
                if progress > last_progress:
                    last_progress = progress
                    last_progress_at = time.time()
                    downloads_store.set_progress(download_id, progress)
                    self._notify(download_id)
                if state in ("error", "missingFiles"):
                    error_message = f"qBittorrent state: {state}"
                    break
                if progress >= 1.0 or state in ("uploading", "stalledUP", "pausedUP",
                                                "queuedUP", "checkingUP", "stoppedUP"):
                    break  # download complete (seeding states)
                if time.time() - last_progress_at > config.TORRENT_STALL_TIMEOUT_SECONDS:
                    error_message = "stalled — no progress within the timeout"
                    break
        except Exception as exc:
            error_message = str(exc)
        finally:
            # Stop seeding + drop the torrent (files stay on disk).
            try:
                client.delete_by_tag(tag, delete_files=False)
            except Exception:
                logger.debug("qBittorrent post-download delete failed.", exc_info=True)
            with self._lock:
                self._qbit_tags.pop(download_id, None)

        if error_message:
            downloads_store.set_status(download_id, "error", error=error_message, completed=True)
            downloads_store.add_history(download_id, "error", before=None, after=error_message)
            self._notify(download_id)
            return

        downloads_store.set_progress(download_id, 1.0)
        downloads_store.set_status(download_id, "downloaded", completed=True)
        try:
            info = client.info_by_tag(tag)
            if info and info.get("hash"):
                rel_paths = [str(f.get("name")) for f in client.files(str(info["hash"]))
                             if f.get("name")]
                if rel_paths:
                    downloads_store.set_files(download_id, rel_paths)
        except Exception:
            logger.debug("qBittorrent file-list capture failed.", exc_info=True)
        downloads_store.add_history(download_id, "downloaded", before=None,
                                    after=f"{staging} (via qBittorrent)")
        self._notify(download_id)
        outcome = self._post_process(download_id, request_title=request_title)
        logger.info("Download #%s post-process: %s", download_id, outcome)
        self._notify(download_id)

    @staticmethod
    def _qbit_prune_to_episode(client: _QBitClient, torrent_hash: str,
                               season: int, episode: int) -> None:
        """Inside a multi-file torrent, keep only the wanted episode's video
        (plus subtitles); everything else is set to priority 0 (skip)."""
        files = client.files(torrent_hash)
        if len(files) < 2:
            return
        keep_exts = torrent_routing.VIDEO_EXTENSIONS | torrent_routing.SUBTITLE_EXTENSIONS
        wanted_videos: list[int] = []
        skip: list[int] = []
        for idx, f in enumerate(files):
            name = Path(str(f.get("name") or "")).name
            suffix = Path(name).suffix.lower()
            file_id = int(f.get("index", idx))
            if suffix not in keep_exts:
                skip.append(file_id)
                continue
            parsed = torrent_routing.parse_torrent_name(name)
            matches = (parsed.episode == episode
                       and (parsed.season is None or parsed.season == season))
            if suffix in torrent_routing.VIDEO_EXTENSIONS:
                (wanted_videos if matches else skip).append(file_id)
        # Only prune when we positively identified the target episode —
        # otherwise download everything rather than guess wrong.
        if wanted_videos:
            client.set_file_priority(torrent_hash, skip, 0)
            logger.info("qBittorrent: pruned pack to S%02dE%02d (%d file(s) skipped)",
                        season, episode, len(skip))

    def _run_download_node(self, download_id: int, magnet: str, staging: str,
                           request_title: str | None) -> None:
        # A fresh install ships the runner script without node_modules (they are
        # platform-specific native builds). Install them on the first grab
        # instead of failing with a raw ERR_MODULE_NOT_FOUND behind an opaque
        # "runner exit code 1".
        ready, why = ensure_runner_ready()
        if not ready:
            downloads_store.set_status(
                download_id, "error", error=why, completed=True)
            downloads_store.add_history(
                download_id, "error", before=None, after=why)
            self._notify(download_id)
            self._maybe_start_next()
            return

        cmd = [
            config.NODE_PATH, str(_RUNNER_PATH), magnet, staging,
            str(config.TORRENT_STALL_TIMEOUT_SECONDS),
        ]
        try:
            # stderr merges into stdout: the JSON reader skips non-JSON
            # lines anyway, and an undrained stderr pipe would fill up and
            # freeze a chatty runner mid-download.
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            logger.exception("Failed to start torrent runner.")
            downloads_store.set_status(
                download_id, "error",
                error=f"couldn't start Node runner: {exc}", completed=True,
            )
            downloads_store.add_history(download_id, "error", before=None, after=str(exc))
            self._notify(download_id)
            return

        with self._lock:
            self._processes[download_id] = proc

        torrent_files: list[dict[str, Any]] = []
        error_message: str | None = None
        metadata_seen = False
        # Until the first metadata/progress event lands, the UI shows "fetching
        # metadata" instead of a bare 0%.
        self._phase[download_id] = "fetching_metadata"
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if kind == "progress":
                    prog = float(event.get("progress") or 0)
                    downloads_store.set_progress(download_id, prog)
                    self._update_phase(download_id, prog,
                                       int(event.get("peers") or 0), metadata_seen)
                    self._notify(download_id)
                elif kind == "metadata":
                    torrent_files = event.get("files") or []
                    metadata_seen = True
                elif kind == "done":
                    torrent_files = event.get("files") or torrent_files
                elif kind == "error":
                    error_message = str(event.get("message") or "unknown runner error")
            proc.wait(timeout=30)
        except Exception as exc:
            error_message = error_message or str(exc)
        finally:
            with self._lock:
                self._processes.pop(download_id, None)
            self._phase.pop(download_id, None)

        row = downloads_store.get_download(download_id)
        if row is not None and row.status in (
                "cancelled", "queued", "error", "stopped"):
            # Someone else already took this row to a terminal/owned state — a
            # user cancel, a queue rotation, a stall->error from the monitor, or
            # an app-shutdown requeue. The kill-induced exit code must not
            # overwrite that status, re-resolve the request, or double-count the
            # failure (the stall path already recorded it).
            return

        if error_message or proc.returncode not in (0, None):
            # A missing-module exit is a setup problem, not a torrent problem —
            # say so plainly instead of leaving "exit code 1" on the row.
            if (error_message is None and proc.returncode not in (0, None)
                    and runner_missing_deps()):
                error_message = (
                    "the torrent runner's dependencies are missing. Install "
                    "Node.js 20+ and restart Sensarr, or use Settings > Setup "
                    "wizard > 'npm install torrent runner'.")
            downloads_store.set_status(
                download_id, "error",
                error=error_message or f"runner exit code {proc.returncode}",
                completed=True,
            )
            downloads_store.add_history(
                download_id, "error", before=None,
                after=error_message or f"exit {proc.returncode}",
            )
            self._notify(download_id)
            self._maybe_start_next()
            return

        downloads_store.set_progress(download_id, 1.0)
        downloads_store.set_status(download_id, "downloaded", completed=True)
        rel_paths = [str(f.get("path")) for f in torrent_files if f.get("path")]
        if rel_paths:
            downloads_store.set_files(download_id, rel_paths)
        file_list = ", ".join(rel_paths) or "?"
        downloads_store.add_history(
            download_id, "downloaded", before=None,
            after=f"{staging} :: {file_list}",
        )
        self._notify(download_id)

        # Post-process according to the flags chosen at grab time.
        outcome = self._post_process(download_id, request_title=request_title)
        logger.info("Download #%s post-process: %s", download_id, outcome)
        self._notify(download_id)
        self._maybe_start_next()

    # ------------------------------------------------------------------
    # Post-processing: rename + move with full history
    # ------------------------------------------------------------------

    def _media_files_in_staging(self, row: downloads_store.DownloadRow) -> list[Path]:
        """Locate this download's media files inside the staging dir.

        Primary source is the ENGINE-REPORTED file list stored on the row —
        exact paths, no guessing. The old fuzzy newest-first name matching
        once swapped two simultaneous downloads' files (the kanji-rename
        incident), so it survives only as a fallback for legacy rows, at a
        much higher similarity bar.
        """
        staging = Path(row.staging_dir or config.TORRENT_DOWNLOAD_DIR)
        wanted_exts = torrent_routing.VIDEO_EXTENSIONS | torrent_routing.SUBTITLE_EXTENSIONS
        if not staging.is_dir():
            return []

        if row.files_json:
            import json as _json
            try:
                rel_paths = _json.loads(row.files_json)
            except ValueError:
                rel_paths = []
            exact = [staging / rel for rel in rel_paths]
            exact = [p for p in exact
                     if p.is_file() and p.suffix.lower() in wanted_exts]
            if exact:
                return exact

        candidates: list[Path] = []
        entries = sorted(staging.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for entry in entries:
            name_match = torrent_routing._folder_similarity(entry.name, row.title) >= 0.8
            if entry.is_file() and entry.suffix.lower() in wanted_exts and name_match:
                candidates.append(entry)
            elif entry.is_dir() and name_match:
                for f in entry.rglob("*"):
                    if f.is_file() and f.suffix.lower() in wanted_exts:
                        candidates.append(f)
                break
            if candidates and entry.is_file():
                break
        return candidates

    def _post_process(
        self, download_id: int, *, force_move: bool = False,
        force_rename: bool = False, request_title: str | None = None,
    ) -> str:
        row = downloads_store.get_download(download_id)
        if row is None:
            return "row vanished"

        do_rename = force_rename or row.auto_rename
        do_move = force_move or row.auto_move

        want_dict = downloads_store.get_want(download_id)

        # Deterministic, identity-first routing (Task D): a KNOWN show_id routes
        # by show_tracker (episode or whole-season pack), a qualified movie into
        # its per-movie folder (Task D2). Fuzzy plan_route is the legacy fallback
        # only for unlinked/manual downloads.
        plan = None
        if row.show_id is not None:
            show = shows_store.get_show(row.show_id)
            if show is not None and row.season is not None and row.episode is not None:
                plan = show_tracker.plan_for_episode(show, row.season, row.episode)
            elif show is not None and row.season is not None:
                # Season pack: episode is None; each file is named per-file below.
                plan = show_tracker.plan_for_season(show, row.season)
        if plan is None and row.media_type == "movie":
            mt, my, mtmdb = _movie_route_identity_from_want(want_dict)
            plan = torrent_routing.plan_route(
                row.title, row.media_type, request_title=request_title,
                movie_title=mt, movie_year=my, movie_tmdb_id=mtmdb)
        if plan is None:
            plan = torrent_routing.plan_route(
                row.title, row.media_type, request_title=request_title
            )
        downloads_store.set_route(
            download_id, planned_dest=plan.dest_dir,
            planned_name=plan.new_filename, route_reason=plan.reason,
        )

        if not do_move and not do_rename:
            return f"left in staging (auto rename/move off) — planned: {plan.describe()}"
        if not plan.confident:
            # Never move on a shaky route; the file stays findable in staging,
            # and (Task D item 3) a needs-placement row surfaces it in the grab
            # queue with a one-click create-folder action.
            self._record_needs_placement(row, plan)
            return f"left in staging — route not confident: {plan.reason}"

        files = self._media_files_in_staging(row)
        if not files:
            return "no media files found in staging for this download"

        # -- IDENTITY GATE (verify BEFORE any move; Design stance 5) ----------
        # Compare the ACTUAL staged files against the immutable want_json via
        # media_identity. A contradictory identity-gating file (a sequel payload,
        # a wrong-country edition, a contradicting season) fails the whole
        # download: NO move, quarantine, blocklist, request reopens.
        if want_dict is not None:
            want_identity = verification.identity_from_want(want_dict)
            result = verification.verify_staging(
                files, want_identity,
                video_exts=torrent_routing.VIDEO_EXTENSIONS,
                subtitle_exts=torrent_routing.SUBTITLE_EXTENSIONS)
            if not result.ok:
                if result.reason_code == "no_media":
                    # Nothing identity-gating to place (samples/extras/subs
                    # only): move NOTHING — non-gating files never enter a
                    # library root on their own (verifier note N3).
                    return ("left in staging — no primary/episode video to "
                            "place (samples/extras/subtitles only)")
                return self._quarantine_payload(
                    row, want_identity, result.reason_code, result.detail,
                    offending=result.offending)

        # Canonical show name for renames: the tracked show's title for
        # episode-linked grabs, else the matched library folder's name.
        show_name = None
        if row.show_id is not None:
            show = shows_store.get_show(row.show_id)
            if show is not None:
                show_name = show.title
        if show_name is None and plan.show_folder:
            show_name = Path(plan.show_folder).name

        video_files = [f for f in files if f.suffix.lower() in torrent_routing.VIDEO_EXTENSIONS]

        # Fresh provenance rows for this pass (idempotent re-runs).
        downloads_store.clear_download_files(download_id)
        dest_dir = Path(plan.dest_dir)
        moved_gating = 0          # gating (primary/episode) files placed
        gating_total = 0
        skipped_gating = 0
        moved_final_paths: list[str] = []

        # VobSub pairing (Task D2 item 4): a .sub/.idx track is only usable with
        # its partner. Move both or neither — a lone half is broken and skipped.
        vobsub_pair: dict[tuple[str, str], set[str]] = {}
        for f in files:
            if f.suffix.lower() in torrent_routing.VOBSUB_EXTENSIONS:
                vobsub_pair.setdefault(
                    (str(f.parent), f.stem.casefold()), set()).add(f.suffix.lower())
        # Subtitle matched-basename collision tracking within this pass; the
        # vobsub partner reuses its pair's resolved base rather than colliding.
        used_sub_names: set[str] = set()
        sub_base_for_stem: dict[tuple[str, str], str] = {}
        movie_video_stem = plan.new_filename if row.media_type == "movie" else None

        # Collision safety (Phase 5 gate: collisions never overwrite).
        # prior_intended: names THIS download planned in an EARLIER pass — the
        # only targets whose smaller on-disk copy may be treated as our own
        # interrupted partial move. Snapshot BEFORE the loop (the loop appends
        # its own 'renamed' rows). placed_this_pass: targets already taken by
        # another file of this payload in THIS pass.
        prior_intended = {
            h.after_value for h in downloads_store.history_for_download(download_id)
            if h.action in ("renamed", "moved") and h.after_value
        }
        placed_this_pass: set[str] = set()

        roles: dict[str, str] = {}
        for src in files:
            roles[str(src)] = verification.classify_role(
                src, is_video=(src.suffix.lower() in torrent_routing.VIDEO_EXTENSIONS),
                is_subtitle=(src.suffix.lower() in torrent_routing.SUBTITLE_EXTENSIONS),
                single_video=(len(video_files) == 1),
                want=(verification.identity_from_want(want_dict)
                      if want_dict else MediaIdentity(media_type=row.media_type)))
        # Videos that will actually be PLACED — the multipart "- cdN" naming
        # only applies when more than one of these exists (a sample/extra never
        # makes a movie look multipart).
        gating_video_count = sum(
            1 for f in video_files
            if roles[str(f)] in verification._IDENTITY_GATING_ROLES)

        for src in files:
            suffix = src.suffix.lower()
            is_video = suffix in torrent_routing.VIDEO_EXTENSIONS
            is_sub = suffix in torrent_routing.SUBTITLE_EXTENSIONS
            role = roles[str(src)]
            is_gating = role in verification._IDENTITY_GATING_ROLES
            if is_gating:
                gating_total += 1

            def _record(state: str, *, final: str | None = None,
                        reason: str | None = None) -> None:
                import datetime as _dt
                pf = verification.parse_file(src)
                downloads_store.add_download_file(
                    download_id,
                    source_relative_path=src.name,
                    source_absolute_path=str(src),
                    media_role=role, parsed_title=pf.parsed_title,
                    parsed_year=pf.year,
                    parsed_season=(pf.seasons[0] if pf.seasons else None),
                    parsed_episode=(pf.episodes[0] if pf.episodes else None),
                    size_bytes=(src.stat().st_size if src.exists() else None),
                    final_path=final, verification_state=state,
                    verification_reason=reason,
                    moved_at=(_dt.datetime.now(_dt.timezone.utc).isoformat()
                              if final else None))

            # ROLE GATE (verification.py contract): samples, extras, and
            # unknown files are NEVER moved to a library root. Only verified
            # primary/episode videos and their subtitles may move; the rest
            # stay in staging with the skip recorded in provenance.
            if role in (verification.ROLE_SAMPLE, verification.ROLE_EXTRA,
                        verification.ROLE_UNKNOWN):
                _record("skipped",
                        reason=f"role {role} never moves to a library root")
                continue

            target_name = src.name
            target_dir = dest_dir
            if is_sub:
                # A lone VobSub half (only .sub OR only .idx) is broken — skip it.
                if suffix in torrent_routing.VOBSUB_EXTENSIONS:
                    have = vobsub_pair.get((str(src.parent), src.stem.casefold()), set())
                    if len(have & torrent_routing.VOBSUB_EXTENSIONS) < 2:
                        _record("skipped",
                                reason="lone VobSub half (missing .sub/.idx partner)")
                        continue
                # Multi-sub packs carry subtitles for every language — only the
                # configured language (and untagged defaults) come along.
                try:
                    from subtitles import subtitle_language_ok
                    if not subtitle_language_ok(src):
                        _record("skipped", reason="subtitle language filtered")
                        continue
                except ImportError:
                    pass
                if config.SUBTITLE_SUBFOLDER:
                    target_dir = dest_dir / "Subs"
                if do_rename:
                    base = self._subtitle_base_for(
                        src, show_name=show_name, plan=plan,
                        movie_stem=movie_video_stem,
                        used=used_sub_names, reserved=sub_base_for_stem)
                    if base:
                        target_name = f"{base}{suffix}"
            elif do_rename and is_video:
                new_stem = self._episode_stem_for_file(
                    src, show_name=show_name, plan=plan,
                    single_video=(gating_video_count <= 1),
                )
                if new_stem:
                    target_name = f"{new_stem}{suffix}"
            if target_name != src.name:
                downloads_store.add_history(
                    download_id, "renamed", before=src.name, after=target_name,
                )
            if do_move:
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / target_name
                if str(target).casefold() in placed_this_pass:
                    # Two DIFFERENT payload files resolved to the same name
                    # (e.g. two videos mapping to the same "- cdN"). Never
                    # touch what we just placed — divert to a suffixed sibling.
                    n = 2
                    while True:
                        cand = target_dir / f"{target.stem} ({n}){target.suffix}"
                        if (str(cand).casefold() not in placed_this_pass
                                and not cand.exists()):
                            break
                        n += 1
                    downloads_store.add_history(
                        download_id, "collision", before=str(target),
                        after=f"name already taken by another file of this "
                              f"download — kept as {cand.name}")
                    target = cand
                if (target.exists() and row.replace_path
                        and str(target) == str(row.replace_path)):
                    # The planned destination IS the low-quality file this
                    # download was grabbed to REPLACE (same canonical name in
                    # the same per-movie folder). Retire it now — recycle bin
                    # first — so the verified replacement can land; checked
                    # BEFORE the same-size/duplicate branches so the old copy
                    # can never win by colliding with its own replacement.
                    try:
                        from send2trash import send2trash
                        send2trash(str(target))
                    except Exception:
                        target.unlink(missing_ok=True)
                    downloads_store.add_history(
                        download_id, "replaced", before=str(target),
                        after="old low-quality copy recycled to make room "
                              "for its replacement")
                if target.exists():
                    try:
                        same_size = target.stat().st_size == src.stat().st_size
                    except OSError:
                        same_size = False
                    if same_size:
                        try:
                            from send2trash import send2trash
                            send2trash(str(src))
                        except Exception:
                            src.unlink(missing_ok=True)
                        downloads_store.add_history(
                            download_id, "duplicate", before=str(src),
                            after=f"already in library ({target}) — staged copy recycled",
                        )
                        _record("duplicate", final=str(target),
                                reason="already in library — staged copy recycled")
                        if is_gating:
                            moved_gating += 1
                            moved_final_paths.append(str(target))
                        continue
                    try:
                        target_smaller = target.stat().st_size < src.stat().st_size
                    except OSError:
                        target_smaller = False
                    # A smaller file at the target is replaced ONLY when it is
                    # attributable to THIS download's own interrupted earlier
                    # move (same release filename, or a name this download
                    # planned before). An unattributed file is someone else's
                    # media and is NEVER overwritten.
                    ours = (target.name == src.name
                            or target.name in prior_intended
                            or str(target) in prior_intended)
                    if target_smaller and ours:
                        # Interrupted cross-drive move left a partial copy —
                        # replace it with the complete staged file.
                        target.unlink(missing_ok=True)
                        downloads_store.add_history(
                            download_id, "replaced", before=str(target),
                            after="partial copy from an interrupted move — replaced",
                        )
                        # fall through to the normal move below
                    else:
                        downloads_store.add_history(
                            download_id, "error", before=str(src),
                            after=f"NOT moved — a DIFFERENT file exists at: {target}",
                        )
                        _record("failed",
                                reason=f"a different file exists at {target}")
                        if is_gating:
                            skipped_gating += 1
                        continue
                shutil.move(str(src), str(target))
                placed_this_pass.add(str(target).casefold())
                downloads_store.add_history(
                    download_id, "moved", before=str(src), after=str(target),
                )
                _record("verified", final=str(target))
                if is_gating:
                    moved_gating += 1
                    moved_final_paths.append(str(target))
                try:
                    from library_index import log_file_event
                    log_file_event("added", str(target),
                                   f"downloaded by Sensarr (download #{download_id})")
                except Exception:
                    pass
            elif target_name != src.name:
                # Rename in place (staging) without moving.
                target = src.with_name(target_name)
                if not target.exists():
                    src.rename(target)
                _record("skipped", reason="renamed in staging (move off)")
            else:
                _record("skipped", reason="left in staging (move off)")

        moved_any = moved_gating > 0
        if moved_any:
            # Per-file rollup: partial when a gating file could not be placed.
            partial = skipped_gating > 0 or moved_gating < gating_total
            downloads_store.set_verification(
                download_id, "partial" if partial else "verified",
                reason=(f"{moved_gating}/{gating_total} gating files placed"
                        if partial else None))
            downloads_store.set_status(download_id, "moved", completed=True)
            self._cleanup_staging_leftovers(row)
            # Quality replacement: the new file is in place — retire the old
            # cam/low-bitrate copy it replaces (recycle bin when available).
            if row.replace_path:
                old = Path(row.replace_path)
                if old.is_file():
                    try:
                        try:
                            from send2trash import send2trash
                            send2trash(str(old))
                        except ImportError:
                            old.unlink()
                        downloads_store.add_history(
                            download_id, "replaced", before=str(old),
                            after=f"deleted — superseded by download #{download_id}",
                        )
                        try:
                            from library_index import (log_file_event,
                                                       remove_from_index)
                            remove_from_index([str(old)])
                            log_file_event(
                                "replaced", str(old),
                                f"low-quality copy recycled by Sensarr — "
                                f"superseded by download #{download_id}")
                        except Exception:
                            pass
                        logger.info("Replaced low-quality file: %s", old)
                    except OSError as exc:
                        downloads_store.add_history(
                            download_id, "error", before=str(old),
                            after=f"could not delete replaced file: {exc}",
                        )
            # Close the loop for tracked episodes: mark it on-disk right away
            # instead of waiting for the next full sync.
            if row.show_id is not None and row.season is not None and row.episode is not None:
                moved_video = next(
                    (h.after_value for h in downloads_store.list_history(limit=20)
                     if h.download_id == download_id and h.action == "moved"
                     and h.after_value),
                    str(dest_dir),
                ) or str(dest_dir)
                shows_store.set_episode_file(row.show_id, row.season, row.episode, moved_video)
            elif row.show_id is not None and row.season is not None:
                # Season pack (episode None): record EACH placed episode from its
                # per-file provenance, and map the show folder so later seasons
                # land beside it (Task D — closes the tracking loop for packs).
                if plan.show_folder:
                    shows_store.add_show_folder(row.show_id, plan.show_folder)
                for f in downloads_store.list_download_files(download_id):
                    if (f.verification_state in ("verified", "duplicate")
                            and f.parsed_episode is not None and f.final_path):
                        shows_store.set_episode_file(
                            row.show_id, row.season, int(f.parsed_episode),
                            f.final_path)
            # Task F: persist this release's quality label onto the QUALIFIED
            # identity for every verified placed file — the label now rides
            # the identity, not the filename, and survives later renames.
            try:
                self._record_quality_labels(row, want_dict)
            except Exception:
                logger.exception("media_quality write failed for #%s", download_id)
            # A per-movie folder created without a verified TMDB tag stays
            # visible for later metadata repair (Task D2 item 1).
            if getattr(plan, "missing_folder_id", False):
                downloads_store.add_history(
                    download_id, "missing_folder_id", before=str(dest_dir),
                    after="per-movie folder created without a verified TMDB id")
            # Provenance link + AGGREGATE fulfillment (Task C item 4): the
            # download is placed; whether the REQUEST is fulfilled is decided by
            # the aggregate (a first episode never completes a season).
            self._on_download_placed(row, moved_final_paths)
            return f"moved to {dest_dir}"

        # Nothing placed. If a gating file existed but could not be moved (e.g.
        # a different larger file already sits at every target), the request
        # needs a human — surface it instead of silently succeeding.
        if skipped_gating > 0:
            downloads_store.set_verification(
                download_id, "failed", reason="no gating file could be placed")
            if row.request_id is not None:
                queue_store.set_status(row.request_id,
                                       queue_store.STATUS_NEEDS_ATTENTION)
        return f"processed (no move) — planned: {plan.describe()}"

    def _record_quality_labels(self, row: downloads_store.DownloadRow,
                               want_dict: dict | None) -> None:
        """Task F verified-move hook: write media_quality for every VERIFIED
        placed gating file. Movies key on the qualified provider identity from
        the frozen want; a movie with NO qualified identity (the unresolved
        manual replacement shape) is path-keyed and marked manual-unresolved —
        an identity-keyed update is never claimed for it. Episodes key on the
        internal (show_id, season, episode) coordinates. The label is the one
        parsed at grab time (downloads.quality_label), so cause and provenance
        stay tied to the release that supplied the file."""
        label = row.quality_label or torrent_select.parse_quality_label(row.title)
        if label is None:
            return
        cause = "replacement" if row.replace_path else "verified_move"
        placed = [f for f in downloads_store.list_download_files(row.download_id)
                  if f.verification_state == "verified" and f.final_path
                  and f.media_role in (verification.ROLE_PRIMARY_VIDEO,
                                       verification.ROLE_EPISODE)]
        if not placed:
            return

        media_type = (want_dict or {}).get("media_type") or row.media_type
        if media_type == "movie":
            primary = max(placed, key=lambda f: f.size_bytes or 0)
            source = (want_dict or {}).get("identity_source")
            ext = (want_dict or {}).get("external_id")
            if source and ext:
                media_quality.record_quality(
                    media_quality.movie_identity_key(source, str(ext)),
                    quality_label=label, file_path=primary.final_path,
                    media_type="movie", identity_source=source,
                    external_id=str(ext),
                    source=media_quality.SOURCE_PARSED, cause=cause)
            else:
                media_quality.record_quality(
                    media_quality.path_identity_key(primary.final_path),
                    quality_label=label, file_path=primary.final_path,
                    media_type="movie",
                    source=media_quality.SOURCE_MANUAL_UNRESOLVED, cause=cause)
            return

        if row.show_id is None:
            return  # unlinked episodic payload — no durable identity to key
        for f in placed:
            season = f.parsed_season if f.parsed_season is not None else row.season
            episode = (f.parsed_episode if f.parsed_episode is not None
                       else row.episode)
            if season is None or episode is None:
                continue
            media_quality.record_quality(
                media_quality.episode_identity_key(
                    row.show_id, int(season), int(episode)),
                quality_label=label, file_path=f.final_path,
                media_type=media_type, show_id=row.show_id,
                season=int(season), episode=int(episode),
                source=media_quality.SOURCE_PARSED, cause=cause)

    @staticmethod
    def _episode_stem_for_file(
        src: Path, *, show_name: str | None,
        plan: torrent_routing.RoutePlan, single_video: bool,
    ) -> str | None:
        """Canonical stem ("Show - S01E05") for one video file.

        Season packs are the reason this parses PER FILE: the pack-level plan
        only knows the season, so renaming every file to the same plan-level
        name would collide (and previously they were left unrenamed). A file
        whose own name parses to an episode gets its own SxxEyy; otherwise a
        lone video can still use the plan's single-episode name."""
        if show_name:
            parsed = torrent_routing.parse_torrent_name(src.name)
            if parsed.episode is not None:
                season = parsed.season
                if season is None:
                    season = (plan.parsed.season if plan.parsed and plan.parsed.season else 1)
                return torrent_routing.sanitize_for_filesystem(
                    f"{show_name} - S{season:02d}E{parsed.episode:02d}"
                )
            return None
        # Movie (no show_name): the plan stem is "<Title (Year)>". A single
        # primary video ALWAYS takes the plain stem — a "Part 2" in the movie's
        # own title ("Mockingjay Part 2") is not a disc marker. Only a payload
        # with several primary videos keeps "- cdN" suffixes so discs don't
        # collide. Extras/samples never reach here (role-gated away).
        if plan.new_filename:
            if not single_video:
                m = _MULTIPART_RE.search(src.name)
                if m:
                    return torrent_routing.sanitize_for_filesystem(
                        f"{plan.new_filename} - cd{int(m.group(1))}")
            return plan.new_filename
        return None

    @staticmethod
    def _subtitle_base_for(
        src: Path, *, show_name: str | None, plan: torrent_routing.RoutePlan,
        movie_stem: str | None, used: set[str],
        reserved: dict[tuple[str, str], str],
    ) -> str | None:
        """Matched-basename stem for a subtitle: `<video stem>.<lang>[.forced|
        .sdh]` (Task D2 item 3). Duplicate-language collisions get a `.N` suffix
        (never an overwrite); a VobSub .sub/.idx pair reuses one resolved base so
        the two halves match. Returns None when no video stem can be derived."""
        from subtitles import parse_subtitle_identity, subtitle_stem
        pair_key = (str(src.parent), src.stem.casefold())
        if pair_key in reserved:
            return reserved[pair_key]  # VobSub partner — reuse the pair's base

        # The video stem this subtitle pairs with.
        video_stem: str | None = None
        if show_name:
            parsed = torrent_routing.parse_torrent_name(src.name)
            if parsed.episode is not None:
                season = parsed.season or (
                    plan.parsed.season if plan.parsed and plan.parsed.season else 1)
                video_stem = torrent_routing.sanitize_for_filesystem(
                    f"{show_name} - S{season:02d}E{parsed.episode:02d}")
        else:
            video_stem = movie_stem
        if not video_stem:
            return None

        identity = parse_subtitle_identity(src)
        base = subtitle_stem(video_stem, identity)
        candidate, n = base, 2
        while candidate in used:
            candidate = f"{base}.{n}"
            n += 1
        used.add(candidate)
        reserved[pair_key] = candidate
        return candidate

    def _record_needs_placement(self, row: downloads_store.DownloadRow,
                                plan: torrent_routing.RoutePlan) -> None:
        """Surface a download that couldn't route confidently as a grab-queue
        needs-placement row (Task D item 3), with a suggested folder when the
        show identity gives us one."""
        suggested = None
        if row.show_id is not None and row.season is not None:
            show = shows_store.get_show(row.show_id)
            if show is not None:
                roots = [p for p in config.media_paths_for_types(show.media_type)
                         if Path(p).is_dir()]
                root = torrent_routing.pick_root_by_free_space(roots) if roots else None
                base = root or (show.folders[0] if show.folders else None)
                if base is not None:
                    show_name = torrent_routing.sanitize_for_filesystem(show.title)
                    suggested = str(Path(base) / show_name / f"Season {row.season:02d}")
        downloads_store.record_needs_placement(
            row.download_id, show_id=row.show_id, season=row.season,
            suggested_dir=suggested, reason=plan.reason)

    def create_placement_folder(self, download_id: int,
                                dest_dir: str | None = None) -> str:
        """One-click action behind a grab-queue needs-placement row (Task D item
        3): create the destination folder, map it to the show, and re-run
        placement so the staged files move. `dest_dir` overrides the suggestion."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return "row vanished"
        np = downloads_store.get_needs_placement(download_id)
        target = dest_dir or (np.suggested_dir if np else None)
        if not target:
            return "no destination folder to create — supply one"
        tp = Path(target)
        tp.mkdir(parents=True, exist_ok=True)
        if row.show_id is not None and row.season is not None:
            # Map the show folder (parent of "Season NN") and pin the season
            # target so plan_for_season routes here deterministically.
            show_dir = tp.parent if tp.name.lower().startswith("season") else tp
            shows_store.add_show_folder(row.show_id, str(show_dir))
            shows_store.set_season_target(row.show_id, row.season, str(tp))
        downloads_store.clear_needs_placement(download_id)
        out = self._post_process(download_id, force_move=True, force_rename=True)
        return f"created {target} — {out}"

    def _cleanup_staging_leftovers(self, row: downloads_store.DownloadRow) -> None:
        """Remove the download's now-empty (or junk-only) staging folder."""
        staging = Path(row.staging_dir or config.TORRENT_DOWNLOAD_DIR)
        if not staging.is_dir():
            return
        for entry in staging.iterdir():
            if not entry.is_dir():
                continue
            if torrent_routing._folder_similarity(entry.name, row.title) < 0.5:
                continue
            remaining = [f for f in entry.rglob("*") if f.is_file()]
            junk_exts = {".nfo", ".txt", ".jpg", ".png", ".sfv", ".exe", ".url"}
            if all(f.suffix.lower() in junk_exts for f in remaining):
                shutil.rmtree(entry, ignore_errors=True)
            break

    def _notify(self, download_id: int) -> None:
        if self._on_update is not None:
            try:
                self._on_update(download_id)
            except Exception:
                logger.exception("Download update callback failed.")


# Re-exported for backwards compatibility; the canonical home is
# torrent_routing.sanitize_for_filesystem.
sanitize_for_filesystem = torrent_routing.sanitize_for_filesystem
