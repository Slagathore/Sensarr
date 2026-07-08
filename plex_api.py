import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
        out.append(WatchlistItem(
            title=str(item.get("title") or "Unknown"),
            year=_safe_int(item.get("year")) or None,
            item_type=str(item.get("type") or "movie"),
        ))
    return out


# ---------------------------------------------------------------------------
# Recommendations — genre affinity from one user's watch history
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Recommendation:
    title: str
    year: int | None
    item_type: str          # "movie" | "show"
    genres: tuple[str, ...]
    note: str               # "because you watch Action, Sci-Fi"
    in_library: bool


def get_recommendations(
    account_name: str | None, *, in_library_only: bool = True,
    genre_filter: str | None = None, limit: int = 40,
) -> tuple[list[str], list[Recommendation]]:
    """(top_genres, recommendations) for one Plex user.

    Tallies genres from the user's recent watch history, then surfaces
    UNWATCHED library items in those genres. With in_library_only=False,
    TMDB discover adds popular titles you don't have yet (TMDB key needed).

    Caveat: per-item viewCount comes from the admin token's perspective;
    Plex doesn't expose other users' watched flags through this API, so
    "unwatched" means unwatched-by-the-server-account.
    """
    names = _account_name_map()
    account_id = next(
        (i for i, n in names.items() if n.casefold() == (account_name or "").casefold()),
        None,
    )
    history = _history_entries(300)
    mine = ([h for h in history if _safe_int(h.get("accountID"), -1) == account_id]
            if account_id is not None else history)

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
    top_genres = [g for g, _n in sorted(genre_count.items(), key=lambda kv: -kv[1])][:8]
    wanted = ({genre_filter} if genre_filter else set(top_genres))

    recs: list[tuple[int, Recommendation]] = []
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
            recs.append((len(overlap), Recommendation(
                title=title, year=_safe_int(item.get("year")) or None,
                item_type=section_type, genres=genres,
                note="because you watch " + ", ".join(overlap[:3]),
                in_library=True,
            )))
    recs.sort(key=lambda pair: -pair[0])
    results = [r for _score, r in recs[:limit]]

    if not in_library_only and config.TMDB_API_KEY and (genre_filter or top_genres):
        results.extend(_tmdb_discover_recs(
            genre_filter or top_genres[0],
            exclude_titles=watched_titles | {r.title.casefold() for r in results},
            limit=max(5, limit // 3),
        ))
    return top_genres, results


# TMDB genre-name → id (movie discover). Only common ones; unknown names skip.
_TMDB_GENRE_IDS = {
    "action": 28, "adventure": 12, "animation": 16, "comedy": 35, "crime": 80,
    "documentary": 99, "drama": 18, "family": 10751, "fantasy": 14,
    "history": 36, "horror": 27, "music": 10402, "mystery": 9648,
    "romance": 10749, "science fiction": 878, "sci-fi": 878, "thriller": 53,
    "war": 10752, "western": 37,
}


def _tmdb_discover_recs(genre_name: str, *, exclude_titles: set[str],
                        limit: int) -> list[Recommendation]:
    """Popular TMDB movies in a genre that aren't in the library yet."""
    import json as _json
    import urllib.request as _rq
    genre_id = _TMDB_GENRE_IDS.get(genre_name.casefold())
    if genre_id is None:
        return []
    try:
        req = _rq.Request(
            "https://api.themoviedb.org/3/discover/movie"
            f"?api_key={config.TMDB_API_KEY}&with_genres={genre_id}"
            "&sort_by=popularity.desc&vote_count.gte=500",
            headers={"Accept": "application/json"},
        )
        with _rq.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        logger.debug("TMDB discover failed.", exc_info=True)
        return []
    out: list[Recommendation] = []
    for item in data.get("results", []):
        title = str(item.get("title") or "")
        if not title or title.casefold() in exclude_titles:
            continue
        year = None
        if item.get("release_date"):
            year = _safe_int(str(item["release_date"])[:4]) or None
        out.append(Recommendation(
            title=title, year=year, item_type="movie",
            genres=(genre_name,), note=f"popular {genre_name} (not in library)",
            in_library=False,
        ))
        if len(out) >= limit:
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
