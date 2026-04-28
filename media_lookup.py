# =============================================================================
# media_lookup.py
# =============================================================================
# External media database search for the Plex request flow.
#
# Supports:
#   TMDB  — movies + TV shows  (requires TMDB_API_KEY)
#   TVDB  — TV shows, primary  (requires TVDB_API_KEY)
#   Jikan — MAL wrapper, anime (no key needed; rate-limited to ~3 req/s)
#   AniDB — xAnime/hentai      (uses daily public title dump; no auth needed)
#
# All HTTP calls are synchronous and are meant to be called from a thread
# executor so they don't block the async Telegram event loop.
# =============================================================================

import gzip
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_USER_AGENT = f"PlexResetButton/{config.APP_VERSION} (household-media-manager)"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedRequest:
    """One parsed item from a comma-separated user request string."""
    original: str           # exactly what the user typed for this item
    title: str              # cleaned title (no year, no qualifier)
    year: int | None        # optional (YYYY) from user input
    qualifier: str | None   # optional [season / arc] for anime

    def display(self) -> str:
        parts = [self.title]
        if self.qualifier:
            parts.append(f"[{self.qualifier}]")
        if self.year:
            parts.append(f"({self.year})")
        return " ".join(parts)


@dataclass
class MediaResult:
    """One candidate result from an external media DB search."""
    title: str
    year: int | None
    external_id: str
    external_url: str
    media_type: str         # "movie" | "tv" | "anime" | "xanime"
    overview: str
    source: str             # "tmdb" | "tvdb" | "jikan" | "anidb"
    qualifier: str | None = None   # anime season/arc from the parsed request


@dataclass
class LookupResult:
    """Full lookup result for one ParsedRequest."""
    request: ParsedRequest
    in_library: bool
    library_matches: list[str]      # matched titles already in Plex
    external_matches: list[MediaResult]
    best_match: MediaResult | None


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def parse_request_list(text: str) -> list[ParsedRequest]:
    """
    Parse a comma-separated list of media titles.

    Supported extras:
      (YYYY)   — release year (extracted, stripped from title)
      [text]   — qualifier like "Season 2" or "OVA" (for anime)

    Examples:
        "Inception (2010), Dune Part Two"
        "Attack on Titan [Final Season], Frieren (2023)"
    """
    results: list[ParsedRequest] = []

    for raw_part in text.split(","):
        raw_part = raw_part.strip()
        if not raw_part:
            continue

        working = raw_part

        # Extract [qualifier] — anime season / arc
        qualifier: str | None = None
        m = re.search(r"\[([^\]]*)\]", working)
        if m:
            qualifier = m.group(1).strip() or None
            working = (working[: m.start()] + working[m.end() :]).strip()

        # Extract (year) at end
        year: int | None = None
        m = re.search(r"\((\d{4})\)\s*$", working)
        if m:
            try:
                year = int(m.group(1))
            except ValueError:
                pass
            working = working[: m.start()].strip()

        title = working.strip()
        if not title:
            continue

        results.append(ParsedRequest(
            original=raw_part,
            title=title,
            year=year,
            qualifier=qualifier,
        ))

    return results


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 10) -> dict:
    req_headers: dict[str, str] = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error for {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {url}") from exc


def _post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> dict:
    req_headers: dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from POST {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection error for POST {url}: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# TMDB — movies + TV shows
# ---------------------------------------------------------------------------

_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_MOVIE_WEB = "https://www.themoviedb.org/movie"
_TMDB_TV_WEB = "https://www.themoviedb.org/tv"


def _tmdb_enabled() -> bool:
    return bool(config.TMDB_API_KEY)


def search_tmdb_movies(title: str, year: int | None = None) -> list[MediaResult]:
    """Search TMDB for movies. Returns up to 5 ranked results."""
    if not _tmdb_enabled():
        logger.warning("TMDB_API_KEY not set — movie search unavailable.")
        return []

    params: dict[str, str] = {
        "api_key": config.TMDB_API_KEY,
        "query": title,
        "include_adult": "false",
        "language": "en-US",
        "page": "1",
    }
    if year:
        params["year"] = str(year)

    url = f"{_TMDB_BASE}/search/movie?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except RuntimeError as exc:
        logger.error("TMDB movie search failed: %s", exc)
        return []

    results: list[MediaResult] = []
    for item in (data.get("results") or [])[:5]:
        tmdb_id = item.get("id")
        release_year: int | None = None
        rd = item.get("release_date") or ""
        if rd and len(rd) >= 4:
            try:
                release_year = int(rd[:4])
            except ValueError:
                pass

        results.append(MediaResult(
            title=item.get("title") or item.get("original_title") or "Unknown",
            year=release_year,
            external_id=str(tmdb_id) if tmdb_id else "",
            external_url=f"{_TMDB_MOVIE_WEB}/{tmdb_id}" if tmdb_id else "",
            media_type="movie",
            overview=(item.get("overview") or "")[:200],
            source="tmdb",
        ))

    return results


def search_tmdb_shows(title: str, year: int | None = None) -> list[MediaResult]:
    """Search TMDB for TV shows. Returns up to 5 ranked results."""
    if not _tmdb_enabled():
        logger.warning("TMDB_API_KEY not set — TV search via TMDB unavailable.")
        return []

    params: dict[str, str] = {
        "api_key": config.TMDB_API_KEY,
        "query": title,
        "include_adult": "false",
        "language": "en-US",
        "page": "1",
    }
    if year:
        params["first_air_date_year"] = str(year)

    url = f"{_TMDB_BASE}/search/tv?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except RuntimeError as exc:
        logger.error("TMDB TV search failed: %s", exc)
        return []

    results: list[MediaResult] = []
    for item in (data.get("results") or [])[:5]:
        tmdb_id = item.get("id")
        release_year: int | None = None
        fa = item.get("first_air_date") or ""
        if fa and len(fa) >= 4:
            try:
                release_year = int(fa[:4])
            except ValueError:
                pass

        results.append(MediaResult(
            title=item.get("name") or item.get("original_name") or "Unknown",
            year=release_year,
            external_id=str(tmdb_id) if tmdb_id else "",
            external_url=f"{_TMDB_TV_WEB}/{tmdb_id}" if tmdb_id else "",
            media_type="tv",
            overview=(item.get("overview") or "")[:200],
            source="tmdb",
        ))

    return results


# ---------------------------------------------------------------------------
# TVDB — TV shows (primary for show requests)
# ---------------------------------------------------------------------------

_TVDB_BASE = "https://api4.thetvdb.com/v4"
_TVDB_WEB = "https://thetvdb.com/series"

_tvdb_token: str | None = None
_tvdb_token_expires: float = 0.0


def _tvdb_get_token() -> str | None:
    global _tvdb_token, _tvdb_token_expires

    if not config.TVDB_API_KEY:
        return None

    if _tvdb_token and time.time() < _tvdb_token_expires:
        return _tvdb_token

    try:
        resp = _post_json(f"{_TVDB_BASE}/login", {"apikey": config.TVDB_API_KEY})
        token = (resp.get("data") or {}).get("token")
        if token:
            _tvdb_token = token
            _tvdb_token_expires = time.time() + 3600 * 23  # 23 h to be safe
            return _tvdb_token
    except RuntimeError as exc:
        logger.error("TVDB login failed: %s", exc)

    return None


def search_tvdb_shows(title: str, year: int | None = None) -> list[MediaResult]:
    """Search TVDB for TV series. Returns up to 5 results."""
    token = _tvdb_get_token()
    if not token:
        logger.warning("TVDB not available (missing key or auth failure).")
        return []

    params: dict[str, str] = {"query": title, "type": "series", "limit": "5"}
    if year:
        params["year"] = str(year)

    url = f"{_TVDB_BASE}/search?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url, headers={"Authorization": f"Bearer {token}"})
    except RuntimeError as exc:
        logger.error("TVDB search failed: %s", exc)
        return []

    results: list[MediaResult] = []
    for item in (data.get("data") or [])[:5]:
        tvdb_id = item.get("tvdb_id") or item.get("id")
        slug = item.get("slug") or ""
        release_year: int | None = None
        raw_year = item.get("year") or (item.get("first_air_time") or "")[:4]
        if raw_year:
            try:
                release_year = int(str(raw_year)[:4])
            except ValueError:
                pass

        ext_url = (
            f"{_TVDB_WEB}/{slug}"
            if slug
            else f"https://thetvdb.com/?id={tvdb_id}&tab=series"
        )

        results.append(MediaResult(
            title=item.get("name") or item.get("title") or "Unknown",
            year=release_year,
            external_id=str(tvdb_id) if tvdb_id else "",
            external_url=ext_url,
            media_type="tv",
            overview=(item.get("overview") or "")[:200],
            source="tvdb",
        ))

    return results


# ---------------------------------------------------------------------------
# Jikan (MAL wrapper) — anime + xAnime
# ---------------------------------------------------------------------------

_JIKAN_BASE = "https://api.jikan.moe/v4"
_MAL_ANIME_WEB = "https://myanimelist.net/anime"
_jikan_last_request: float = 0.0
_JIKAN_RATE_LIMIT_S = 0.4  # Jikan allows 3/s; stay polite


def _jikan_throttle() -> None:
    global _jikan_last_request
    now = time.time()
    wait = _JIKAN_RATE_LIMIT_S - (now - _jikan_last_request)
    if wait > 0:
        time.sleep(wait)
    _jikan_last_request = time.time()


def search_jikan_anime(title: str, *, explicit: bool = False) -> list[MediaResult]:
    """
    Search MAL via the Jikan v4 API.

    explicit=True filters to rating=rx (hentai on MAL).
    explicit=False adds sfw=true to exclude adult content from results.
    """
    _jikan_throttle()

    params: dict[str, str] = {
        "q": title,
        "limit": "5",
        "order_by": "score",
        "sort": "desc",
    }
    if explicit:
        params["rating"] = "rx"
    else:
        params["sfw"] = "true"

    url = f"{_JIKAN_BASE}/anime?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except RuntimeError as exc:
        logger.error("Jikan anime search failed: %s", exc)
        return []

    results: list[MediaResult] = []
    for item in (data.get("data") or [])[:5]:
        mal_id = item.get("mal_id")
        title_str = item.get("title_english") or item.get("title") or "Unknown"

        year: int | None = None
        y = item.get("year")
        if not y:
            y = ((item.get("aired") or {}).get("prop") or {}).get("from", {}).get("year")
        if y:
            try:
                year = int(y)
            except (TypeError, ValueError):
                pass

        episodes = item.get("episodes")
        synopsis = (item.get("synopsis") or "")[:180]
        if episodes:
            synopsis = f"{episodes} ep. | " + synopsis

        results.append(MediaResult(
            title=title_str,
            year=year,
            external_id=str(mal_id) if mal_id else "",
            external_url=f"{_MAL_ANIME_WEB}/{mal_id}" if mal_id else "",
            media_type="xanime" if explicit else "anime",
            overview=synopsis,
            source="jikan",
        ))

    return results


# ---------------------------------------------------------------------------
# AniDB — xAnime (explicit anime) via public title dump
# ---------------------------------------------------------------------------
#
# AniDB's HTTP API doesn't expose a title-search endpoint, but they publish a
# full compressed title dump at a public URL, updated daily.  We download it
# once and cache it locally; subsequent calls are pure in-memory lookups.
#
# File format:  aid|type|lang|title
# type codes:   1=primary  2=synonyms  3=short  4=official
# ---------------------------------------------------------------------------

_ANIDB_TITLES_URL = "https://anidb.net/api/anime-titles.dat.gz"
_ANIDB_ANIME_WEB = "https://anidb.net/anime"
_ANIDB_CACHE_FILE = "anidb_titles.dat.gz"
_ANIDB_CACHE_MAX_AGE_S = 24 * 3600

# In-memory title index: title_lower → list of (display_title, aid_str)
_anidb_index: dict[str, list[tuple[str, str]]] | None = None
_anidb_index_loaded_at: float = 0.0


def _refresh_anidb_cache() -> Path:
    """Download AniDB title dump if missing or older than 24 h."""
    cache_path = Path(config.APP_DIR) / _ANIDB_CACHE_FILE
    now = time.time()

    if cache_path.exists() and (now - cache_path.stat().st_mtime) < _ANIDB_CACHE_MAX_AGE_S:
        return cache_path

    try:
        logger.info("Downloading AniDB title dump …")
        req = urllib.request.Request(
            _ANIDB_TITLES_URL,
            headers={"User-Agent": _USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        cache_path.write_bytes(data)
        logger.info("AniDB title dump saved (%d bytes compressed).", len(data))
    except Exception as exc:
        logger.error("Failed to download AniDB title dump: %s", exc)
        if not cache_path.exists():
            raise RuntimeError("AniDB title dump unavailable.") from exc
        logger.warning("Using stale AniDB cache as fallback.")

    return cache_path


def _load_anidb_index() -> dict[str, list[tuple[str, str]]]:
    """Build or return the cached AniDB title index."""
    global _anidb_index, _anidb_index_loaded_at

    now = time.time()
    if _anidb_index is not None and (now - _anidb_index_loaded_at) < _ANIDB_CACHE_MAX_AGE_S:
        return _anidb_index

    try:
        cache_path = _refresh_anidb_cache()
    except RuntimeError:
        _anidb_index = {}
        return {}

    primary_titles: dict[str, str] = {}  # aid → primary title
    raw_index: dict[str, list[tuple[str, str]]] = {}  # title_lower → [(raw_title, aid)]

    try:
        with gzip.open(cache_path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|", 3)
                if len(parts) < 4:
                    continue
                aid, ttype, _lang, title = parts

                if ttype == "1":
                    primary_titles[aid] = title

                title_lower = title.casefold()
                bucket = raw_index.setdefault(title_lower, [])
                if not any(a == aid for _, a in bucket):
                    bucket.append((title, aid))
    except Exception as exc:
        logger.error("Failed to parse AniDB title dump: %s", exc)
        _anidb_index = {}
        return {}

    # Replace raw title with the primary title for the same AID
    final: dict[str, list[tuple[str, str]]] = {}
    for title_lower, pairs in raw_index.items():
        final[title_lower] = [
            (primary_titles.get(aid, raw_title), aid) for raw_title, aid in pairs
        ]

    _anidb_index = final
    _anidb_index_loaded_at = now
    logger.info("AniDB index loaded: %d title entries.", len(final))
    return final


def search_anidb(title: str) -> list[MediaResult]:
    """
    Search AniDB for (typically explicit) anime titles using the local index.

    Scoring:
        1.0 — exact match (case-insensitive)
        0.8 — query is a prefix of the indexed title
        0.6 — query is a substring of the indexed title
        0.0–0.5 — word-overlap ratio
    """
    title_lower = title.casefold()

    try:
        index = _load_anidb_index()
    except Exception as exc:
        logger.error("AniDB index unavailable: %s", exc)
        return []

    if not index:
        logger.warning("AniDB index is empty — title dump may have failed to download.")
        return []

    matches: list[tuple[float, str, str]] = []  # (score, display_title, aid)
    query_words = set(title_lower.split())

    for indexed_lower, pairs in index.items():
        if indexed_lower == title_lower:
            score = 1.0
        elif indexed_lower.startswith(title_lower):
            score = 0.8
        elif title_lower in indexed_lower:
            score = 0.6
        else:
            title_words = set(indexed_lower.split())
            overlap = len(query_words & title_words) / max(len(query_words), 1)
            if overlap < 0.5:
                continue
            score = overlap * 0.5

        for display_title, aid in pairs:
            matches.append((score, display_title, aid))

    # Sort descending by score, deduplicate by aid, take top 5
    matches.sort(key=lambda x: -x[0])
    seen_aids: set[str] = set()
    results: list[MediaResult] = []

    for score, display_title, aid in matches:
        if aid in seen_aids or len(results) >= 5:
            break
        seen_aids.add(aid)
        results.append(MediaResult(
            title=display_title,
            year=None,
            external_id=aid,
            external_url=f"{_ANIDB_ANIME_WEB}/{aid}",
            media_type="xanime",
            overview=f"AniDB ID: {aid}",
            source="anidb",
        ))

    return results


# ---------------------------------------------------------------------------
# Library check — avoids false positives from episode filenames
# ---------------------------------------------------------------------------

_EP_PATTERNS = [
    re.compile(r"\s*[-–]\s*S\d{2}E\d{2}\b.*", re.IGNORECASE),
    re.compile(r"\s*[-–]\s*\d+x\d+\b.*", re.IGNORECASE),
    re.compile(r"\s*\(\d{4}\).*"),
    re.compile(r"\s*\.\w{2,4}$"),
]

_FUZZY_LIBRARY_THRESHOLD = 0.75


def _clean_library_name(name: str) -> str:
    """Strip episode notation / extensions from a library filename."""
    result = name
    for pat in _EP_PATTERNS:
        result = pat.sub("", result)
    return result.strip().casefold()


def _word_fallback_search(title: str) -> list:
    """
    When a full-title search finds nothing (e.g. due to a typo), search
    the library for each meaningful word individually and return the union.
    Words shorter than 4 chars are skipped; if none qualify, all words are used.
    """
    try:
        from library_index import search_library
    except ImportError:
        return []

    words = [w for w in title.casefold().split() if len(w) >= 4]
    if not words:
        words = title.casefold().split()

    seen_paths: set[str] = set()
    all_results = []
    for word in words:
        try:
            for entry in search_library(word, limit=10):
                if entry.path not in seen_paths:
                    seen_paths.add(entry.path)
                    all_results.append(entry)
        except Exception:
            pass

    return all_results


def check_library_for_title(title: str, media_type: str) -> tuple[bool, list[str]]:
    """
    Check whether a title exists in the configured Plex library (or file index).

    Returns:
        (found: bool, matched_display_titles: list[str])

    Fuzzy matching: a misspelled title like "get hin to the greek" will still
    match "Get Him to the Greek" via rapidfuzz WRatio (threshold 0.75).
    If the full-title search returns no candidates at all, a word-by-word
    fallback search is run so that correctly-spelled words still locate the entry.
    """
    try:
        from library_index import search_library
        results = search_library(title, limit=10)
    except Exception as exc:
        logger.warning("Library search failed for '%s': %s", title, exc)
        return False, []

    # If the full-title query found nothing, try word-by-word so a single typo
    # doesn't silently eliminate all candidates.
    if not results:
        results = _word_fallback_search(title)

    if not results:
        return False, []

    title_lower = title.casefold()

    matched: list[str] = []
    for entry in results:
        cleaned = _clean_library_name(entry.name)
        if not cleaned:
            continue

        # Fuzzy match: tolerates typos and minor spelling errors.
        sim = title_similarity(title_lower, cleaned)
        if sim >= _FUZZY_LIBRARY_THRESHOLD:
            matched.append(entry.name)
            continue

        # Exact substring fallback for very short titles (e.g. "It", "Us").
        if title_lower in cleaned or cleaned in title_lower:
            if len(title_lower) >= 4 or title_lower == cleaned:
                matched.append(entry.name)

    return bool(matched), list(dict.fromkeys(matched))  # deduplicated


# ---------------------------------------------------------------------------
# Title similarity check (for uncertain-match flagging)
# ---------------------------------------------------------------------------

def title_similarity(a: str, b: str) -> float:
    """
    Return a 0–1 similarity score between two titles.
    Uses rapidfuzz when available, falls back to character overlap ratio.
    """
    a_clean = a.casefold().strip()
    b_clean = b.casefold().strip()
    if a_clean == b_clean:
        return 1.0

    try:
        from rapidfuzz import fuzz
        return fuzz.WRatio(a_clean, b_clean) / 100.0
    except ImportError:
        pass

    # Simple SequenceMatcher-style ratio
    longer = max(len(a_clean), len(b_clean))
    if longer == 0:
        return 1.0
    common = sum(1 for ac, bc in zip(a_clean, b_clean) if ac == bc)
    return common / longer


# ---------------------------------------------------------------------------
# Main lookup pipeline
# ---------------------------------------------------------------------------

def lookup_media(request: ParsedRequest, media_type: str) -> LookupResult:
    """
    Full lookup pipeline for one parsed request:
        1. Check Plex library.
        2. Search external DB if not in library (or alongside for info).
        3. Return structured LookupResult.

    Args:
        request:    Parsed user request (title + optional year/qualifier).
        media_type: "movie" | "tv" | "anime" | "xanime"
    """
    in_library, library_matches = check_library_for_title(request.title, media_type)

    external_matches: list[MediaResult] = []

    if not in_library:
        if media_type == "movie":
            external_matches = search_tmdb_movies(request.title, request.year)

        elif media_type == "tv":
            # TVDB is primary for shows; TMDB is the fallback
            external_matches = search_tvdb_shows(request.title, request.year)
            if not external_matches:
                external_matches = search_tmdb_shows(request.title, request.year)

        elif media_type == "anime":
            external_matches = search_jikan_anime(request.title, explicit=False)

        elif media_type == "xanime":
            # AniDB first (better explicit coverage), Jikan rx as fallback
            external_matches = search_anidb(request.title)
            if not external_matches:
                external_matches = search_jikan_anime(request.title, explicit=True)

    # Attach the user's qualifier (e.g. "[Season 2]") to the best match
    best = external_matches[0] if external_matches else None
    if best is not None and request.qualifier:
        best = MediaResult(
            title=best.title,
            year=best.year,
            external_id=best.external_id,
            external_url=best.external_url,
            media_type=best.media_type,
            overview=best.overview,
            source=best.source,
            qualifier=request.qualifier,
        )

    return LookupResult(
        request=request,
        in_library=in_library,
        library_matches=library_matches,
        external_matches=external_matches,
        best_match=best,
    )
