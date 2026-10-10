# =============================================================================
# torrentio.py
# =============================================================================
# Torrentio (Stremio addon) search client.
#
# Unlike the text-search providers in torrent_search.py, Torrentio is keyed by
# IDs (IMDb for movies and episodes, Kitsu for anime), so the caller resolves
# the title to an id first and gets back a clean list of releases for exactly
# that movie or episode. That sidesteps the fuzzy-title junk problem entirely.
#
# With a Real-Debrid key in the config segment, Torrentio tags each stream
# "[RD+]" (already cached on Real-Debrid, instant) or "[RD download]" (not
# cached) and hands back a `url` that embeds the key instead of an info hash.
# The hash is still recoverable from that url, and the url itself is never
# stored on the parsed stream, so a key cannot leak through a repr or a log.
#
# Network access goes through ONE injectable transport so tests never touch
# the network:   transport(url, headers, timeout) -> (status, body_bytes)
# =============================================================================

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://torrentio.strem.fun"
# Verified 2026-10-09: a Sensarr/ UA gets HTTP 200, same as Mozilla/5.0.
USER_AGENT = "Sensarr/1.6"

# Be polite to a free public addon: one request per second, and remember
# answers for half an hour (releases for one episode barely change).
MIN_INTERVAL_S = 1.0
CACHE_TTL_S = 30 * 60

_THROTTLE_LOCK = threading.Lock()
_last_request_at = 0.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, list["TorrentioStream"]]] = {}

Transport = Callable[[str, dict, float], "tuple[int, bytes]"]


class TorrentioError(Exception):
    """Torrentio could not be reached or answered badly.

    `status` is the HTTP status when there was one, else None. Callers use it
    to trip their provider circuit breaker.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class TorrentioStream:
    """One release Torrentio offered for a movie or episode."""

    info_hash: str
    file_idx: int | None
    torrent_name: str
    file_name: str | None
    file_path: str | None
    size_bytes: int
    seeders: int
    provider: str
    audio_tags: tuple[str, ...]
    language_flags: tuple[str, ...]
    quality: str
    cached: bool | None


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_RD_OPTION_RE = re.compile(r"(realdebrid=)[^|/&\s]+", re.IGNORECASE)
_RD_RESOLVE_RE = re.compile(r"(/resolve/realdebrid/)[^/\s]+", re.IGNORECASE)


def redact(text: str) -> str:
    """Mask any Real-Debrid key in a url or message before it can be logged."""
    text = _RD_OPTION_RE.sub(r"\1***", text or "")
    return _RD_RESOLVE_RE.sub(r"\1***", text)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
               "TB": 1024 ** 4}
_SIZE_RE = re.compile(r"([\d.,]+)\s*([KMGT]?)i?B", re.IGNORECASE)


def parse_size(text: str) -> int:
    """'1.23 GB' -> bytes. Torrentio prints binary (1024) multiples."""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    unit = (m.group(2).upper() + "B")
    return int(value * _SIZE_UNITS.get(unit, 1))


_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_URL_HASH_RE = re.compile(r"/([0-9a-fA-F]{40})(?:/|$)")
_SEEDERS_RE = re.compile(r"\U0001F464\s*(\d+)")
_SIZE_LINE_RE = re.compile(r"\U0001F4BE\s*([\d.,]+\s*[KMGT]?i?B)", re.IGNORECASE)
_PROVIDER_RE = re.compile(r"⚙️?\s*(.+)")
_FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")


def _split_languages(lines: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Pull audio tags and emoji flags out of the lines after the stats line."""
    audio: list[str] = []
    flags: list[str] = []
    for line in lines:
        for token in line.split("/"):
            token = token.strip()
            if not token:
                continue
            found = _FLAG_RE.findall(token)
            if found:
                flags.extend(found)
            elif "audio" in token.lower():
                audio.append(token)
    return tuple(audio), tuple(flags)


def _hash_and_idx_from_url(url: str) -> tuple[str | None, int | None]:
    """Recover the info hash (and file index) from a debrid resolve url."""
    m = _URL_HASH_RE.search(url or "")
    if not m:
        return None, None
    rest = url[m.end():].split("?")[0].split("/")
    idx = None
    # .../{hash}/{season-or-null}/{fileIdx}/{filename}
    if len(rest) >= 2 and rest[1].isdigit():
        idx = int(rest[1])
    return m.group(1).lower(), idx


def parse_stream(obj: dict) -> TorrentioStream | None:
    """Turn one Torrentio stream object into a TorrentioStream.

    Returns None when there is no usable info hash (e.g. an external-link
    stream), so callers can just drop it.
    """
    if not isinstance(obj, dict):
        return None
    info_hash = str(obj.get("infoHash") or "").strip()
    url_idx = None
    if not _HASH_RE.match(info_hash):
        info_hash, url_idx = _hash_and_idx_from_url(str(obj.get("url") or ""))
        if not info_hash:
            return None
    info_hash = info_hash.lower()

    file_idx = obj.get("fileIdx")
    file_idx = file_idx if isinstance(file_idx, int) and not isinstance(file_idx, bool) else url_idx

    title = html.unescape(str(obj.get("title") or ""))
    lines = title.split("\n")
    stats_i = next((i for i, ln in enumerate(lines)
                    if "\U0001F464" in ln or "\U0001F4BE" in ln
                    or "⚙" in ln), None)
    if stats_i is None:
        head, stats, tail = lines, "", []
    else:
        head, stats, tail = lines[:stats_i], lines[stats_i], lines[stats_i + 1:]

    torrent_name = head[0].strip() if head else ""
    file_path = head[1].strip() if len(head) > 1 and head[1].strip() else None

    hints = obj.get("behaviorHints") or {}
    hint_name = hints.get("filename") if isinstance(hints, dict) else None
    if file_path:
        file_name = re.split(r"[\\/]", file_path)[-1] or None
    else:
        file_name = str(hint_name).strip() if hint_name else None

    seeders_m = _SEEDERS_RE.search(stats)
    size_m = _SIZE_LINE_RE.search(stats)
    prov_m = _PROVIDER_RE.search(stats)
    audio_tags, flags = _split_languages(tail)

    name_lines = html.unescape(str(obj.get("name") or "")).split("\n")
    first = name_lines[0]
    quality = name_lines[1].strip() if len(name_lines) > 1 else ""
    if "[RD+]" in first:
        cached: bool | None = True
    elif "[RD download]" in first:
        cached = False
    else:
        cached = None

    return TorrentioStream(
        info_hash=info_hash,
        file_idx=file_idx,
        torrent_name=torrent_name,
        file_name=file_name,
        file_path=file_path,
        size_bytes=parse_size(size_m.group(1)) if size_m else 0,
        seeders=int(seeders_m.group(1)) if seeders_m else 0,
        provider=prov_m.group(1).strip() if prov_m else "",
        audio_tags=audio_tags,
        language_flags=flags,
        quality=quality,
        cached=cached,
    )


def build_url(base: str, kind: str, video_id: str, *,
              options: dict | None = None,
              debrid_key: str | None = None) -> str:
    """Build a stream url. `video_id` is the full id incl. season/episode."""
    if kind not in ("movie", "series"):
        raise ValueError(f"kind must be 'movie' or 'series', got {kind!r}")
    parts = [f"{k}={v}" for k, v in (options or {}).items()
             if v not in (None, "")]
    if debrid_key:
        parts.append(f"realdebrid={debrid_key}")
    root = (base or DEFAULT_BASE).rstrip("/")
    if parts:
        root += "/" + "|".join(parts)
    vid = urllib.parse.quote(video_id, safe=":")
    return f"{root}/stream/{kind}/{vid}.json"


def magnet_for(stream: TorrentioStream, trackers: list[str]) -> str:
    """Magnet link for a stream. Always carries btih:{hash}; the app keys
    failures on that."""
    magnet = (f"magnet:?xt=urn:btih:{stream.info_hash}"
              f"&dn={urllib.parse.quote(stream.torrent_name, safe='')}")
    for tr in trackers or []:
        magnet += "&tr=" + urllib.parse.quote(tr, safe="")
    return magnet


# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

def _default_transport(url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _throttle() -> None:
    global _last_request_at
    with _THROTTLE_LOCK:
        wait = _last_request_at + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _video_id(video_id: str, kind: str, season, episode) -> str:
    if kind == "movie":
        return video_id
    if video_id.startswith("kitsu:"):
        if episode is None:
            raise ValueError("episode is required for a kitsu series id")
        return f"{video_id}:{int(episode)}"
    if season is None or episode is None:
        raise ValueError("season and episode are required for series")
    return f"{video_id}:{int(season)}:{int(episode)}"


def search(video_id: str, kind: str, *, season=None, episode=None, base=None,
           options=None, debrid_key=None, timeout=15,
           transport: Transport | None = None) -> list[TorrentioStream]:
    """Streams for a movie or episode. 404 -> []; other failures raise
    TorrentioError so the caller can trip its circuit breaker."""
    url = build_url(base or DEFAULT_BASE, kind,
                    _video_id(video_id, kind, season, episode),
                    options=options, debrid_key=debrid_key)
    safe_url = redact(url)

    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(safe_url)
        if hit and now - hit[0] < CACHE_TTL_S:
            return list(hit[1])

    _throttle()
    send = transport or _default_transport
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        status, body = send(url, headers, timeout)
    except Exception as exc:  # network/timeouts; message may embed the url
        raise TorrentioError(
            f"Torrentio request failed for {safe_url}: {redact(str(exc))}"
        ) from None

    if status == 404:
        result: list[TorrentioStream] = []
    elif status != 200:
        raise TorrentioError(f"Torrentio HTTP {status} for {safe_url}", status)
    else:
        try:
            data = json.loads(body.decode("utf-8"))
            raw = data.get("streams") or []
        except (ValueError, AttributeError) as exc:
            raise TorrentioError(
                f"Torrentio returned bad JSON for {safe_url}", status) from None
        result = [s for s in (parse_stream(o) for o in raw) if s]

    with _CACHE_LOCK:
        _CACHE[safe_url] = (time.monotonic(), list(result))
    logger.debug("Torrentio %s -> %d streams", safe_url, len(result))
    return result
