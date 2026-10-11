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
#
# Query handling (see the "Query shaping" section): indexer search engines are
# dumber than they look. apibay tokenizes on punctuation and, when a query
# matches nothing, answers with its TRENDING list instead of an empty array —
# so "C.H.U.D." came back as 100 unrelated blockbusters, and every caller that
# treated "the pool is non-empty" as "the query worked" happily gated the whole
# junk list and gave up. Everything a query touches is therefore shaped here:
# punctuation-stripped and acronym-collapsed spellings, a distinctive-word
# fallback, an optional translation for native-script titles, and a relevance
# filter that throws the trending list away instead of believing it.
# =============================================================================

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace

import config
import torrentio

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_USER_AGENT = "Sensarr/1.0"

_PROVIDER_LOCK = threading.Lock()
_PROVIDER_FAILURE_UNTIL: dict[str, float] = {}
_PROVIDER_LAST_ERROR: dict[str, str] = {}


def _provider_available(provider: str) -> bool:
    """Whether a provider's failure circuit currently permits a probe."""
    now = time.monotonic()
    with _PROVIDER_LOCK:
        until = _PROVIDER_FAILURE_UNTIL.get(provider, 0.0)
        if until <= now:
            _PROVIDER_FAILURE_UNTIL.pop(provider, None)
            return True
        return False


def _record_provider_failure(provider: str, exc: Exception) -> None:
    until = time.monotonic() + max(
        1, int(config.TORRENT_PROVIDER_BACKOFF_SECONDS))
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURE_UNTIL[provider] = until
        _PROVIDER_LAST_ERROR[provider] = str(exc)
    logger.warning(
        "%s search provider failed; pausing it for %ss: %s",
        provider.upper(), config.TORRENT_PROVIDER_BACKOFF_SECONDS, exc)


def _record_provider_success(provider: str) -> None:
    with _PROVIDER_LOCK:
        _PROVIDER_FAILURE_UNTIL.pop(provider, None)
        _PROVIDER_LAST_ERROR.pop(provider, None)


def provider_circuit_status(providers: list[str] | tuple[str, ...]) -> dict[str, dict]:
    """Read-only circuit detail included in selection provenance."""
    now = time.monotonic()
    with _PROVIDER_LOCK:
        return {
            provider: {
                "available": _PROVIDER_FAILURE_UNTIL.get(provider, 0.0) <= now,
                "retry_in_seconds": max(
                    0, round(_PROVIDER_FAILURE_UNTIL.get(provider, 0.0) - now)),
                "last_error": _PROVIDER_LAST_ERROR.get(provider),
            }
            for provider in providers
        }

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
    source: str        # "yts" | "tpb" | "nyaa" | "sukebei" | "torrentio"
    media_type: str
    # Torrentio only: the one file this result is about inside a multi-file
    # torrent (a season pack offered for a single episode), so the engines
    # fetch that file instead of the whole pack. size_bytes is THAT file's.
    file_idx: int | None = None
    file_name: str | None = None
    # True when Real-Debrid already holds the payload (instant download).
    cached: bool | None = None
    # Provider audio/language tags ("Dual Audio", flag emoji) the name may lack.
    audio_tags: tuple = ()


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read()


def _magnet_from_hash(info_hash: str, name: str) -> str:
    magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}"
    for tracker in _TRACKERS:
        magnet += f"&tr={urllib.parse.quote(tracker)}"
    return magnet


_BTIH_RE = re.compile(r"btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)


def _infohash(magnet: str) -> str:
    """Lowercased btih info-hash from a magnet URI, or '' if absent. Used to
    dedupe pools across sources."""
    m = _BTIH_RE.search(magnet or "")
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------------------
# Query shaping — spellings, relevance, and the fallback ladder
# ---------------------------------------------------------------------------
# Nothing in here talks to the network. It decides WHAT to ask for and WHICH
# answers are plausibly about the thing we asked for.

# Words too common to prove a result is on-topic.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "of", "in", "on", "at", "to", "for", "is", "it",
    "with", "from", "part", "season", "series", "complete", "episode", "vol",
    "volume", "movie", "film", "show", "tv", "hd", "us", "uk",
})

# Tokens that qualify a search rather than name it: S01, S01E02, E05, EP12,
# a 4-digit year, "1080p"-style resolutions. Preserved verbatim by every
# rewrite, because "Scooby S01" must not degrade into plain "Scooby".
_MARKER_RE = re.compile(
    r"^(?:s\d{1,3}(?:e\d{1,4})?|e\d{1,4}|ep\d{1,4}|\d{4}|\d{3,4}p)$",
    re.IGNORECASE)

# How many spellings one search is allowed to try (see query_variants).
_MAX_VARIANTS = 3

# A dotted acronym: C.H.U.D. / S.W.A.T. / R.I.P.D. Indexer search engines split
# on the dots and match nothing, so these must be collapsed to CHUD / SWAT.
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]\.){2,}", re.IGNORECASE)

# Grammar artifacts nobody puts in a release name. Apostrophes vanish
# ("What's" -> "Whats"); everything else becomes a space.
_APOSTROPHES = "'\u2019\u02bc\u00b4`"
# Unicode-aware on purpose: a CJK title must survive punctuation stripping
# intact, or "逐玉 S01" degrades into a bare "S01" that matches the world.
_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def collapse_acronyms(text: str) -> str:
    """'C.H.U.D. (1984)' -> 'CHUD (1984)'."""
    def _join(match: "re.Match[str]") -> str:
        return match.group(0).replace(".", "")
    return _ACRONYM_RE.sub(_join, text or "")


def strip_grammar(text: str) -> str:
    """Punctuation-free spelling: apostrophes deleted, other punctuation to
    spaces. "What's New Scooby-Doo?" -> "Whats New Scooby Doo"."""
    text = collapse_acronyms(text or "")
    for mark in _APOSTROPHES:
        text = text.replace(mark, "")
    return " ".join(_PUNCT_RE.sub(" ", text).split())


def compact(text: str) -> str:
    """Lowercase, word characters only — the shape used to compare a result
    title against a query without punctuation getting a vote."""
    return re.sub(r"[^\w]+", "", (text or "").casefold(), flags=re.UNICODE)


def _split_markers(query: str) -> tuple[str, str]:
    """Split 'Whats New Scooby Doo S01' into ('Whats New Scooby Doo', 'S01')."""
    words = strip_grammar(query).split()
    head: list[str] = []
    tail: list[str] = []
    for word in words:
        if _MARKER_RE.match(word):
            tail.append(word)
        elif tail:
            tail.append(word)   # anything after a marker stays with it
        else:
            head.append(word)
    return " ".join(head), " ".join(tail)


def distinctive_word(query: str) -> str:
    """The single most identifying word in a title, or '' when there isn't one.

    Release groups rename things stupidly ("What's New, Scooby-Doo?" is filed
    a dozen different ways) but they almost always keep the one weird word.
    Searching that word alone and letting the caller's own matching sort it
    out finds releases no faithful spelling ever will.
    """
    head, _tail = _split_markers(query)
    words = [w for w in head.split() if w.casefold() not in _STOPWORDS]
    if not words:
        return ""
    best = max(words, key=lambda w: (len(w), w.casefold()))
    return best if len(best) >= 4 else ""


def _has_latin_letters(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def translate_query(query: str) -> str:
    """English spelling of a native-script title, or '' when unavailable.

    A Chinese/Japanese/Korean title searched verbatim finds nothing on trackers
    that index English release names, while the translated title ("逐玉" ->
    "Pursuit of Jade") has whole seasons sitting there. Best effort: the LLM is
    optional and a failure just means one fewer variant to try.
    """
    # Judge the TITLE, not the markers: "逐玉 S01" is still a native-script
    # title even though "S01" is Latin.
    head, _tail = _split_markers(query)
    if not head.strip() or _has_latin_letters(head):
        return ""
    query = head
    try:
        import llm_service
        english = llm_service.translate_title_to_english(query)
    except Exception:
        logger.debug("Title translation unavailable for %r.", query, exc_info=True)
        return ""
    english = (english or "").strip()
    return english if english and _has_latin_letters(english) else ""


def query_variants(query: str) -> tuple[str, ...]:
    """Spellings to try, in order, until one answers with relevant results.

    1. exactly what the caller asked for
    2. the same thing with the grammar removed (apostrophes, dots, dashes)
    3. the distinctive word alone, keeping any S01/year marker
    4. an English translation, for native-script titles only
    """
    query = " ".join((query or "").split())
    if not query:
        return tuple()

    variants = [query]

    def _add(candidate: str) -> None:
        candidate = " ".join((candidate or "").split())
        if candidate and not any(
                candidate.casefold() == existing.casefold() for existing in variants):
            variants.append(candidate)

    _add(strip_grammar(query))

    head, tail = _split_markers(query)
    word = distinctive_word(query)
    if word and word.casefold() != head.casefold():
        _add(f"{word} {tail}".strip())

    english = translate_query(query)
    if english:
        _add(english)
        _add(f"{english} {tail}".strip())

    # Bounded on purpose: every extra spelling is another provider request, and
    # the auto-grab pass already walks its own alias variants on top of these.
    # Three attempts is enough to cover "as typed", "without the grammar" and
    # "the one weird word" without turning one search into a burst.
    return tuple(variants[:_MAX_VARIANTS])


def is_relevant(title: str, query: str) -> bool:
    """Could this result plausibly be what the query asked for?

    True when the result title contains any distinctive query token (compared
    punctuation-free, so "C.H.U.D." matches "CHUD" and vice versa). Markers
    alone never qualify — 'S01' matching is not evidence of anything.

    This exists because apibay answers a query it cannot match with its
    TRENDING list rather than an empty array. Without this filter a search for
    an obscure 1984 horror film returns the week's blockbusters, callers see a
    full pool and stop looking, and every one of those results then burns a
    rejection in the selection log.
    """
    head, _tail = _split_markers(query)
    tokens = [w for w in head.split() if w.casefold() not in _STOPWORDS]
    if not tokens:
        tokens = head.split()
    if not tokens:
        return True     # nothing to judge against; don't invent a verdict

    haystack = compact(title)
    if not haystack:
        return False
    # The whole punctuation-free title is the strongest signal; single tokens
    # are the fallback so renamed/reordered releases still qualify.
    if compact(head) and compact(head) in haystack:
        return True
    return any(compact(token) in haystack for token in tokens if len(token) >= 3)


def filter_relevant(results: list["TorrentResult"], query: str) -> list["TorrentResult"]:
    """Drop results that cannot be about `query` (see is_relevant)."""
    kept = [r for r in results if is_relevant(r.title, query)]
    dropped = len(results) - len(kept)
    if dropped:
        logger.debug("Dropped %d irrelevant result(s) for %r.", dropped, query)
    return kept


# ---------------------------------------------------------------------------
# YTS — movies (JSON API)
# ---------------------------------------------------------------------------

def search_yts(query: str, *, limit: int = 20) -> list[TorrentResult]:
    if not _provider_available("yts"):
        logger.debug("YTS circuit open; skipping search for %r.", query)
        return []
    url = (
        "https://yts.mx/api/v2/list_movies.json?"
        + urllib.parse.urlencode({"query_term": query, "limit": limit, "sort_by": "seeds"})
    )
    try:
        payload = json.loads(_http_get(url))
    except Exception as exc:
        _record_provider_failure("yts", exc)
        return []
    _record_provider_success("yts")

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

# TPB category ids. Searching the specific categories a media type actually
# lives in beats the whole-video tree (cat=200): apibay caps a response at 100
# rows, so a broad search spends that budget on music videos and 3D rips before
# it reaches the film you asked for. Movies get plain Movies, HD Movies and
# Movies-DVDR; shows get TV and HD TV. 4K is not a category — it is scored
# against in torrent_select, so a 2160p-only title is still findable.
_TPB_CATEGORIES: dict[str, tuple[str, ...]] = {
    "movie": ("201", "207", "202"),   # Movies, HD Movies, Movies DVDR
    "tv": ("205", "208"),             # TV shows, HD TV shows
    "anime": ("205", "208", "201", "207"),
    "xanime": ("205", "208", "201", "207"),
}
# Everything else (media_type "other"/"unknown") keeps the whole video tree.
_TPB_DEFAULT_CATEGORIES = ("200",)

# apibay never returns more than this many rows per request; hitting it means
# the answer was truncated and per-category requests are worth the extra calls.
_TPB_PAGE_CAP = 100


# ---------------------------------------------------------------------------
# Torrentio — ID-keyed (IMDb), aggregates TPB/1337x/TGx/EZTV/YTS/Nyaa/...
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TorrentioTarget:
    """What to ask Torrentio for: a movie, or one episode of a series."""
    imdb_id: str
    kind: str                  # "movie" | "series"
    season: int | None = None
    episode: int | None = None


_EPISODE_MARK_RE = re.compile(
    r"(?i)(?<![a-z0-9])(s\d{1,2}e\d{1,3}|\d{1,2}x\d{2,3})(?![0-9])")
_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def _torrent_flags_matter(torrent_name: str) -> bool:
    """True when the torrent's own name carries a red flag the inner file name
    may not: a CAM marker or a non-English language tag. Then the torrent name
    is what the gates must judge ("Movie.2024.HDCAM-XYZ/Movie.2024.mkv")."""
    try:
        import rtn_compat
        import video_quality
        if video_quality.is_cam_release(torrent_name):
            return True
        parsed, _reason = rtn_compat.parse_release(torrent_name)
        languages = {str(c).lower() for c in (getattr(parsed, "languages", None) or ())}
        return bool(languages - {"en"})
    except Exception:
        return False


def _torrentio_title(stream: "torrentio.TorrentioStream") -> str:
    """The name the selector should judge. For a file inside a multi-file
    torrent that is the FILE's name when it identifies itself (show name plus
    an episode marker, or a movie title plus year); otherwise the torrent's."""
    if stream.file_path and stream.file_name and not _torrent_flags_matter(
            stream.torrent_name):
        stem = stream.file_name.rsplit(".", 1)[0]
        mark = _EPISODE_MARK_RE.search(stem)
        if mark and re.search(r"[A-Za-z]{3,}", stem[:mark.start()]):
            return stream.file_name
        if not mark and _YEAR_RE.search(stem) and re.search(r"[A-Za-z]{3,}", stem):
            return stream.file_name
    return stream.torrent_name


# Torrentio aggregates 25 sites. For everything but anime, English is the
# language that matters, so the foreign-language sites (Russian, Brazilian,
# Spanish, Italian, French) stay out of the pool instead of feeding it dubs.
# Anime adds the anime trackers. Cams, screeners and 3D are filtered at the
# source. No limit and no sort: Sensarr's selector ranks the full pool itself.
# Each type can be overridden with a raw "key=value|key=value" string in
# Settings (TORRENTIO_OPTIONS_MOVIE / _TV / _ANIME / _XANIME).
_TORRENTIO_GENERAL = ("yts", "eztv", "rarbg", "1337x", "ext", "thepiratebay",
                      "kickasstorrents", "torrentgalaxy", "magnetdl")
_TORRENTIO_ANIME = ("nyaasi", "horriblesubs", "tokyotosho", "anidex", "nekobt")
_TORRENTIO_EXCLUDED_QUALITIES = ("threed", "scr", "cam")
TORRENTIO_TYPE_KEYS = {"movie": "TORRENTIO_OPTIONS_MOVIE",
                       "tv": "TORRENTIO_OPTIONS_TV",
                       "anime": "TORRENTIO_OPTIONS_ANIME",
                       "xanime": "TORRENTIO_OPTIONS_XANIME"}


def torrentio_default_options(media_type: str) -> dict:
    """Sensarr's own Torrentio config for a type: English-language sites, the
    anime trackers for anime and hentai, cams/screeners/3D filtered out."""
    providers = _TORRENTIO_GENERAL + (
        _TORRENTIO_ANIME if media_type in ("anime", "xanime") else ())
    return {"providers": ",".join(providers),
            "qualityfilter": ",".join(_TORRENTIO_EXCLUDED_QUALITIES)}


def format_torrentio_options(opts: dict) -> str:
    """The dict back into Torrentio's own "key=value|key=value" syntax."""
    return "|".join(f"{k}={v}" for k, v in opts.items())


def torrentio_options(media_type: str) -> dict:
    """The Torrentio config segment for one media type, as a dict: that type's
    override from Settings, else the old all-types override, else the
    default."""
    key = TORRENTIO_TYPE_KEYS.get(media_type, "TORRENTIO_OPTIONS_TV")
    raw = ((getattr(config, key, "") or "").strip()
           or (getattr(config, "TORRENTIO_OPTIONS", "") or "").strip())
    if raw:
        opts = {}
        for part in raw.split("|"):
            k, _, value = part.partition("=")
            if k.strip() and value.strip():
                opts[k.strip()] = value.strip()
        if opts:
            return opts
    return torrentio_default_options(media_type)


def debrid_cache_key() -> str | None:
    """The Real-Debrid token to send Torrentio for cached-release tagging, or
    None. Only when RD will actually be the download engine (qBittorrent off):
    a 'cached' tag lets a 0-seed release past the seeder floor, which is only
    true for a release Real-Debrid is going to fetch."""
    if (config.REALDEBRID_API_TOKEN and config.REALDEBRID_CACHE_CHECK
            and not config.QBITTORRENT_ENABLED):
        return config.REALDEBRID_API_TOKEN
    return None


def search_torrentio(target: TorrentioTarget | None,
                     media_type: str) -> list[TorrentResult]:
    """Releases Torrentio knows for exactly this movie/episode. [] when off,
    unknown, or the provider circuit is open."""
    if target is None or not target.imdb_id or not config.TORRENTIO_ENABLED:
        return []
    if not _provider_available("torrentio"):
        logger.debug("Torrentio circuit open; skipping %s.", target.imdb_id)
        return []
    key = debrid_cache_key()
    query = dict(season=target.season, episode=target.episode,
                 base=config.TORRENTIO_BASE_URL,
                 options=torrentio_options(media_type))
    try:
        # The full pool comes from a KEYLESS query: with a Real-Debrid key
        # Torrentio answers mostly with cached releases and drops most of the
        # rest (The Love Hypothesis, 2026-10-10: 51 releases without the key,
        # 11 with it, every plain-English copy gone).
        streams = torrentio.search(target.imdb_id, target.kind, **query)
    except torrentio.TorrentioError as exc:
        _record_provider_failure("torrentio", exc)
        return []
    except ValueError as exc:
        logger.debug("Torrentio target unusable (%s): %s", target, exc)
        return []
    _record_provider_success("torrentio")

    # The keyed query is used only to learn which releases RD already holds.
    cached_hashes: set[str] = set()
    cache_known = False
    extra: list = []
    if key:
        try:
            keyed = torrentio.search(target.imdb_id, target.kind,
                                     debrid_key=key, **query)
            cached_hashes = {s.info_hash for s in keyed if s.cached}
            known = {s.info_hash for s in streams}
            extra = [s for s in keyed if s.info_hash not in known]
            cache_known = True
        except torrentio.TorrentioError as exc:
            logger.info("Torrentio cache check failed (pool kept, cache "
                        "state unknown): %s", exc)

    results = []
    for stream in list(streams) + extra:
        cached = (True if stream.info_hash in cached_hashes
                  else (False if cache_known else None))
        if stream.size_bytes <= 0 and cached is not True:
            continue
        in_pack = bool(stream.file_path)
        results.append(TorrentResult(
            title=_torrentio_title(stream),
            magnet=torrentio.magnet_for(stream, _TRACKERS),
            size_bytes=stream.size_bytes, seeders=stream.seeders,
            source="torrentio", media_type=media_type,
            file_idx=stream.file_idx if in_pack else None,
            file_name=stream.file_name if in_pack else None,
            cached=cached,
            audio_tags=tuple(stream.audio_tags) + tuple(stream.language_flags)))
    return results


def _torrentio_is_enough(results: list[TorrentResult], media_type: str) -> bool:
    """Skip the text providers when Torrentio already answered well: saves the
    apibay calls that kept tripping its rate limit. Anime still asks Nyaa (new
    fansub releases land there first)."""
    if media_type in ("anime", "xanime") or not results:
        return False
    floor = max(1, int(config.MIN_SEEDERS))
    distinct = {_infohash(r.magnet) or r.title.casefold() for r in results}
    # Enough copies to choose between, and at least one that can actually be
    # fetched (seeded past the floor, or already cached on Real-Debrid). A
    # single cached result is not a pool: it may be a CAM the gates reject.
    return (len(distinct) >= _ENOUGH_RESULTS
            and any(r.seeders >= floor or r.cached for r in results))


def tpb_categories(media_type: str) -> tuple[str, ...]:
    """The TPB category ids to search for a media type."""
    return _TPB_CATEGORIES.get(media_type, _TPB_DEFAULT_CATEGORIES)


def _tpb_request(query: str, cat: str, media_type: str) -> list[TorrentResult]:
    """One apibay call. Returns [] on failure (and trips the circuit)."""
    url = "https://apibay.org/q.php?" + urllib.parse.urlencode(
        {"q": query, "cat": cat})
    try:
        payload = json.loads(_http_get(url))
    except Exception as exc:
        _record_provider_failure("tpb", exc)
        return []
    _record_provider_success("tpb")

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
    return results


def search_tpb(query: str, media_type: str, *, limit: int = 30,
               collect: bool = False) -> list[TorrentResult]:
    if not _provider_available("tpb"):
        logger.debug("TPB circuit open; skipping search for %r.", query)
        return []

    categories = tpb_categories(media_type)
    # One combined request first (apibay accepts a comma-separated list); only
    # when it comes back capped is the answer truncated enough to be worth
    # asking each category separately.
    results = _tpb_request(query, ",".join(categories), media_type)
    if len(results) >= _TPB_PAGE_CAP and len(categories) > 1:
        seen = {_infohash(r.magnet) for r in results}
        for cat in categories:
            for extra in _tpb_request(query, cat, media_type):
                ih = _infohash(extra.magnet)
                if ih and ih in seen:
                    continue
                seen.add(ih)
                results.append(extra)

    if not results and categories != _TPB_DEFAULT_CATEGORIES:
        # Uploaders miscategorise constantly. Narrowing the search must make
        # the good results easier to find, never make a findable release
        # unfindable, so an empty answer falls back to the whole video tree.
        results = _tpb_request(query, ",".join(_TPB_DEFAULT_CATEGORIES),
                               media_type)

    # Collection mode keeps the API's own order and only BOUNDS the pool — it
    # must not seeder-sort-then-truncate, or a correct low-seed release gets
    # dropped before the gates ever run (section 4 item 1).
    if collect:
        return results[:limit]
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
                     media_type: str, *, limit: int = 30,
                     collect: bool = False) -> list[TorrentResult]:
    if not _provider_available(source):
        logger.debug("%s circuit open; skipping search for %r.", source, query)
        return []
    # RSS is sorted by seeders desc server-side. In collection mode we still
    # only BOUND the pool (take the first `limit`); the point is that `limit` is
    # the wide per-source pool, not a narrow final cut, and the selection gates —
    # not seeders — decide the winner (section 4 item 1).
    url = f"{base}/?" + urllib.parse.urlencode(
        {"page": "rss", "q": query, "c": category, "f": "0", "s": "seeders", "o": "desc"}
    )
    try:
        root = ET.fromstring(_http_get(url))
    except Exception as exc:
        _record_provider_failure(source, exc)
        return []
    _record_provider_success(source)

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


def search_nyaa(query: str, *, limit: int = 30,
                collect: bool = False) -> list[TorrentResult]:
    # c=1_2: Anime — English-translated (same filter the old browser links used)
    return _search_nyaa_rss("https://nyaa.si", query, "1_2", "nyaa", "anime",
                            limit=limit, collect=collect)


def search_sukebei(query: str, *, limit: int = 30,
                   collect: bool = False) -> list[TorrentResult]:
    return _search_nyaa_rss("https://sukebei.nyaa.si", query, "0_0", "sukebei",
                            "xanime", limit=limit, collect=collect)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

def _search_one(query: str, media_type: str, *, limit: int,
                collect: bool) -> list[TorrentResult]:
    """Every source for a media type, for exactly one spelling of the query."""
    results: list[TorrentResult] = []
    if media_type == "movie":
        results.extend(search_yts(query, limit=limit))
        results.extend(search_tpb(query, media_type, limit=limit, collect=collect))
    elif media_type == "anime":
        results.extend(search_nyaa(query, limit=limit, collect=collect))
    elif media_type == "xanime":
        results.extend(search_sukebei(query, limit=limit, collect=collect))
        # Softer titles live on the main nyaa site, not just sukebei.
        results.extend(_search_nyaa_rss("https://nyaa.si", query, "1_2", "nyaa",
                                        "xanime", limit=limit, collect=collect))
    else:  # tv / other / unknown
        results.extend(search_tpb(query, media_type, limit=limit, collect=collect))
    return results


# A pool worth stopping on: something is actually seeded, and there is enough
# of it to choose between. Anything thinner and the ladder keeps climbing —
# one dead 0-seeder result is "nothing download-worthy", not a search hit.
_ENOUGH_RESULTS = 3


def _pool_is_solid(results: list[TorrentResult]) -> bool:
    distinct = {_infohash(r.magnet) or r.title.casefold() for r in results}
    return (len(distinct) >= _ENOUGH_RESULTS
            and any(r.seeders > 0 for r in results))


def _ladder(query: str, media_type: str, *, per_source: int, collect: bool,
            fetch) -> tuple[list[tuple[str, list[TorrentResult]]],
                            list[str], tuple[str, ...]]:
    """Walk the spelling ladder, merging until the pool is worth stopping on.

    `fetch(variant)` returns [(source, results), ...] for one spelling. Every
    spelling's results are relevance-filtered before they count, so apibay's
    trending-list answer to an unmatched query can never masquerade as a hit.

    Returns (per_source_results, queries_that_contributed, queries_tried).
    """
    variants = query_variants(query)
    per_source_totals: dict[str, list[TorrentResult]] = {}
    contributed: list[str] = []
    tried: list[str] = []
    seen: set[str] = set()

    for variant in variants:
        tried.append(variant)
        # Relevance is judged against the ORIGINAL request for the faithful
        # spelling and against the rewrite for the rest — a distinctive-word
        # search is supposed to return more than the exact phrase.
        against = query if variant == variants[0] else variant
        got_any = False
        for source, results in fetch(variant):
            kept = filter_relevant(results, against)
            # Duplicates are NOT dropped here — search_collect's own dedupe
            # keeps the healthier copy of an identical payload and reports how
            # many it merged. `seen` only answers "did this spelling add
            # anything new", which is what decides whether to keep climbing.
            fresh = [r for r in kept
                     if (_infohash(r.magnet) or r.title.casefold()) not in seen]
            seen.update(_infohash(r.magnet) or r.title.casefold() for r in kept)
            per_source_totals.setdefault(source, []).extend(kept)
            got_any = got_any or bool(fresh)
        if got_any:
            contributed.append(variant)
            if variant != variants[0]:
                logger.info("Search for %r was thin; %r added results.",
                            query, variant)
        combined = [r for results in per_source_totals.values() for r in results]
        if _pool_is_solid(combined):
            break

    return list(per_source_totals.items()), contributed, tuple(tried)


def search_with_variants(query: str, media_type: str, *, limit: int = 30,
                         collect: bool = False
                         ) -> tuple[list[TorrentResult], str, tuple[str, ...]]:
    """Walk the spelling ladder and return (results, query_used, queries_tried).

    Results are always relevance-filtered, so "the pool is non-empty" finally
    means what callers have always assumed it means.
    """
    def fetch(variant: str):
        return [("all", _search_one(variant, media_type, limit=limit,
                                    collect=collect))]

    per_source, contributed, tried = _ladder(
        query, media_type, per_source=limit, collect=collect, fetch=fetch)
    results = [r for _src, batch in per_source for r in batch]
    used = ", ".join(contributed) if contributed else (tried[0] if tried else "")
    return results, used, tried


def search_torrents(query: str, media_type: str, *, limit: int = 30) -> list[TorrentResult]:
    """Search the right source(s) for the media type, best-seeded first."""
    return search_torrents_detail(query, media_type, limit=limit)[0]


def search_torrents_detail(query: str, media_type: str, *, limit: int = 30
                           ) -> tuple[list[TorrentResult], str]:
    """search_torrents plus the spelling that actually found the results, so
    the UI can say a search for "C.H.U.D." was answered by "CHUD"."""
    query = query.strip()
    if not query:
        return [], ""

    results, used, _tried = search_with_variants(query, media_type, limit=limit)
    # One row per payload, keeping the copy reporting the healthier swarm.
    best: dict[str, TorrentResult] = {}
    for result in results:
        key = _infohash(result.magnet) or result.title.casefold()
        if key not in best or result.seeders > best[key].seeders:
            best[key] = result
    unique = sorted(best.values(), key=lambda r: r.seeders, reverse=True)
    return unique[:limit], used


# ---------------------------------------------------------------------------
# Collection mode — wide per-source pools for the selection engine (Task B)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CollectedPool:
    """A widened, deduped candidate pool plus the per-source counts the
    selection decision records (pick_meta). `results` is NOT seeder-truncated —
    the selection gates, not seeders, decide the winner."""
    results: tuple           # tuple[TorrentResult, ...]
    pool_stats: dict         # {"per_source": {src: n}, "collected": n,
                             #  "deduped": n, "duplicates_removed": n}


def search_collect(query: str, media_type: str, *,
                   per_source: int = 50,
                   torrentio_target: TorrentioTarget | None = None
                   ) -> CollectedPool:
    """Gather wide per-source pools (30-50 each), normalize, and dedupe by
    info-hash WITHOUT any global seeder truncation. This is what every automatic
    path uses instead of search_torrents (section 4 item 1). Per-source pool
    sizes are recorded for pick_meta.

    Automatic callers are NOT rewired to this yet — that is Phase 3. search_torrents
    keeps its legacy seeder-sorted shape for manual/legacy callers.
    """
    query = query.strip()
    per_source = max(1, min(int(per_source), 50))
    if not query:
        return CollectedPool(results=tuple(),
                             pool_stats={"per_source": {}, "collected": 0,
                                         "deduped": 0, "duplicates_removed": 0})

    # ID-keyed first: Torrentio answers for exactly this movie/episode. When
    # that pool is already good enough the text providers are not asked.
    torrentio_results = search_torrentio(torrentio_target, media_type)
    skip_text = _torrentio_is_enough(torrentio_results, media_type)

    # The spelling ladder runs here, so every automatic caller gets the
    # punctuation-free / distinctive-word / translated retries for free.
    def fetch(variant: str):
        if media_type == "movie":
            return [("yts", search_yts(variant, limit=per_source)),
                    ("tpb", search_tpb(variant, media_type,
                                       limit=per_source, collect=True))]
        if media_type == "anime":
            return [("nyaa", search_nyaa(variant, limit=per_source, collect=True))]
        if media_type == "xanime":
            return [("sukebei", search_sukebei(variant, limit=per_source,
                                               collect=True))]
        return [("tpb", search_tpb(variant, media_type,
                                   limit=per_source, collect=True))]

    if skip_text:
        per_source_results, contributed, variants_tried = [], [], ()
    else:
        per_source_results, contributed, variants_tried = _ladder(
            query, media_type, per_source=per_source, collect=True, fetch=fetch)
    if torrentio_target is not None:
        per_source_results = [("torrentio", torrentio_results),
                              *per_source_results]
    used_query = ", ".join(contributed) if contributed else query

    per_source_stats: dict = {}
    collected: list[TorrentResult] = []
    for src, res in per_source_results:
        per_source_stats[src] = len(res)
        collected.extend(res)

    # Dedupe by info-hash. On a collision keep the copy reporting more seeders
    # (better swarm health for the identical payload); order is otherwise stable.
    # A Torrentio copy always survives (it knows the exact file inside a pack
    # and whether it is debrid-cached) but takes the higher seeder count.
    seen: dict[str, int] = {}
    deduped: list[TorrentResult] = []
    for r in collected:
        ih = _infohash(r.magnet)
        key = ih or f"__noihash__{len(deduped)}"
        if key in seen:
            idx = seen[key]
            kept = deduped[idx]
            if "torrentio" in (kept.source, r.source):
                base = kept if kept.source == "torrentio" else r
                deduped[idx] = replace(base, seeders=max(kept.seeders, r.seeders))
            elif r.seeders > kept.seeders:
                deduped[idx] = r
            continue
        seen[key] = len(deduped)
        deduped.append(r)

    stats = {
        "per_source": per_source_stats,
        "provider_health": provider_circuit_status(
            tuple(source for source, _results in per_source_results)),
        "collected": len(collected),
        "deduped": len(deduped),
        "duplicates_removed": len(collected) - len(deduped),
        # Which spelling actually answered, and everything tried on the way.
        "query_used": used_query,
        "query_variants_tried": list(variants_tried),
        "text_search_skipped": skip_text,
    }
    return CollectedPool(results=tuple(deduped), pool_stats=stats)


def format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size_bytes} B"
