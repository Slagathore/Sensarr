import json
import logging
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlexUserMetrics:
    label: str
    play_count: int
    watch_time_ms: int


@dataclass(frozen=True)
class PlexSessionMetrics:
    user_label: str
    title: str
    media_type: str
    state: str
    player: str


@dataclass(frozen=True)
class PlexRecentPlay:
    viewed_at: int
    user_label: str
    title: str
    media_type: str


@dataclass(frozen=True)
class PlexLibrarySearchResult:
    name: str
    path: str
    section_title: str
    media_type: str
    size_bytes: int
    added_at: int


@dataclass(frozen=True)
class PlexLibrarySection:
    title: str
    section_type: str
    item_count: int
    locations: list[str]
    hidden: bool


@dataclass(frozen=True)
class PlexMetrics:
    configured: bool
    connected: bool
    server_name: str
    current_sessions: int
    active_users: int
    history_items: int
    plays_7d: int
    plays_30d: int
    watch_time_7d_ms: int
    watch_time_30d_ms: int
    top_users_30d: list[PlexUserMetrics]
    current_session_details: list[PlexSessionMetrics]
    recent_plays: list[PlexRecentPlay]
    section_counts: list[tuple[str, str, int]]
    error: str | None = None


@dataclass(frozen=True)
class PlexSectionInventoryMetrics:
    title: str
    section_type: str
    item_count: int
    movie_count: int
    show_count: int
    episode_count: int
    total_duration_ms: int
    recent_7d: int
    recent_30d: int
    locations: list[str]
    recent_titles: list[str]
    hidden: bool


@dataclass(frozen=True)
class PlexLibraryInventoryMetrics:
    configured: bool
    connected: bool
    total_movies: int
    total_shows: int
    total_episodes: int
    total_runtime_ms: int
    recent_7d: int
    recent_30d: int
    sections: list[PlexSectionInventoryMetrics]
    error: str | None = None


def plex_metrics_enabled() -> bool:
    return bool(config.PLEX_SERVER_URL and config.PLEX_TOKEN)


def _normalized_base_url() -> str:
    return config.PLEX_SERVER_URL.rstrip("/")


def _default_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Token": config.PLEX_TOKEN,
        "X-Plex-Client-Identifier": config.PLEX_CLIENT_IDENTIFIER or "plex-reset-button",
        "X-Plex-Product": config.APP_PRODUCT_NAME,
        "X-Plex-Version": config.APP_VERSION,
        "X-Plex-Platform": "Windows",
    }


def _ssl_context() -> ssl.SSLContext | None:
    if config.PLEX_VERIFY_SSL:
        return None
    return ssl._create_unverified_context()


def _request_json(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not plex_metrics_enabled():
        raise RuntimeError("PLEX_SERVER_URL and PLEX_TOKEN must be configured.")

    base_url = _normalized_base_url()
    url = f"{base_url}{path}"
    if query:
        encoded = urllib.parse.urlencode(query, doseq=True)
        url = f"{url}?{encoded}"

    headers = _default_headers()
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(url, headers=headers, method="GET")
    # The local Plex server can briefly stall when several requests hit it at
    # once (the app fires status + metrics + library + maintenance refreshes on
    # startup). One retry after a short pause absorbs those transient timeouts
    # instead of surfacing an uncaught TimeoutError. A read-timeout raises a
    # bare TimeoutError (not URLError), so it must be caught explicitly.
    timeout = config.PLEX_REQUEST_TIMEOUT_SECONDS
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
                payload = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Plex API request failed with HTTP {exc.code} for {path}: {details or exc.reason}"
            ) from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
            reason = getattr(exc, "reason", exc)
            if attempt == 0:
                logger.debug("Plex API timeout for %s (%s) — retrying once.", path, reason)
                time.sleep(1.0)
                continue
            raise RuntimeError(f"Plex API connection failed for {path}: {reason}") from exc
    else:  # pragma: no cover - loop always breaks or raises
        raise RuntimeError(f"Plex API connection failed for {path}: {last_error}")

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Plex API returned invalid JSON for {path}.") from exc


def _media_container(payload: dict[str, Any]) -> dict[str, Any]:
    container = payload.get("MediaContainer")
    return container if isinstance(container, dict) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _format_duration_ms(duration_ms: int) -> str:
    total_seconds = max(0, duration_ms // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _playback_title(item: dict[str, Any]) -> str:
    media_type = str(item.get("type") or "").lower()
    title = str(item.get("title") or "Unknown Title")

    if media_type == "episode":
        show = item.get("grandparentTitle") or item.get("parentTitle") or "Unknown Show"
        season = _safe_int(item.get("parentIndex"))
        episode = _safe_int(item.get("index"))
        if season or episode:
            return f"{show} - S{season:02d}E{episode:02d} - {title}"
        return f"{show} - {title}"

    return title


def _session_user_label(
    item: dict[str, Any],
    *,
    account_names: dict[int, str] | None = None,
) -> str:
    for field in ("User", "user", "Account"):
        nested = item.get(field)
        if isinstance(nested, dict):
            for key in ("title", "username", "name"):
                value = nested.get(key)
                if value:
                    return str(value)

    account_id = _safe_int(item.get("accountID"), default=-1)
    if account_id >= 0:
        if account_names is not None and account_id in account_names:
            return account_names[account_id]
        return f"Account {account_id}"
    return "Unknown User"


def _player_label(item: dict[str, Any]) -> str:
    player = item.get("Player")
    if isinstance(player, dict):
        for key in ("title", "product", "platform"):
            value = player.get(key)
            if value:
                return str(value)
    return "Unknown Player"


def _history_entries(limit: int) -> list[dict[str, Any]]:
    payload = _request_json(
        "/status/sessions/history/all",
        extra_headers={
            "X-Plex-Container-Start": "0",
            "X-Plex-Container-Size": str(limit),
        },
    )
    return [
        item
        for item in _as_list(_media_container(payload).get("Metadata"))
        if isinstance(item, dict)
    ]


def _account_name_map() -> dict[int, str]:
    payload = _request_json("/accounts")
    accounts = _as_list(_media_container(payload).get("Account"))
    mapping: dict[int, str] = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        account_id = _safe_int(account.get("id"), default=-1)
        if account_id < 0:
            continue
        name = str(account.get("name") or "").strip()
        if name:
            mapping[account_id] = name
    return mapping


@dataclass(frozen=True)
class WatchHistoryEntry:
    at: str        # local "YYYY-MM-DD HH:MM"
    user: str
    title: str     # "Show - S01E02 - Episode Title" or movie title


def get_watch_history(limit: int = 200) -> list[WatchHistoryEntry]:
    """Recent plays across all users — for the Users tab history viewer."""
    from datetime import datetime
    names = _account_name_map()
    rows: list[WatchHistoryEntry] = []
    for item in _history_entries(limit):
        viewed = _safe_int(item.get("viewedAt"))
        at = (datetime.fromtimestamp(viewed).strftime("%Y-%m-%d %H:%M")
              if viewed else "")
        rows.append(WatchHistoryEntry(
            at=at,
            user=_session_user_label(item, account_names=names),
            title=_playback_title(item),
        ))
    return rows


# ---------------------------------------------------------------------------
# Watchlist (plex.tv discover API — account-level, uses the same token)
# ---------------------------------------------------------------------------

_DISCOVER_URL = "https://discover.provider.plex.tv"


@dataclass(frozen=True)
class WatchlistItem:
    title: str
    year: int | None
    item_type: str      # "movie" | "show"
    # Provider ids parsed from the Plex Guid list — a watchlist item usually
    # already carries a tmdb/tvdb/imdb GUID, so we keep the identity instead of
    # re-searching by title (Task A). None when Plex gave no GUID.
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    imdb_id: str | None = None

    @property
    def identity(self) -> tuple[str, str] | None:
        """(identity_source, external_id) preferring the provider Plex's own
        agents key on: tmdb for movies, tvdb then tmdb for shows. None when no
        usable GUID is present."""
        if self.item_type == "show":
            if self.tvdb_id:
                return ("tvdb", self.tvdb_id)
            if self.tmdb_id:
                return ("tmdb", self.tmdb_id)
        else:
            if self.tmdb_id:
                return ("tmdb", self.tmdb_id)
        if self.imdb_id:
            return ("omdb", self.imdb_id)
        return None


def _parse_plex_guids(item: dict) -> dict[str, str]:
    """Extract {provider: id} from a Plex Metadata item's Guid list.

    Discover/PMS return GUIDs as [{"id": "tmdb://12345"}, {"id": "tvdb://678"},
    {"id": "imdb://tt123"}]. A single legacy 'guid' string is also handled.
    """
    out: dict[str, str] = {}

    def _add(raw: str) -> None:
        raw = (raw or "").strip()
        for prov, prefix in (("tmdb", "tmdb://"), ("tvdb", "tvdb://"), ("imdb", "imdb://")):
            if raw.startswith(prefix):
                out[prov] = raw[len(prefix):].split("?")[0]

    for g in _as_list(item.get("Guid")):
        if isinstance(g, dict):
            _add(str(g.get("id") or ""))
    single = item.get("guid")
    if isinstance(single, str):
        _add(single)
    return out


_accounts_cache: list[str] = []


def list_plex_accounts(*, force: bool = False) -> list[str]:
    """Account names known to this server (for the you-are dropdowns).

    Cached after the first success so combobox refreshes are instant; a
    failed fetch keeps whatever the cache had (possibly empty) so the UI can
    simply retry on the next dropdown open."""
    global _accounts_cache
    if _accounts_cache and not force:
        return _accounts_cache
    try:
        _accounts_cache = sorted(_account_name_map().values(), key=str.casefold)
    except Exception as exc:
        logger.debug("Plex account list fetch failed: %s", exc)
    return _accounts_cache


def get_watchlist(limit: int = 100) -> list[WatchlistItem]:
    """The account's Plex watchlist, via the plex.tv discover API."""
    import json as _json
    import urllib.request as _rq
    if not config.PLEX_TOKEN:
        raise RuntimeError("Plex token required — click 'Get Plex Token' first.")
    req = _rq.Request(
        f"{_DISCOVER_URL}/library/sections/watchlist/all"
        f"?X-Plex-Container-Start=0&X-Plex-Container-Size={limit}",
        headers={"Accept": "application/json", "X-Plex-Token": config.PLEX_TOKEN},
    )
    with _rq.urlopen(req, timeout=20) as resp:
        payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
    items = _as_list(_media_container(payload).get("Metadata"))
    out: list[WatchlistItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        guids = _parse_plex_guids(item)
        out.append(WatchlistItem(
            title=str(item.get("title") or "Unknown"),
            year=_safe_int(item.get("year")) or None,
            item_type=str(item.get("type") or "movie"),
            tmdb_id=guids.get("tmdb"),
            tvdb_id=guids.get("tvdb"),
            imdb_id=guids.get("imdb"),
        ))
    return out


# ---------------------------------------------------------------------------
# Recommendations — two honest sections: "In your library (unwatched)" and
# "Discover (not in library)", never mixed (fix sprint, Task C).
#
# Presence (owned-or-not) is checked provider-id FIRST — a Plex GUID / TMDB /
# TVDB id, read the same way _parse_plex_guids / WatchlistItem.identity
# already do elsewhere in this module — and identity-aware title match
# SECOND (media_lookup.check_library_for_title, which applies
# media_identity.sequel_mismatch — different sequel/part numbers are treated
# as a different entry no matter how similar the base titles are). Filename
# substrings are NEVER used for presence; that was the bug in both this
# function's old TMDB-owned relabelling and watchlist_tab's "In Library"
# column.
#
# Discover is seeded from TMDB similar/recommendations on this account's
# recency-weighted most-watched titles (a title watched recently outweighs
# one watched the same number of times a year ago), blended with TMDB's own
# vote_average/vote_count, excluding watched, owned, and already-requested/
# queued titles. Falls back to the existing popular-in-genre discover when
# watch history is too thin to seed anything trustworthy.
#
# Scope note: Discover candidates come from TMDB only. Anime titles within
# that TMDB set are labelled via the same original_language=='ja'/genre-16
# heuristic media_lookup.search_tmdb_anime uses, so they land in their own
# item_type instead of being lumped into "show". A dedicated Jikan/AniList-
# seeded anime discover path (mirroring the movie/tv seeding below) is a
# reasonable follow-up, not built this sprint.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    title: str
    year: int | None
    item_type: str          # "movie" | "show" | "anime" | "xanime"
    genres: tuple[str, ...]
    note: str               # "because you watch Action, Sci-Fi" / "similar to X"
    in_library: bool
    tmdb_id: str | None = None   # provider id, when known (mainly Discover)


@dataclass(frozen=True)
class RecommendationsResult:
    top_genres: list[str]
    library: list[Recommendation]     # "In your library (unwatched)"
    discover: list[Recommendation]    # "Discover (not in library)"
    generated_at: float               # time.time() this result was built


@dataclass(frozen=True)
class LibraryProviderIndex:
    """Provider ids the Plex library currently holds, split by movie/show
    namespace — a TMDB movie id and a TMDB tv id are different id spaces, so
    the same numeric id can legitimately mean two different titles."""
    movie_tmdb_ids: frozenset = frozenset()
    movie_imdb_ids: frozenset = frozenset()
    show_tmdb_ids: frozenset = frozenset()
    show_tvdb_ids: frozenset = frozenset()
    show_imdb_ids: frozenset = frozenset()


_RECS_HISTORY_DEPTH = 300
# Fewer than this many recency-weighted seeds (or zero candidates back from
# TMDB for the seeds we do have) is "too thin to trust" — Discover falls back
# to the popular-in-genre list instead of guessing off one data point.
_DISCOVER_MIN_SEEDS = 3
_DISCOVER_HALF_LIFE_DAYS = 30.0


def recs_cache_is_stale(generated_at: float, *, ttl_hours: float,
                        now: float | None = None) -> bool:
    """True when a cached recommendations payload is old enough to refetch.
    A missing/zero timestamp (no freshness stamp yet, or a cache miss) is
    always stale."""
    if not generated_at:
        return True
    now = time.time() if now is None else now
    return (now - generated_at) > (ttl_hours * 3600.0)


def _section_media_kind(section: dict[str, Any], plex_type: str) -> str:
    """Map a Plex section to Sensarr's movie/show/anime/xanime tagging via
    the configured library-path types (config.MEDIA_LIBRARY_PATHS) — Plex
    itself only ever reports section type "movie" or "show", so a dedicated
    "Anime" Plex library was previously indistinguishable from a regular one
    here. Falls back to the raw Plex section type when no configured anime/
    xanime path matches a section's location (the common case)."""
    locations = [
        str(loc.get("path")) for loc in _as_list(section.get("Location"))
        if isinstance(loc, dict) and loc.get("path")
    ]
    if not locations:
        return plex_type
    for raw in locations:
        try:
            loc_path = Path(raw).resolve()
        except OSError:
            loc_path = Path(raw)
        for entry in config.MEDIA_LIBRARY_PATHS:
            if entry.media_type not in ("anime", "xanime"):
                continue
            try:
                root = Path(entry.path).resolve()
            except OSError:
                root = Path(entry.path)
            if loc_path == root:
                return entry.media_type
            try:
                loc_path.relative_to(root)
                return entry.media_type
            except ValueError:
                pass
            try:
                root.relative_to(loc_path)
                return entry.media_type
            except ValueError:
                continue
    return plex_type


def build_library_provider_index() -> LibraryProviderIndex:
    """Provider ids (TMDB/TVDB/IMDB) the Plex library currently holds, read
    from the SAME Guid field Plex itself carries on every item
    (_parse_plex_guids) — so presence checks are provider-id exact instead of
    filename-substring guesses. Movie/show ids are kept in separate
    namespaces (see LibraryProviderIndex)."""
    movie_tmdb: set[str] = set()
    movie_imdb: set[str] = set()
    show_tmdb: set[str] = set()
    show_tvdb: set[str] = set()
    show_imdb: set[str] = set()
    for section in _library_sections():
        section_type = str(section.get("type") or "")
        if section_type not in ("movie", "show"):
            continue
        key = str(section.get("key") or "")
        if not key:
            continue
        try:
            items = _section_items(key)
        except Exception:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            guids = _parse_plex_guids(item)
            if section_type == "movie":
                if guids.get("tmdb"):
                    movie_tmdb.add(guids["tmdb"])
                if guids.get("imdb"):
                    movie_imdb.add(guids["imdb"])
            else:
                if guids.get("tmdb"):
                    show_tmdb.add(guids["tmdb"])
                if guids.get("tvdb"):
                    show_tvdb.add(guids["tvdb"])
                if guids.get("imdb"):
                    show_imdb.add(guids["imdb"])
    return LibraryProviderIndex(
        movie_tmdb_ids=frozenset(movie_tmdb), movie_imdb_ids=frozenset(movie_imdb),
        show_tmdb_ids=frozenset(show_tmdb), show_tvdb_ids=frozenset(show_tvdb),
        show_imdb_ids=frozenset(show_imdb))


def check_item_in_library(
    title: str, media_kind: str, *, tmdb_id: str | None = None,
    tvdb_id: str | None = None, imdb_id: str | None = None,
    provider_index: LibraryProviderIndex | None = None,
) -> bool:
    """Presence check for one title. Provider id FIRST (an exact Plex GUID /
    TMDB / TVDB id match against `provider_index`), identity-aware title
    match SECOND (media_lookup.check_library_for_title, which already applies
    media_identity.sequel_mismatch — a candidate with a different sequel/part
    number is never treated as the same title, however similar the base
    titles look). Note: check_library_for_title does NOT additionally call
    media_identity.numeric_title_mismatch — it doesn't need to, since
    sequel_mismatch is symmetric (any differing sequel signature is already a
    mismatch) where numeric_title_mismatch is the deliberately narrower,
    directional guard compare_media_identity uses for grab verification.
    Filename substrings are never used here — this is the single presence
    check both get_recommendations and watchlist_tab's "In Library" column
    call, replacing the two separate substring checks that used to live in
    each."""
    kind = "movie" if media_kind == "movie" else "show"
    if provider_index is not None:
        if kind == "movie":
            if tmdb_id and str(tmdb_id) in provider_index.movie_tmdb_ids:
                return True
            if imdb_id and str(imdb_id) in provider_index.movie_imdb_ids:
                return True
        else:
            if tvdb_id and str(tvdb_id) in provider_index.show_tvdb_ids:
                return True
            if tmdb_id and str(tmdb_id) in provider_index.show_tmdb_ids:
                return True
            if imdb_id and str(imdb_id) in provider_index.show_imdb_ids:
                return True
    if not title:
        return False
    try:
        from media_lookup import check_library_for_title
        found, _matches = check_library_for_title(
            title, "movie" if kind == "movie" else "tv")
        return found
    except Exception:
        return False


def _history_seed_weights(history: list[dict[str, Any]], *,
                          now: float | None = None,
                          limit: int = 8) -> list[dict[str, Any]]:
    """Recency-weighted most-watched seeds from raw Plex history entries.

    Each distinct title (grandparentRatingKey for episodes, ratingKey for
    movies) accumulates a score per watch, exponentially decayed by age
    (half-life _DISCOVER_HALF_LIFE_DAYS) — a title watched several times
    recently outranks one watched the same number of times a year ago.
    Returns up to `limit` seeds, highest score first. A watch with no
    viewedAt timestamp is treated as old (four half-lives back) rather than
    "now", so malformed history entries never dominate the seed list.
    """
    now = time.time() if now is None else now
    scores: dict[str, float] = {}
    meta: dict[str, dict[str, Any]] = {}
    for h in history:
        rk = str(h.get("grandparentRatingKey") or h.get("ratingKey") or "")
        if not rk:
            continue
        viewed = _safe_int(h.get("viewedAt"))
        age_days = (max(0.0, (now - viewed) / 86400.0) if viewed
                   else _DISCOVER_HALF_LIFE_DAYS * 4)
        scores[rk] = scores.get(rk, 0.0) + 0.5 ** (age_days / _DISCOVER_HALF_LIFE_DAYS)
        if rk not in meta:
            is_episode = str(h.get("type") or "").casefold() == "episode"
            meta[rk] = {
                "rating_key": rk,
                "title": str(h.get("grandparentTitle") or h.get("title") or ""),
                "item_type": "show" if is_episode else "movie",
            }
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
    out = []
    for rk, score in ranked:
        row = dict(meta[rk])
        row["score"] = score
        out.append(row)
    return out


def _seed_provider_id(rating_key: str) -> tuple[str, str] | None:
    """(provider, id) for a watched item, read off its Plex metadata Guid —
    the same GUID data used everywhere else in this module."""
    try:
        payload = _request_json(f"/library/metadata/{rating_key}")
    except Exception:
        return None
    for meta in _as_list(_media_container(payload).get("Metadata")):
        if not isinstance(meta, dict):
            continue
        guids = _parse_plex_guids(meta)
        if guids.get("tmdb"):
            return ("tmdb", guids["tmdb"])
        if guids.get("tvdb"):
            return ("tvdb", guids["tvdb"])
    return None


def _tmdb_get_json(path: str) -> dict[str, Any]:
    import json as _json
    import urllib.request as _rq
    req = _rq.Request(
        f"https://api.themoviedb.org/3/{path}?api_key={config.TMDB_API_KEY}",
        headers={"Accept": "application/json"})
    with _rq.urlopen(req, timeout=15) as resp:
        return _json.loads(resp.read().decode("utf-8", errors="replace"))


def _tmdb_related(media_kind: str, tmdb_id: str) -> list[dict[str, Any]]:
    """TMDB 'recommendations' + 'similar' candidates for one seed, merged and
    de-duplicated by TMDB id. Both endpoints run — recommendations is TMDB's
    own model, similar is genre/keyword based — either alone misses picks."""
    if not config.TMDB_API_KEY:
        return []
    kind = "movie" if media_kind == "movie" else "tv"
    seen: dict[str, dict[str, Any]] = {}
    for endpoint in ("recommendations", "similar"):
        try:
            data = _tmdb_get_json(f"{kind}/{tmdb_id}/{endpoint}")
        except Exception:
            logger.debug("TMDB %s/%s failed for %s.", kind, endpoint, tmdb_id,
                        exc_info=True)
            continue
        results = data.get("results") if isinstance(data, dict) else None
        for item in (results or []):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "")
            if cid and cid not in seen:
                seen[cid] = item
    return list(seen.values())


def _tmdb_quality_score(vote_average: Any, vote_count: Any) -> float:
    """Blend TMDB's rating and its confidence (vote count) into one score —
    a 9.0 average from 3 votes should not outrank a 7.5 from 5000 votes."""
    va = float(vote_average) if vote_average else 0.0
    vc = float(vote_count) if vote_count else 0.0
    return va * math.log10(vc + 10.0)


def _discover_candidate_to_recommendation(
    item: dict[str, Any], media_kind: str, note: str,
) -> Recommendation:
    title = str(item.get("title") or item.get("name") or "")
    date_field = item.get("release_date") or item.get("first_air_date")
    year = (_safe_int(str(date_field)[:4]) or None) if date_field else None
    is_anime = (str(item.get("original_language") or "") == "ja"
               or 16 in (item.get("genre_ids") or []))
    item_type = "anime" if is_anime else media_kind
    return Recommendation(
        title=title, year=year, item_type=item_type, genres=(),
        note=note, in_library=False, tmdb_id=str(item.get("id") or "") or None)


def _discover_from_history(
    mine_history: list[dict[str, Any]], *, watched_titles: set[str],
    provider_index: LibraryProviderIndex, requested_keys: set[tuple[str, str]],
    limit: int, now: float | None = None,
) -> list[Recommendation]:
    """Discover candidates seeded from this account's recency-weighted most-
    watched titles, blended with TMDB vote_average/vote_count. Excludes
    watched, owned, and already-requested/queued titles. Returns an empty
    list when history is too thin to seed (caller falls back to
    popular-in-genre in that case)."""
    seeds = _history_seed_weights(mine_history, now=now)
    if len(seeds) < _DISCOVER_MIN_SEEDS:
        return []

    # tmdb_id -> [running score, TMDB item payload, media_kind, {seed titles}]
    scored: dict[str, list] = {}
    seeded_any = False
    for seed in seeds:
        ident = _seed_provider_id(seed["rating_key"])
        if not ident or ident[0] != "tmdb":
            continue
        candidates = _tmdb_related(seed["item_type"], ident[1])
        if not candidates:
            continue
        seeded_any = True
        for item in candidates:
            cid = str(item.get("id") or "")
            title = str(item.get("title") or item.get("name") or "")
            if not cid or not title:
                continue
            if title.casefold() in watched_titles:
                continue
            if ("tmdb", cid) in requested_keys:
                continue
            quality = _tmdb_quality_score(item.get("vote_average"), item.get("vote_count"))
            contribution = seed["score"] * quality
            if cid in scored:
                scored[cid][0] += contribution
                scored[cid][3].add(seed["title"])
            else:
                scored[cid] = [contribution, item, seed["item_type"], {seed["title"]}]

    if not seeded_any:
        return []

    ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
    out: list[Recommendation] = []
    for _cid, (_score, item, media_kind, sources) in ranked:
        rec = _discover_candidate_to_recommendation(
            item, media_kind, "similar to " + ", ".join(sorted(sources)[:2]))
        if not rec.title:
            continue
        if check_item_in_library(
            rec.title, "movie" if rec.item_type == "movie" else "show",
            tmdb_id=rec.tmdb_id, provider_index=provider_index,
        ):
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _requested_provider_keys() -> set[tuple[str, str]]:
    """(identity_source, external_id) of every request ever queued — Discover
    must never re-suggest something already requested/queued, whether or not
    it was ever fulfilled."""
    try:
        import queue_store as qs
        return {
            (req.identity_source, str(req.external_id))
            for req in qs.list_requests(status="all", limit=2000)
            if req.identity_source and req.external_id
        }
    except Exception:
        logger.debug("Request-history lookup failed for Discover exclusion.",
                    exc_info=True)
        return set()


def get_recommendations(
    account_name: str | None, *, genre_filter: str | None = None,
    limit: int = 40,
) -> RecommendationsResult:
    """Two honest sections for one Plex user: 'library' (owned, unwatched,
    genre-matched — including anime-tagged Plex sections) and 'discover'
    (not owned, seeded from recency-weighted watch history + TMDB similar/
    recommendations, falling back to popular-in-genre when history is too
    thin). Never mixed — a title appears in exactly one of the two lists.

    Caveat: per-item viewCount comes from the admin token's perspective;
    Plex doesn't expose other users' watched flags through this API, so
    "unwatched" means unwatched-by-the-server-account.
    """
    names = _account_name_map()
    account_id = next(
        (i for i, n in names.items() if n.casefold() == (account_name or "").casefold()),
        None,
    )
    history = _history_entries(_RECS_HISTORY_DEPTH)
    mine = ([h for h in history if _safe_int(h.get("accountID"), -1) == account_id]
            if account_id is not None else history)
    if not mine:
        # Account filter matched nothing (fresh user / name mismatch) — fall
        # back to everyone's history rather than returning zero genres.
        mine = history

    watched_titles: set[str] = set()
    rating_keys: list[str] = []
    for h in mine:
        watched_titles.add(str(h.get("grandparentTitle") or h.get("title") or "").casefold())
        rk = str(h.get("grandparentRatingKey") or h.get("ratingKey") or "")
        if rk and rk not in rating_keys:
            rating_keys.append(rk)

    genre_count: dict[str, int] = {}
    for rk in rating_keys[:30]:
        try:
            payload = _request_json(f"/library/metadata/{rk}")
        except Exception:
            continue
        for meta in _as_list(_media_container(payload).get("Metadata")):
            if not isinstance(meta, dict):
                continue
            for g in _as_list(meta.get("Genre")):
                if isinstance(g, dict) and g.get("tag"):
                    tag = str(g["tag"])
                    genre_count[tag] = genre_count.get(tag, 0) + 1
    if not genre_count and account_id is not None and mine is not history:
        # Metadata fetches for this user's items all whiffed (Plex busy /
        # transient timeouts) — retry the tally across everyone's history
        # rather than returning an empty genre list.
        for h in history:
            rk = str(h.get("grandparentRatingKey") or h.get("ratingKey") or "")
            if not rk:
                continue
            try:
                payload = _request_json(f"/library/metadata/{rk}")
            except Exception:
                continue
            for meta in _as_list(_media_container(payload).get("Metadata")):
                if isinstance(meta, dict):
                    for g in _as_list(meta.get("Genre")):
                        if isinstance(g, dict) and g.get("tag"):
                            tag = str(g["tag"])
                            genre_count[tag] = genre_count.get(tag, 0) + 1
            if len(genre_count) >= 5:
                break

    top_genres = [g for g, _n in sorted(genre_count.items(), key=lambda kv: -kv[1])][:8]
    wanted = ({genre_filter} if genre_filter else set(top_genres))

    # --- Library (owned, unwatched, genre-matched) + provider index in ONE
    # walk of the sections — the provider index needs every item regardless
    # of watched state (a watched item is still "owned" for Discover to
    # exclude), the library-recs list only wants unwatched genre matches.
    library_recs: list[tuple[int, Recommendation]] = []
    movie_tmdb: set[str] = set()
    movie_imdb: set[str] = set()
    show_tmdb: set[str] = set()
    show_tvdb: set[str] = set()
    show_imdb: set[str] = set()
    for section in _library_sections():
        section_type = str(section.get("type") or "")
        if section_type not in ("movie", "show"):
            continue
        key = str(section.get("key") or "")
        if not key:
            continue
        media_kind = _section_media_kind(section, section_type)
        try:
            items = _section_items(key)
        except Exception:
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            guids = _parse_plex_guids(item)
            if section_type == "movie":
                if guids.get("tmdb"):
                    movie_tmdb.add(guids["tmdb"])
                if guids.get("imdb"):
                    movie_imdb.add(guids["imdb"])
            else:
                if guids.get("tmdb"):
                    show_tmdb.add(guids["tmdb"])
                if guids.get("tvdb"):
                    show_tvdb.add(guids["tvdb"])
                if guids.get("imdb"):
                    show_imdb.add(guids["imdb"])

            if _safe_int(item.get("viewCount")) > 0:
                continue
            title = str(item.get("title") or "")
            if not title or title.casefold() in watched_titles:
                continue
            genres = tuple(
                str(g["tag"]) for g in _as_list(item.get("Genre"))
                if isinstance(g, dict) and g.get("tag")
            )
            overlap = [g for g in genres if g in wanted]
            if not overlap:
                continue
            library_recs.append((len(overlap), Recommendation(
                title=title, year=_safe_int(item.get("year")) or None,
                item_type=media_kind, genres=genres,
                note="because you watch " + ", ".join(overlap[:3]),
                in_library=True, tmdb_id=guids.get("tmdb"))))
    library_recs.sort(key=lambda pair: -pair[0])
    library = [r for _score, r in library_recs[:limit]]

    provider_index = LibraryProviderIndex(
        movie_tmdb_ids=frozenset(movie_tmdb), movie_imdb_ids=frozenset(movie_imdb),
        show_tmdb_ids=frozenset(show_tmdb), show_tvdb_ids=frozenset(show_tvdb),
        show_imdb_ids=frozenset(show_imdb))

    # --- Discover (not owned) ---
    discover: list[Recommendation] = []
    if config.TMDB_API_KEY:
        requested_keys = _requested_provider_keys()
        discover = _discover_from_history(
            mine, watched_titles=watched_titles, provider_index=provider_index,
            requested_keys=requested_keys, limit=limit)
        if len(discover) < _DISCOVER_MIN_SEEDS and (genre_filter or top_genres):
            fallback = _tmdb_discover_recs(
                genre_filter or top_genres[0],
                exclude_titles=watched_titles | {r.title.casefold() for r in discover},
                limit=max(5, limit // 3),
            )
            seen_ids = {r.tmdb_id for r in discover if r.tmdb_id}
            for rec in fallback:
                if rec.tmdb_id and rec.tmdb_id in seen_ids:
                    continue
                if ("tmdb", rec.tmdb_id) in requested_keys if rec.tmdb_id else False:
                    continue
                if check_item_in_library(
                    rec.title, "movie" if rec.item_type == "movie" else "show",
                    tmdb_id=rec.tmdb_id, provider_index=provider_index,
                ):
                    continue
                discover.append(rec)

    return RecommendationsResult(
        top_genres=top_genres, library=library, discover=discover,
        generated_at=time.time())


# TMDB genre-name → id. Movies and TV use DIFFERENT id sets for several
# genres (TMDB merges some into combo genres on the TV side), so discover
# queries each endpoint with its own map; unknown names skip that endpoint.
_TMDB_GENRE_IDS = {
    "action": 28, "adventure": 12, "animation": 16, "comedy": 35, "crime": 80,
    "documentary": 99, "drama": 18, "family": 10751, "fantasy": 14,
    "history": 36, "horror": 27, "music": 10402, "mystery": 9648,
    "romance": 10749, "science fiction": 878, "sci-fi": 878, "thriller": 53,
    "war": 10752, "western": 37,
}
_TMDB_TV_GENRE_IDS = {
    "action": 10759, "adventure": 10759, "animation": 16, "anime": 16,
    "comedy": 35, "crime": 80, "documentary": 99, "drama": 18,
    "family": 10751, "fantasy": 10765, "kids": 10762, "mystery": 9648,
    "science fiction": 10765, "sci-fi": 10765, "war": 10768,
    "western": 37, "reality": 10764, "soap": 10766, "talk": 10767,
}


def _tmdb_discover_recs(genre_name: str, *, exclude_titles: set[str],
                        limit: int) -> list[Recommendation]:
    """Popular TMDB movies AND shows in a genre that aren't in the library.

    Both endpoints run — "Animation shows not in library" used to return
    nothing because only /discover/movie was queried and the Type filter
    then dropped every result."""
    import json as _json
    import urllib.request as _rq

    plans = []
    movie_id = _TMDB_GENRE_IDS.get(genre_name.casefold())
    if movie_id is not None:
        plans.append(("movie", "discover/movie", movie_id, "title", "release_date"))
    tv_id = _TMDB_TV_GENRE_IDS.get(genre_name.casefold())
    if tv_id is not None:
        plans.append(("show", "discover/tv", tv_id, "name", "first_air_date"))

    out: list[Recommendation] = []
    per_type = max(3, limit // 2) if len(plans) > 1 else limit
    for item_type, endpoint, genre_id, title_key, date_key in plans:
        try:
            req = _rq.Request(
                f"https://api.themoviedb.org/3/{endpoint}"
                f"?api_key={config.TMDB_API_KEY}&with_genres={genre_id}"
                "&sort_by=popularity.desc&vote_count.gte=200",
                headers={"Accept": "application/json"},
            )
            with _rq.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            logger.debug("TMDB discover failed for %s.", endpoint, exc_info=True)
            continue
        added = 0
        for item in data.get("results", []):
            title = str(item.get(title_key) or "")
            if not title or title.casefold() in exclude_titles:
                continue
            year = None
            if item.get(date_key):
                year = _safe_int(str(item[date_key])[:4]) or None
            out.append(Recommendation(
                title=title, year=year, item_type=item_type,
                genres=(genre_name,),
                note=f"popular {genre_name}",
                in_library=False,
                tmdb_id=str(item.get("id") or "") or None,
            ))
            added += 1
            if added >= per_type:
                break
    return out


def _library_sections() -> list[dict[str, Any]]:
    payload = _request_json("/library/sections")
    return [
        item
        for item in _as_list(_media_container(payload).get("Directory"))
        if isinstance(item, dict)
    ]


def _section_item_count(section_key: str) -> int:
    payload = _request_json(
        f"/library/sections/{section_key}/all",
        extra_headers={
            "X-Plex-Container-Start": "0",
            "X-Plex-Container-Size": "0",
        },
    )
    container = _media_container(payload)
    for key in ("totalSize", "size"):
        value = container.get(key)
        if value is not None:
            return _safe_int(value)
    metadata = _as_list(container.get("Metadata"))
    return len(metadata)


def _section_items(section_key: str, *, item_type: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = 0
    page_size = 200

    while True:
        query = {"type": item_type} if item_type is not None else None
        payload = _request_json(
            f"/library/sections/{section_key}/all",
            query=query,
            extra_headers={
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(page_size),
            },
        )
        container = _media_container(payload)
        batch = [
            item
            for item in _as_list(container.get("Metadata"))
            if isinstance(item, dict)
        ]
        if not batch:
            break

        items.extend(batch)
        total_size = _safe_int(container.get("totalSize"), default=len(items))
        start += len(batch)
        if start >= total_size or len(batch) < page_size:
            break

    return items


def get_plex_library_sections() -> list[PlexLibrarySection]:
    if not plex_metrics_enabled():
        return []

    sections = _library_sections()
    results: list[PlexLibrarySection] = []
    for section in sections:
        section_key = str(section.get("key") or "").strip()
        if not section_key:
            continue

        title = str(section.get("title") or f"Section {section_key}")
        section_type = str(section.get("type") or "unknown")
        hidden = bool(_safe_int(section.get("hidden")))
        locations = [
            str(location.get("path"))
            for location in _as_list(section.get("Location"))
            if isinstance(location, dict) and location.get("path")
        ]
        try:
            item_count = _section_item_count(section_key)
        except RuntimeError:
            item_count = -1

        results.append(
            PlexLibrarySection(
                title=title,
                section_type=section_type,
                item_count=item_count,
                locations=locations,
                hidden=hidden,
            )
        )

    return results


def search_plex_library(query: str, *, limit: int = 25) -> list[PlexLibrarySearchResult]:
    clean_query = " ".join(query.split())
    if not clean_query:
        return []
    if not plex_metrics_enabled():
        return []

    payload = _request_json(
        "/hubs/search",
        query={
            "query": clean_query,
            "includeExternalMedia": "0",
            "limit": str(limit),
        },
    )
    hubs = [
        hub
        for hub in _as_list(_media_container(payload).get("Hub"))
        if isinstance(hub, dict)
    ]

    results: list[PlexLibrarySearchResult] = []
    seen: set[str] = set()
    for hub in hubs:
        for item in _as_list(hub.get("Metadata")):
            if not isinstance(item, dict):
                continue

            rating_key = str(item.get("ratingKey") or "").strip()
            section_title = str(item.get("librarySectionTitle") or "").strip()
            if not rating_key or not section_title or rating_key in seen:
                continue

            part_file = ""
            size_bytes = 0
            for media in _as_list(item.get("Media")):
                if not isinstance(media, dict):
                    continue
                for part in _as_list(media.get("Part")):
                    if not isinstance(part, dict):
                        continue
                    if not part_file and part.get("file"):
                        part_file = str(part.get("file"))
                    if part.get("size") is not None:
                        size_bytes = _safe_int(part.get("size"), default=size_bytes)
                    if part_file:
                        break
                if part_file:
                    break

            results.append(
                PlexLibrarySearchResult(
                    name=_playback_title(item),
                    path=part_file or str(item.get("key") or ""),
                    section_title=section_title,
                    media_type=str(item.get("type") or "unknown"),
                    size_bytes=size_bytes,
                    added_at=_safe_int(item.get("addedAt")),
                )
            )
            seen.add(rating_key)
            if len(results) >= limit:
                return results

    return results


def get_plex_library_inventory_metrics() -> PlexLibraryInventoryMetrics:
    if not plex_metrics_enabled():
        return PlexLibraryInventoryMetrics(
            configured=False,
            connected=False,
            total_movies=0,
            total_shows=0,
            total_episodes=0,
            total_runtime_ms=0,
            recent_7d=0,
            recent_30d=0,
            sections=[],
            error="Set PLEX_SERVER_URL and PLEX_TOKEN in .env to enable Plex library metrics.",
        )

    try:
        sections = _library_sections()
    except RuntimeError as exc:
        return PlexLibraryInventoryMetrics(
            configured=True,
            connected=False,
            total_movies=0,
            total_shows=0,
            total_episodes=0,
            total_runtime_ms=0,
            recent_7d=0,
            recent_30d=0,
            sections=[],
            error=str(exc),
        )

    now = int(time.time())
    cutoff_7d = now - (7 * 24 * 60 * 60)
    cutoff_30d = now - (30 * 24 * 60 * 60)

    metrics: list[PlexSectionInventoryMetrics] = []
    total_movies = 0
    total_shows = 0
    total_episodes = 0
    total_runtime_ms = 0
    total_recent_7d = 0
    total_recent_30d = 0

    try:
        for section in sections:
            section_key = str(section.get("key") or "").strip()
            if not section_key:
                continue

            title = str(section.get("title") or f"Section {section_key}")
            section_type = str(section.get("type") or "unknown")
            hidden = bool(_safe_int(section.get("hidden")))
            locations = [
                str(location.get("path"))
                for location in _as_list(section.get("Location"))
                if isinstance(location, dict) and location.get("path")
            ]

            top_items = _section_items(section_key)
            episode_items = _section_items(section_key, item_type="4") if section_type == "show" else []

            if section_type == "movie":
                movie_count = len(top_items)
                show_count = 0
                episode_count = 0
                runtime_source = top_items
                recent_source = top_items
                item_count = movie_count
            elif section_type == "show":
                movie_count = 0
                show_count = len(top_items)
                episode_count = len(episode_items)
                runtime_source = episode_items
                recent_source = episode_items or top_items
                item_count = show_count
            else:
                movie_count = sum(1 for item in top_items if str(item.get("type") or "") == "movie")
                show_count = sum(1 for item in top_items if str(item.get("type") or "") == "show")
                episode_count = sum(1 for item in top_items if str(item.get("type") or "") == "episode")
                runtime_source = top_items
                recent_source = top_items
                item_count = len(top_items)

            total_duration_ms_section = sum(
                _safe_int(item.get("duration"))
                for item in runtime_source
            )
            recent_7d = sum(
                1 for item in recent_source if _safe_int(item.get("addedAt")) >= cutoff_7d
            )
            recent_30d = sum(
                1 for item in recent_source if _safe_int(item.get("addedAt")) >= cutoff_30d
            )
            recent_titles = [
                _playback_title(item)
                for item in sorted(
                    recent_source,
                    key=lambda item: _safe_int(item.get("addedAt")),
                    reverse=True,
                )[:3]
            ]

            metrics.append(
                PlexSectionInventoryMetrics(
                    title=title,
                    section_type=section_type,
                    item_count=item_count,
                    movie_count=movie_count,
                    show_count=show_count,
                    episode_count=episode_count,
                    total_duration_ms=total_duration_ms_section,
                    recent_7d=recent_7d,
                    recent_30d=recent_30d,
                    locations=locations,
                    recent_titles=recent_titles,
                    hidden=hidden,
                )
            )

            total_movies += movie_count
            total_shows += show_count
            total_episodes += episode_count
            total_runtime_ms += total_duration_ms_section
            total_recent_7d += recent_7d
            total_recent_30d += recent_30d
    except RuntimeError as exc:
        return PlexLibraryInventoryMetrics(
            configured=True,
            connected=False,
            total_movies=0,
            total_shows=0,
            total_episodes=0,
            total_runtime_ms=0,
            recent_7d=0,
            recent_30d=0,
            sections=[],
            error=str(exc),
        )

    metrics.sort(key=lambda item: (item.hidden, item.title.casefold()))
    return PlexLibraryInventoryMetrics(
        configured=True,
        connected=True,
        total_movies=total_movies,
        total_shows=total_shows,
        total_episodes=total_episodes,
        total_runtime_ms=total_runtime_ms,
        recent_7d=total_recent_7d,
        recent_30d=total_recent_30d,
        sections=metrics,
    )


def get_plex_metrics() -> PlexMetrics:
    if not plex_metrics_enabled():
        return PlexMetrics(
            configured=False,
            connected=False,
            server_name="",
            current_sessions=0,
            active_users=0,
            history_items=0,
            plays_7d=0,
            plays_30d=0,
            watch_time_7d_ms=0,
            watch_time_30d_ms=0,
            top_users_30d=[],
            current_session_details=[],
            recent_plays=[],
            section_counts=[],
            error="Set PLEX_SERVER_URL and PLEX_TOKEN in .env to enable Plex usage metrics.",
        )

    try:
        root_payload = _request_json("/")
        sessions_payload = _request_json("/status/sessions")
        history_entries = _history_entries(config.PLEX_HISTORY_FETCH_LIMIT)
        sections = _library_sections()
        account_names = _account_name_map()
    except RuntimeError as exc:
        return PlexMetrics(
            configured=True,
            connected=False,
            server_name="",
            current_sessions=0,
            active_users=0,
            history_items=0,
            plays_7d=0,
            plays_30d=0,
            watch_time_7d_ms=0,
            watch_time_30d_ms=0,
            top_users_30d=[],
            current_session_details=[],
            recent_plays=[],
            section_counts=[],
            error=str(exc),
        )

    server_name = str(_media_container(root_payload).get("friendlyName") or "Plex Server")
    session_items = [
        item
        for item in _as_list(_media_container(sessions_payload).get("Metadata"))
        if isinstance(item, dict)
    ]

    current_session_details = [
        PlexSessionMetrics(
            user_label=_session_user_label(item, account_names=account_names),
            title=_playback_title(item),
            media_type=str(item.get("type") or "unknown"),
            state=str(item.get("Player", {}).get("state") or item.get("state") or "unknown"),
            player=_player_label(item),
        )
        for item in session_items
    ]

    active_users = len({session.user_label for session in current_session_details})

    now = int(time.time())
    cutoff_7d = now - (7 * 24 * 60 * 60)
    cutoff_30d = now - (30 * 24 * 60 * 60)
    plays_7d = 0
    plays_30d = 0
    watch_time_7d_ms = 0
    watch_time_30d_ms = 0
    user_rollup: dict[str, tuple[int, int]] = {}
    recent_plays: list[PlexRecentPlay] = []

    for item in history_entries:
        viewed_at = _safe_int(item.get("viewedAt"))
        duration_ms = _safe_int(item.get("duration"))
        user_label = _session_user_label(item, account_names=account_names)
        title = _playback_title(item)
        media_type = str(item.get("type") or "unknown")

        if viewed_at >= cutoff_30d:
            plays_30d += 1
            watch_time_30d_ms += duration_ms
            prior_count, prior_duration = user_rollup.get(user_label, (0, 0))
            user_rollup[user_label] = (prior_count + 1, prior_duration + duration_ms)

        if viewed_at >= cutoff_7d:
            plays_7d += 1
            watch_time_7d_ms += duration_ms

        if viewed_at >= cutoff_30d:
            recent_plays.append(
                PlexRecentPlay(
                    viewed_at=viewed_at,
                    user_label=user_label,
                    title=title,
                    media_type=media_type,
                )
            )

    top_users = sorted(
        (
            PlexUserMetrics(label=label, play_count=count, watch_time_ms=watch_ms)
            for label, (count, watch_ms) in user_rollup.items()
        ),
        key=lambda item: (-item.play_count, -item.watch_time_ms, item.label),
    )[:5]

    recent_plays.sort(key=lambda item: item.viewed_at, reverse=True)
    section_counts: list[tuple[str, str, int]] = []
    for section in sections:
        section_key = str(section.get("key") or "").strip()
        if not section_key:
            continue
        title = str(section.get("title") or f"Section {section_key}")
        section_type = str(section.get("type") or "unknown")
        try:
            item_count = _section_item_count(section_key)
        except RuntimeError:
            item_count = -1
        section_counts.append((title, section_type, item_count))

    return PlexMetrics(
        configured=True,
        connected=True,
        server_name=server_name,
        current_sessions=len(current_session_details),
        active_users=active_users,
        history_items=len(history_entries),
        plays_7d=plays_7d,
        plays_30d=plays_30d,
        watch_time_7d_ms=watch_time_7d_ms,
        watch_time_30d_ms=watch_time_30d_ms,
        top_users_30d=top_users,
        current_session_details=current_session_details[:5],
        recent_plays=recent_plays[:5],
        section_counts=section_counts,
    )


def format_plex_metrics_message() -> str:
    metrics = get_plex_metrics()
    if not metrics.configured:
        return f"Plex Usage Metrics\n{metrics.error}"
    if not metrics.connected:
        return f"Plex Usage Metrics\nUnavailable: {metrics.error}"

    lines = [
        "Plex Usage Metrics",
        f"- Server: {metrics.server_name}",
        f"- Current streams: {metrics.current_sessions}",
        f"- Active users right now: {metrics.active_users}",
        f"- History items analyzed: {metrics.history_items}",
        f"- Plays in last 7 days: {metrics.plays_7d}",
        f"- Plays in last 30 days: {metrics.plays_30d}",
        f"- Estimated watch time in last 7 days: {_format_duration_ms(metrics.watch_time_7d_ms)}",
        f"- Estimated watch time in last 30 days: {_format_duration_ms(metrics.watch_time_30d_ms)}",
    ]

    if metrics.current_session_details:
        lines.append("")
        lines.append("Current sessions:")
        for session in metrics.current_session_details:
            lines.append(
                f"- {session.user_label}: {session.title} "
                f"[{session.state} on {session.player}]"
            )

    if metrics.top_users_30d:
        lines.append("")
        lines.append("Top users in last 30 days:")
        for user in metrics.top_users_30d:
            lines.append(
                f"- {user.label}: {user.play_count} play(s), "
                f"{_format_duration_ms(user.watch_time_ms)} watched"
            )

    if metrics.recent_plays:
        lines.append("")
        lines.append("Recent plays in last 30 days:")
        for play in metrics.recent_plays:
            timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(play.viewed_at))
            lines.append(f"- {timestamp}: {play.user_label} watched {play.title}")

    if metrics.section_counts:
        lines.append("")
        lines.append("Plex library sections:")
        for title, section_type, item_count in metrics.section_counts:
            count_label = "unavailable" if item_count < 0 else f"{item_count} item(s)"
            lines.append(f"- {title} [{section_type}]: {count_label}")

    return "\n".join(lines)


def format_plex_library_inventory_message() -> str:
    inventory = get_plex_library_inventory_metrics()
    if not inventory.configured:
        return f"Plex Library Metrics\n{inventory.error}"
    if not inventory.connected:
        return f"Plex Library Metrics\nUnavailable: {inventory.error}"

    lines = [
        "Plex Library Metrics",
        f"- Movies: {inventory.total_movies}",
        f"- Shows: {inventory.total_shows}",
        f"- Episodes: {inventory.total_episodes}",
        f"- Total runtime: {_format_duration_ms(inventory.total_runtime_ms)}",
        f"- Added in last 7 days: {inventory.recent_7d}",
        f"- Added in last 30 days: {inventory.recent_30d}",
    ]

    visible_sections = [section for section in inventory.sections if not section.hidden]
    hidden_sections = [section for section in inventory.sections if section.hidden]

    if visible_sections:
        lines.append("")
        lines.append("Per-section totals:")
        for section in visible_sections:
            parts = [f"{section.title} [{section.section_type}]"]
            if section.movie_count:
                parts.append(f"{section.movie_count} movie(s)")
            if section.show_count:
                parts.append(f"{section.show_count} show(s)")
            if section.episode_count:
                parts.append(f"{section.episode_count} episode(s)")
            parts.append(f"{_format_duration_ms(section.total_duration_ms)} total runtime")
            parts.append(f"{section.recent_7d} added in 7d")
            lines.append("- " + ", ".join(parts))
            if section.recent_titles:
                lines.append("  Recent additions: " + " | ".join(section.recent_titles))

    if hidden_sections:
        lines.append("")
        lines.append("Hidden sections:")
        for section in hidden_sections:
            parts = [f"{section.title} [{section.section_type}]"]
            if section.movie_count:
                parts.append(f"{section.movie_count} movie(s)")
            if section.show_count:
                parts.append(f"{section.show_count} show(s)")
            if section.episode_count:
                parts.append(f"{section.episode_count} episode(s)")
            parts.append(f"{_format_duration_ms(section.total_duration_ms)} total runtime")
            lines.append("- " + ", ".join(parts))

    return "\n".join(lines)
