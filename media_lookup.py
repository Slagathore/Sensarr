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
    alt_titles: tuple[str, ...] = ()  # romaji/english/original — for matching


def best_title_similarity(query: str, result: "MediaResult") -> float:
    """Similarity of query against the result's primary AND alternate titles.

    Anime folders are often romaji ("Shingeki no Bahamut") while the primary
    result title may be English ("Rage of Bahamut"); scoring against both and
    taking the max stops those from being scored as mismatches.
    """
    candidates = [result.title, *result.alt_titles]
    return max((title_similarity(query, c) for c in candidates if c), default=0.0)


@dataclass
class LookupResult:
    """Full lookup result for one ParsedRequest."""
    request: ParsedRequest
    in_library: bool
    library_matches: list[str]      # matched titles already in Plex
    external_matches: list[MediaResult]
    best_match: MediaResult | None
    search_attempted: bool = False  # False when no API key is configured for this type


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


def search_tmdb_movies(title: str, year: int | None = None, *, limit: int = 5) -> list[MediaResult]:
    """Search TMDB for movies. Returns up to `limit` ranked results."""
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
    for item in (data.get("results") or [])[:limit]:
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


def search_tmdb_shows(title: str, year: int | None = None, *, limit: int = 5) -> list[MediaResult]:
    """Search TMDB for TV shows. Returns up to `limit` ranked results."""
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
    for item in (data.get("results") or [])[:limit]:
        tmdb_id = item.get("id")
        release_year: int | None = None
        fa = item.get("first_air_date") or ""
        if fa and len(fa) >= 4:
            try:
                release_year = int(fa[:4])
            except ValueError:
                pass

        name = item.get("name") or item.get("original_name") or "Unknown"
        original = item.get("original_name") or ""
        results.append(MediaResult(
            title=name,
            year=release_year,
            external_id=str(tmdb_id) if tmdb_id else "",
            external_url=f"{_TMDB_TV_WEB}/{tmdb_id}" if tmdb_id else "",
            media_type="tv",
            overview=(item.get("overview") or "")[:200],
            source="tmdb",
            alt_titles=(original,) if original and original != name else (),
        ))

    return results


# ---------------------------------------------------------------------------
# OMDB — alternate movie source. Used as an opt-in "try a different DB"
# fallback when TMDB returned nothing useful.
# ---------------------------------------------------------------------------

_OMDB_BASE = "https://www.omdbapi.com/"
_IMDB_TITLE_WEB = "https://www.imdb.com/title"


def _omdb_enabled() -> bool:
    return bool(config.OMDB_API_KEY)


def search_omdb_movies(
    title: str, year: int | None = None, *, limit: int = 10,
) -> list[MediaResult]:
    """
    Search OMDB (an IMDB-backed free API) for movies. Used as a fallback
    when TMDB doesn't have what the user is looking for. Free tier is
    capped at 1000 requests/day; treat any HTTP error as 'no results'
    rather than failing the whole correction flow.

    Returns up to `limit` ranked results. Empty list if no key configured,
    no results, or the API errors out.
    """
    if not _omdb_enabled():
        logger.warning("OMDB_API_KEY not set — OMDB fallback unavailable.")
        return []

    params: dict[str, str] = {
        "s": title,
        "type": "movie",
        "apikey": config.OMDB_API_KEY,
    }
    if year:
        params["y"] = str(year)

    url = f"{_OMDB_BASE}?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url)
    except RuntimeError as exc:
        logger.error("OMDB movie search failed: %s", exc)
        return []

    if data.get("Response") != "True":
        # OMDB returns {"Response":"False","Error":"Movie not found!"} for misses
        return []

    results: list[MediaResult] = []
    for item in (data.get("Search") or [])[:limit]:
        imdb_id = (item.get("imdbID") or "").strip()
        raw_year = (item.get("Year") or "").strip()
        # OMDB sometimes returns "2014–" or "2014–2018" for year ranges
        year_int: int | None = None
        m = re.match(r"^(\d{4})", raw_year)
        if m:
            try:
                year_int = int(m.group(1))
            except ValueError:
                pass

        results.append(MediaResult(
            title=item.get("Title") or "Unknown",
            year=year_int,
            external_id=imdb_id,
            external_url=f"{_IMDB_TITLE_WEB}/{imdb_id}/" if imdb_id else "",
            media_type="movie",
            overview="",  # OMDB's search endpoint doesn't include plots
            source="omdb",
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


def search_tvdb_shows(title: str, year: int | None = None, *, limit: int = 5) -> list[MediaResult]:
    """Search TVDB for TV series. Returns up to `limit` results."""
    token = _tvdb_get_token()
    if not token:
        logger.warning("TVDB not available (missing key or auth failure).")
        return []

    params: dict[str, str] = {"query": title, "type": "series", "limit": str(limit)}
    if year:
        params["year"] = str(year)

    url = f"{_TVDB_BASE}/search?{urllib.parse.urlencode(params)}"
    try:
        data = _get_json(url, headers={"Authorization": f"Bearer {token}"})
    except RuntimeError as exc:
        logger.error("TVDB search failed: %s", exc)
        return []

    results: list[MediaResult] = []
    for item in (data.get("data") or [])[:limit]:
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
# Jikan v4 limits: ~3 req/s AND 60 req/min. The old 0.4s gap respected the
# per-second cap but blew the per-minute one on multi-page fetches, causing
# HTTP 429s. 1.1s between calls keeps us under both (~54/min worst case).
_JIKAN_RATE_LIMIT_S = 1.1
_JIKAN_MAX_RETRIES = 4


def _jikan_throttle() -> None:
    global _jikan_last_request
    now = time.time()
    wait = _JIKAN_RATE_LIMIT_S - (now - _jikan_last_request)
    if wait > 0:
        time.sleep(wait)
    _jikan_last_request = time.time()


def _jikan_get(url: str) -> dict | None:
    """GET a Jikan URL with throttling and exponential backoff on HTTP 429.

    Returns the parsed dict, or None if it kept getting rate-limited / failed.
    """
    for attempt in range(_JIKAN_MAX_RETRIES):
        _jikan_throttle()
        try:
            return _get_json(url)
        except RuntimeError as exc:
            if "429" in str(exc) and attempt < _JIKAN_MAX_RETRIES - 1:
                backoff = 2.0 * (attempt + 1)
                logger.info("Jikan 429 — backing off %.1fs (attempt %d)", backoff, attempt + 1)
                time.sleep(backoff)
                continue
            logger.error("Jikan request failed: %s", exc)
            return None
    return None


def search_jikan_anime(title: str, *, explicit: bool = False, limit: int = 5) -> list[MediaResult]:
    """
    Search MAL via the Jikan v4 API.

    explicit=True filters to rating=rx (hentai on MAL).
    explicit=False adds sfw=true to exclude adult content from results.
    """
    # NOTE: no order_by=score here. Ordering by MAL rating buried the actual
    # title match under generically-popular shows (a search for "Shingeki no
    # Bahamut" surfaced "Attack on Titan" first). Jikan's default ordering is
    # relevance to the query, which is what identification needs.
    params: dict[str, str] = {"q": title, "limit": str(limit)}
    if explicit:
        params["rating"] = "rx"
    else:
        params["sfw"] = "true"

    url = f"{_JIKAN_BASE}/anime?{urllib.parse.urlencode(params)}"
    data = _jikan_get(url)
    if data is None:
        return []

    results: list[MediaResult] = []
    for item in (data.get("data") or [])[:limit]:
        mal_id = item.get("mal_id")
        # Keep BOTH titles so callers can match romaji or english folder names.
        title_en = item.get("title_english") or ""
        title_romaji = item.get("title") or ""
        title_str = title_en or title_romaji or "Unknown"
        alt = tuple(t for t in {title_en, title_romaji,
                                *(item.get("title_synonyms") or [])} if t and t != title_str)

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
            alt_titles=alt,
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

# --- Sequel-number guard -----------------------------------------------------
# "Dune Part Two" must never match a library entry of just "Dune", and
# "<movie> 2" must not match "<movie>". Both the substring fallback and
# rapidfuzz's WRatio (which rewards partial matches) were producing these
# false "already in your library" positives. The guard extracts the sequel
# numbers each title carries and rejects a match when they differ.

_ROMAN_NUMERALS = {
    "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_PART_MARKERS = {"part", "pt", "chapter", "vol", "volume", "book", "season"}


# Tokens that mark the start of release-group junk in a filename. Sequel
# numbers sit NEXT TO the title ("John Wick 3"); digits after these markers
# are codec/audio noise ("DDP5.1", "x265", "1080p") and must not pollute the
# signature — otherwise junk digits block legitimate matches.
_JUNK_TOKEN_RE = re.compile(
    r"^(?:\d{3,4}p|x26[45]|h26[45]|hevc|avc|blu-?ray|b[dr]rip|web-?(?:dl|rip)?"
    r"|hdtv|dvdrip|remux|proper|repack|extended|uncut|imax|hdr(?:10)?\+?"
    r"|dolby|vision|dv|atmos|ddp?\d?|dts(?:hd)?|aac\d?|ac3|truehd|opus"
    r"|\d+bit|multi|dual|sub(?:bed|s)?|dub(?:bed)?|remaster(?:ed)?)$",
    re.IGNORECASE,
)


def _signature_portion(title: str) -> list[str]:
    """Tokens of the title up to the first year or release-junk marker.

    "john wick 3 2019 1080p ddp5 1 x265 group" → ["john", "wick", "3"]
    """
    tokens = re.findall(r"[a-z0-9]+", title.casefold())
    portion: list[str] = []
    for tok in tokens:
        if tok.isdigit() and len(tok) == 4 and 1880 <= int(tok) <= 2159:
            break  # year — title (and any sequel number) ends here
        if _JUNK_TOKEN_RE.match(tok):
            break
        portion.append(tok)
    return portion or tokens  # a title that IS a year/junk word survives


def _sequel_signature(title: str) -> frozenset[int]:
    """Return the set of sequel/part numbers a title carries.

    Captures: standalone digit tokens ("Movie 2", "Movie 10"), roman numerals
    ("Rocky III"), and number words directly after a part marker
    ("Dune Part Two"). Only the portion of the name before the first
    year/release-junk marker is considered, so "DDP5.1"/"x265"-style noise
    from release groups can't inject phantom numbers. Four-digit numbers in
    the plausible-year range are ignored so "Blade Runner 2049" isn't
    treated as sequel #2049.
    """
    numbers: set[int] = set()
    prev = ""
    for tok in _signature_portion(title):
        if tok.isdigit():
            value = int(tok)
            if not (1880 <= value <= 2159 and len(tok) == 4):  # skip years
                numbers.add(value)
        elif tok in _ROMAN_NUMERALS:
            numbers.add(_ROMAN_NUMERALS[tok])
        elif tok in _NUMBER_WORDS and prev in _PART_MARKERS:
            numbers.add(_NUMBER_WORDS[tok])
        prev = tok
    return frozenset(numbers)


def _sequel_mismatch(a: str, b: str) -> bool:
    """True when the two titles clearly refer to different entries in a series."""
    return _sequel_signature(a) != _sequel_signature(b)


def clean_library_name(name: str) -> str:
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


def check_library_for_title(
    title: str,
    media_type: str,
    *,
    strict: bool = False,
) -> tuple[bool, list[str]]:
    """
    Check whether a title exists in the configured Plex library (or file index).

    Returns:
        (found: bool, matched_display_titles: list[str])

    Modes:
        strict=False (default) — typo-tolerant. Threshold 0.75 plus a substring
            fallback and a word-by-word fallback so misspellings still match.
        strict=True — used when `title` is a canonical name picked from an
            external DB (e.g. after the user corrects a mismatch). Requires
            ≥0.92 similarity OR exact case-insensitive match. No substring or
            word-fallback hacks, since those produce false positives like
            "Reacher 2" matching unrelated entries returned by Plex search.
    """
    try:
        from library_index import search_library
        results = search_library(title, limit=10)
    except Exception as exc:
        logger.warning("Library search failed for '%s': %s", title, exc)
        return False, []

    # Word-by-word fallback only in fuzzy mode — strict mode doesn't want it.
    if not results and not strict:
        results = _word_fallback_search(title)

    if not results:
        return False, []

    title_lower = title.casefold()
    threshold = 0.92 if strict else _FUZZY_LIBRARY_THRESHOLD

    matched: list[str] = []
    for entry in results:
        cleaned = clean_library_name(entry.name)
        if not cleaned:
            continue

        if cleaned == title_lower:
            matched.append(entry.name)
            continue

        # Different sequel numbers → different film/season, no matter how
        # similar the base titles are ("Dune Part Two" vs "Dune").
        if _sequel_mismatch(title_lower, cleaned):
            continue

        sim = title_similarity(title_lower, cleaned)
        if sim >= threshold:
            matched.append(entry.name)
            continue

        if strict:
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
    search_attempted = False

    if not in_library:
        if media_type == "movie":
            if _tmdb_enabled():
                search_attempted = True
            external_matches = search_tmdb_movies(request.title, request.year)

        elif media_type == "tv":
            # TVDB is primary for shows; TMDB is the fallback
            if config.TVDB_API_KEY or _tmdb_enabled():
                search_attempted = True
            external_matches = search_tvdb_shows(request.title, request.year)
            if not external_matches:
                external_matches = search_tmdb_shows(request.title, request.year)

        elif media_type == "anime":
            search_attempted = True  # Jikan requires no API key
            external_matches = search_jikan_anime(request.title, explicit=False)

        elif media_type == "xanime":
            search_attempted = True  # AniDB + Jikan require no API key
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
        search_attempted=search_attempted,
    )


# ---------------------------------------------------------------------------
# Episode lists — used by the Shows tracker (show_tracker.py)
# ---------------------------------------------------------------------------
# One fetcher per identification source. All return EpisodeInfo lists sorted
# by (season, episode) and degrade to [] on failure, matching the rest of
# this module. AniDB has no cheap episode API, so xanime shows identified
# via AniDB sync episodes through Jikan when a MAL id is available.


@dataclass(frozen=True)
class EpisodeInfo:
    season: int
    episode: int
    title: str
    air_date: str | None   # ISO YYYY-MM-DD, None when unaired/unknown


def get_tvdb_episodes(series_id: str) -> list[EpisodeInfo]:
    """All aired-order episodes for a TVDB series (specials = season 0)."""
    token = _tvdb_get_token()
    if not token:
        return []

    episodes: list[EpisodeInfo] = []
    page = 0
    while page < 20:  # hard cap — longest real shows are < 20 pages of 500
        url = f"{_TVDB_BASE}/series/{series_id}/episodes/default?page={page}"
        try:
            data = _get_json(url, headers={"Authorization": f"Bearer {token}"})
        except RuntimeError as exc:
            logger.error("TVDB episodes fetch failed for %s: %s", series_id, exc)
            break
        payload = data.get("data") or {}
        for ep in payload.get("episodes") or []:
            season = ep.get("seasonNumber")
            number = ep.get("number")
            if season is None or number is None:
                continue
            episodes.append(EpisodeInfo(
                season=int(season), episode=int(number),
                title=ep.get("name") or "",
                air_date=(ep.get("aired") or None),
            ))
        links = data.get("links") or {}
        if not links.get("next"):
            break
        page += 1
    episodes.sort(key=lambda e: (e.season, e.episode))
    return episodes


def get_tvdb_series_status(series_id: str) -> str:
    """'Continuing' / 'Ended' / '' (unknown)."""
    token = _tvdb_get_token()
    if not token:
        return ""
    try:
        data = _get_json(
            f"{_TVDB_BASE}/series/{series_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except RuntimeError:
        return ""
    return ((data.get("data") or {}).get("status") or {}).get("name") or ""


def get_tmdb_tv_episodes(tv_id: str) -> list[EpisodeInfo]:
    """All episodes for a TMDB TV show, season by season."""
    if not _tmdb_enabled():
        return []
    key = config.TMDB_API_KEY
    try:
        show = _get_json(f"{_TMDB_BASE}/tv/{tv_id}?api_key={key}")
    except RuntimeError as exc:
        logger.error("TMDB show fetch failed for %s: %s", tv_id, exc)
        return []

    episodes: list[EpisodeInfo] = []
    for season_stub in show.get("seasons") or []:
        season_num = season_stub.get("season_number")
        if season_num is None:
            continue
        try:
            season = _get_json(f"{_TMDB_BASE}/tv/{tv_id}/season/{season_num}?api_key={key}")
        except RuntimeError:
            continue
        for ep in season.get("episodes") or []:
            episodes.append(EpisodeInfo(
                season=int(ep.get("season_number") or season_num),
                episode=int(ep.get("episode_number") or 0),
                title=ep.get("name") or "",
                air_date=ep.get("air_date") or None,
            ))
    episodes.sort(key=lambda e: (e.season, e.episode))
    return episodes


def get_tmdb_tv_status(tv_id: str) -> str:
    if not _tmdb_enabled():
        return ""
    try:
        data = _get_json(f"{_TMDB_BASE}/tv/{tv_id}?api_key={config.TMDB_API_KEY}")
    except RuntimeError:
        return ""
    return data.get("status") or ""   # "Returning Series" / "Ended" / ...


def get_jikan_episodes(mal_id: str, *, max_pages: int = 12) -> list[EpisodeInfo]:
    """Episodes for one MAL entry. MAL entries are per-season, so season=1.

    Jikan lists only AIRED episodes (never future ones) — do not use this for
    'next episode to air'; use get_tmdb_next_air for that. Pagination is capped
    (100 eps/page) so a 1000-episode show doesn't hammer the rate limit; the
    tracker only needs aired counts, not every episode of One Piece.
    """
    episodes: list[EpisodeInfo] = []
    page = 1
    while page <= max_pages:
        data = _jikan_get(f"{_JIKAN_BASE}/anime/{mal_id}/episodes?page={page}")
        if data is None:
            break
        for ep in data.get("data") or []:
            number = ep.get("mal_id")
            if number is None:
                continue
            aired = ep.get("aired") or ""
            episodes.append(EpisodeInfo(
                season=1, episode=int(number),
                title=ep.get("title") or "",
                air_date=aired[:10] if aired else None,
            ))
        if not ((data.get("pagination") or {}).get("has_next_page")):
            break
        page += 1
    episodes.sort(key=lambda e: (e.season, e.episode))
    return episodes


def get_jikan_status(mal_id: str) -> str:
    data = _jikan_get(f"{_JIKAN_BASE}/anime/{mal_id}")
    if data is None:
        return ""
    return ((data.get("data") or {}).get("status")) or ""  # "Currently Airing" / "Finished Airing"


# ---------------------------------------------------------------------------
# Next-episode-to-air — the reliable airing signal (see the airing bug notes)
# ---------------------------------------------------------------------------
# TMDB exposes next_episode_to_air directly; that's the only source that
# consistently answers "when does the next episode air?" for both TV and the
# anime TMDB carries. TVDB and Jikan episode lists are aired-only, so the
# tracker resolves a TMDB id (even for TVDB/Jikan-identified shows) to fill in
# the airing schedule.


def resolve_tmdb_tv_id(title: str, year: int | None = None,
                       *, prefer_anime: bool = False) -> str | None:
    """Best-effort TMDB TV id for a title (for airing data on non-TMDB shows).

    Scores raw TMDB search hits by title similarity, with bonuses for a year
    match and — when prefer_anime is set — for Japanese-language animation.
    Without the anime bias a search for "One Piece" would happily return the
    live-action series instead of the 1999 anime.
    """
    if not _tmdb_enabled():
        return None
    params = {"api_key": config.TMDB_API_KEY, "query": title,
              "include_adult": "true", "page": "1"}
    if year:
        params["first_air_date_year"] = str(year)
    try:
        data = _get_json(f"{_TMDB_BASE}/search/tv?{urllib.parse.urlencode(params)}")
    except RuntimeError:
        return None

    best_id: str | None = None
    best_score = 0.0
    for item in (data.get("results") or [])[:8]:
        names = [item.get("name") or "", item.get("original_name") or ""]
        score = max((title_similarity(title, n) for n in names if n), default=0.0)
        fa = (item.get("first_air_date") or "")[:4]
        if year and fa == str(year):
            score += 0.15
        if prefer_anime:
            if item.get("original_language") == "ja":
                score += 0.15
            if 16 in (item.get("genre_ids") or []):  # 16 = Animation
                score += 0.1
        if score > best_score and item.get("id"):
            best_score, best_id = score, str(item["id"])
    return best_id if best_score >= 0.6 else None


def get_tmdb_next_air(tv_id: str) -> EpisodeInfo | None:
    """The next episode scheduled to air for a TMDB TV id, or None."""
    if not _tmdb_enabled():
        return None
    try:
        data = _get_json(f"{_TMDB_BASE}/tv/{tv_id}?api_key={config.TMDB_API_KEY}")
    except RuntimeError:
        return None
    nxt = data.get("next_episode_to_air")
    if not nxt or not nxt.get("air_date"):
        return None
    return EpisodeInfo(
        season=int(nxt.get("season_number") or 0),
        episode=int(nxt.get("episode_number") or 0),
        title=nxt.get("name") or "",
        air_date=nxt.get("air_date"),
    )
