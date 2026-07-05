# =============================================================================
# torrent_search.py
# =============================================================================
# In-app torrent source search — replaces the "open a search page in Firefox"
# workflow with structured results the Downloads tab can grab directly.
#
# Sources by media type (mirroring torlink's source registry, but implemented
# natively in Python against each site's JSON/RSS API instead of scraping):
#   movie   → YTS (yts.mx JSON API) + The Pirate Bay (apibay.org JSON API)
#   tv      → The Pirate Bay
#   other   → The Pirate Bay
#   anime   → nyaa.si RSS
#   xanime  → sukebei.nyaa.si RSS
#
# Every source degrades gracefully to [] on failure (same pattern as
# media_lookup.py) — a dead mirror should never break the tab.
# =============================================================================

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_USER_AGENT = "PlexResetButton/1.0"

# Standard open trackers appended to magnets built from a bare info-hash.
_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
]


@dataclass(frozen=True)
class TorrentResult:
    title: str
    magnet: str
    size_bytes: int
    seeders: int
    source: str        # "yts" | "tpb" | "nyaa" | "sukebei"
    media_type: str


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read()


def _magnet_from_hash(info_hash: str, name: str) -> str:
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}"
    for tracker in _TRACKERS:
        magnet += f"&tr={urllib.parse.quote(tracker)}"
    return magnet


# ---------------------------------------------------------------------------
# YTS — movies (JSON API)
# ---------------------------------------------------------------------------

def search_yts(query: str, *, limit: int = 20) -> list[TorrentResult]:
    url = (
        "https://yts.mx/api/v2/list_movies.json?"
        + urllib.parse.urlencode({"query_term": query, "limit": limit, "sort_by": "seeds"})
    )
    try:
        payload = json.loads(_http_get(url))
    except Exception as exc:
        logger.warning("YTS search failed for %r: %s", query, exc)
        return []

    results: list[TorrentResult] = []
    for movie in (payload.get("data", {}).get("movies") or []):
        title = movie.get("title_long") or movie.get("title") or "?"
        for t in movie.get("torrents") or []:
            info_hash = t.get("hash")
            if not info_hash:
                continue
            quality = t.get("quality") or ""
            type_tag = t.get("type") or ""
            display = f"{title} [{quality} {type_tag}".strip() + " YTS]"
            results.append(TorrentResult(
                title=display,
                magnet=_magnet_from_hash(info_hash, display),
                size_bytes=int(t.get("size_bytes") or 0),
                seeders=int(t.get("seeds") or 0),
                source="yts",
                media_type="movie",
            ))
    return results


# ---------------------------------------------------------------------------
# The Pirate Bay — via the apibay.org JSON API (no HTML scraping)
# ---------------------------------------------------------------------------

def search_tpb(query: str, media_type: str, *, limit: int = 30) -> list[TorrentResult]:
    # cat=200 restricts to Video. (Categories: 201 movies, 205 TV, 207 HD
    # movies, 208 HD TV — 200 covers the whole video tree.)
    url = "https://apibay.org/q.php?" + urllib.parse.urlencode({"q": query, "cat": "200"})
    try:
        payload = json.loads(_http_get(url))
    except Exception as exc:
        logger.warning("TPB (apibay) search failed for %r: %s", query, exc)
        return []

    results: list[TorrentResult] = []
    for item in payload if isinstance(payload, list) else []:
        info_hash = item.get("info_hash") or ""
        name = item.get("name") or ""
        # apibay returns a single placeholder row when there are no results
        if not info_hash or info_hash == "0000000000000000000000000000000000000000":
            continue
        results.append(TorrentResult(
            title=name,
            magnet=_magnet_from_hash(info_hash, name),
            size_bytes=int(item.get("size") or 0),
            seeders=int(item.get("seeders") or 0),
            source="tpb",
            media_type=media_type,
        ))
    results.sort(key=lambda r: r.seeders, reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------------
# nyaa.si / sukebei.nyaa.si — RSS feeds (carry infoHash, seeders, size)
# ---------------------------------------------------------------------------

_NYAA_NS = {"nyaa": "https://nyaa.si/xmlns/nyaa"}
_SIZE_RE = re.compile(r"([\d.]+)\s*(TiB|GiB|MiB|KiB|B)", re.IGNORECASE)
_SIZE_MULT = {"b": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def _parse_nyaa_size(text: str) -> int:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2).lower()])


def _search_nyaa_rss(base: str, query: str, category: str, source: str,
                     media_type: str, *, limit: int = 30) -> list[TorrentResult]:
    url = f"{base}/?" + urllib.parse.urlencode(
        {"page": "rss", "q": query, "c": category, "f": "0", "s": "seeders", "o": "desc"}
    )
    try:
        root = ET.fromstring(_http_get(url))
    except Exception as exc:
        logger.warning("%s search failed for %r: %s", source, query, exc)
        return []

    results: list[TorrentResult] = []
    for item in root.iter("item"):
        title = item.findtext("title") or "?"
        info_hash = item.findtext("nyaa:infoHash", namespaces=_NYAA_NS) or ""
        seeders = int(item.findtext("nyaa:seeders", default="0", namespaces=_NYAA_NS) or 0)
        size = _parse_nyaa_size(item.findtext("nyaa:size", default="", namespaces=_NYAA_NS) or "")
        if not info_hash:
            continue
        results.append(TorrentResult(
            title=title,
            magnet=_magnet_from_hash(info_hash, title),
            size_bytes=size,
            seeders=seeders,
            source=source,
            media_type=media_type,
        ))
        if len(results) >= limit:
            break
    return results


def search_nyaa(query: str, *, limit: int = 30) -> list[TorrentResult]:
    # c=1_2: Anime — English-translated (same filter the old browser links used)
    return _search_nyaa_rss("https://nyaa.si", query, "1_2", "nyaa", "anime", limit=limit)


def search_sukebei(query: str, *, limit: int = 30) -> list[TorrentResult]:
    return _search_nyaa_rss("https://sukebei.nyaa.si", query, "0_0", "sukebei", "xanime", limit=limit)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def search_torrents(query: str, media_type: str, *, limit: int = 30) -> list[TorrentResult]:
    """Search the right source(s) for the media type, best-seeded first."""
    query = query.strip()
    if not query:
        return []

    results: list[TorrentResult] = []
    if media_type == "movie":
        results.extend(search_yts(query, limit=limit))
        results.extend(search_tpb(query, media_type, limit=limit))
    elif media_type == "anime":
        results.extend(search_nyaa(query, limit=limit))
    elif media_type == "xanime":
        results.extend(search_sukebei(query, limit=limit))
    else:  # tv / other / unknown
        results.extend(search_tpb(query, media_type, limit=limit))

    results.sort(key=lambda r: r.seeders, reverse=True)
    return results[:limit]


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size_bytes} B"
