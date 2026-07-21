# =============================================================================
# Task C (fix sprint) — recommendations that actually recommend.
#
# Covers: the two honest sections (library/discover, never mixed), provider-
# id-first presence checks replacing BOTH old substring checks (plex_api's
# TMDB-owned relabelling and watchlist_tab's "In Library" column), anime
# library sections getting their own item_type instead of being lumped into
# movie/show, recency-weighted discover seeding blended with TMDB vote
# quality, exclusion of watched/owned/already-requested titles, the
# popular-in-genre fallback when history is too thin, and the cache
# freshness stamp.
#
# No test in this file touches the network — the autouse socket guard in
# conftest.py would fail any test that tried; every TMDB/Plex seam is
# monkeypatched instead, per project convention.
# =============================================================================

import queue_store
import db

import plex_api
import watchlist_tab
from plex_api import (
    LibraryProviderIndex,
    Recommendation,
    RecommendationsResult,
    WatchlistItem,
    _discover_from_history,
    _history_seed_weights,
    _section_media_kind,
    _tmdb_quality_score,
    build_library_provider_index,
    check_item_in_library,
    get_recommendations,
    recs_cache_is_stale,
)


def _clean():
    queue_store.initialize_queue_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM requests")
        conn.commit()


import pytest


@pytest.fixture(autouse=True)
def _fresh():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------------------
# _history_seed_weights — recency-weighted most-watched seeds
# ---------------------------------------------------------------------------

def test_history_seeds_favor_recent_over_stale_watches():
    now = 1_700_000_000.0
    day = 86400.0
    history = [
        # "Show A": 2 recent watches (episodes -> grandparentRatingKey).
        {"type": "episode", "grandparentRatingKey": "10", "grandparentTitle": "Show A",
         "viewedAt": now - 1 * day},
        {"type": "episode", "grandparentRatingKey": "10", "grandparentTitle": "Show A",
         "viewedAt": now - 2 * day},
        # "Movie B": 2 watches a year ago (should score much lower).
        {"type": "movie", "ratingKey": "20", "title": "Movie B",
         "viewedAt": now - 365 * day},
        {"type": "movie", "ratingKey": "20", "title": "Movie B",
         "viewedAt": now - 360 * day},
    ]
    seeds = _history_seed_weights(history, now=now)
    assert seeds[0]["rating_key"] == "10"
    assert seeds[0]["title"] == "Show A"
    assert seeds[0]["item_type"] == "show"
    assert seeds[0]["score"] > seeds[1]["score"]
    assert seeds[1]["rating_key"] == "20"
    assert seeds[1]["item_type"] == "movie"


def test_history_seeds_missing_viewed_at_treated_as_old_not_now():
    now = 1_700_000_000.0
    history_with_ts = [
        {"type": "movie", "ratingKey": "1", "title": "Timestamped",
         "viewedAt": now - 86400},
    ]
    history_no_ts = [
        {"type": "movie", "ratingKey": "2", "title": "No Timestamp"},
    ]
    seeded = _history_seed_weights(history_with_ts, now=now)
    seedless = _history_seed_weights(history_no_ts, now=now)
    # A malformed/missing-timestamp entry must not outrank a real recent one.
    assert seeded[0]["score"] > seedless[0]["score"]


def test_history_seeds_respects_limit():
    now = 1_700_000_000.0
    history = [
        {"type": "movie", "ratingKey": str(i), "title": f"Movie {i}",
         "viewedAt": now - i * 86400}
        for i in range(20)
    ]
    seeds = _history_seed_weights(history, now=now, limit=5)
    assert len(seeds) == 5


# ---------------------------------------------------------------------------
# _tmdb_quality_score — pure blend of rating + confidence
# ---------------------------------------------------------------------------

def test_quality_score_prefers_high_confidence_over_thin_sample():
    thin_high_rating = _tmdb_quality_score(9.0, 3)
    broad_medium_rating = _tmdb_quality_score(7.5, 5000)
    assert broad_medium_rating > thin_high_rating


def test_quality_score_handles_missing_values():
    assert _tmdb_quality_score(None, None) == 0.0
    assert _tmdb_quality_score(0, 0) == 0.0
    assert _tmdb_quality_score(8.0, 100) > 0.0


# ---------------------------------------------------------------------------
# recs_cache_is_stale — freshness stamp / TTL
# ---------------------------------------------------------------------------

def test_recs_cache_staleness():
    now = 1_700_000_000.0
    assert recs_cache_is_stale(0, ttl_hours=12, now=now) is True
    assert recs_cache_is_stale(now - 3600, ttl_hours=12, now=now) is False
    assert recs_cache_is_stale(now - 13 * 3600, ttl_hours=12, now=now) is True


# ---------------------------------------------------------------------------
# _section_media_kind — anime-tagged Plex sections get their own item_type
# ---------------------------------------------------------------------------

def test_section_media_kind_matches_configured_anime_path(monkeypatch, tmp_path):
    anime_root = tmp_path / "Anime"
    anime_root.mkdir()
    from config import MediaLibraryPath
    monkeypatch.setattr(
        plex_api.config, "MEDIA_LIBRARY_PATHS",
        [MediaLibraryPath(path=str(anime_root), media_type="anime")])
    section = {"type": "show", "Location": [{"path": str(anime_root)}]}
    assert _section_media_kind(section, "show") == "anime"


def test_section_media_kind_falls_back_to_plex_type_when_unmatched(monkeypatch, tmp_path):
    other_root = tmp_path / "TV"
    other_root.mkdir()
    from config import MediaLibraryPath
    monkeypatch.setattr(plex_api.config, "MEDIA_LIBRARY_PATHS",
                        [MediaLibraryPath(path=str(other_root), media_type="tv")])
    unrelated = tmp_path / "Movies"
    unrelated.mkdir()
    section = {"type": "movie", "Location": [{"path": str(unrelated)}]}
    assert _section_media_kind(section, "movie") == "movie"


def test_section_media_kind_no_location_falls_back():
    assert _section_media_kind({"type": "show"}, "show") == "show"


# ---------------------------------------------------------------------------
# check_item_in_library — provider id FIRST, identity-aware title SECOND,
# filename substrings NEVER (the two weak spots this replaces).
# ---------------------------------------------------------------------------

def test_check_item_in_library_provider_id_short_circuits(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("title fallback must not run when a provider id hits")
    monkeypatch.setattr("media_lookup.check_library_for_title", _boom)
    idx = LibraryProviderIndex(movie_tmdb_ids=frozenset({"27205"}))
    assert check_item_in_library("Inception", "movie", tmdb_id="27205",
                                 provider_index=idx) is True


def test_check_item_in_library_falls_back_to_identity_aware_title_match(monkeypatch):
    calls = []

    def _fake(title, media_type, **k):
        calls.append((title, media_type))
        return (title == "Inception", ["Inception (2010)"])
    monkeypatch.setattr("media_lookup.check_library_for_title", _fake)
    idx = LibraryProviderIndex()  # no provider ids known
    assert check_item_in_library("Inception", "movie", provider_index=idx) is True
    assert calls == [("Inception", "movie")]


def test_check_item_in_library_never_matches_a_sequel_by_substring(monkeypatch):
    # THE regression this replaces: a naive substring check would say "Dune"
    # is "in library" just because "Dune Part Two" is on disk. The real
    # check_library_for_title applies media_identity's sequel guard and must
    # say no.
    monkeypatch.setattr(
        "library_index.search_library",
        lambda *a, **k: [_Entry("Dune Part Two (2024)")])
    assert check_item_in_library("Dune", "movie", provider_index=None) is False


def test_check_item_in_library_no_match_anywhere_is_false(monkeypatch):
    monkeypatch.setattr("media_lookup.check_library_for_title",
                        lambda *a, **k: (False, []))
    assert check_item_in_library("Nothing Owns This", "movie",
                                 provider_index=LibraryProviderIndex()) is False


class _Entry:
    def __init__(self, name):
        self.name = name
        self.path = name


# ---------------------------------------------------------------------------
# build_library_provider_index — provider ids read off Plex Guid, not names
# ---------------------------------------------------------------------------

def test_build_library_provider_index_splits_movie_and_show_namespaces(monkeypatch):
    sections = [
        {"type": "movie", "key": "1"},
        {"type": "show", "key": "2"},
        {"type": "artist", "key": "3"},  # never walked — not movie/show
    ]
    monkeypatch.setattr(plex_api, "_library_sections", lambda: sections)

    def _items(key):
        if key == "1":
            return [{"Guid": [{"id": "tmdb://100"}, {"id": "imdb://tt1"}]}]
        if key == "2":
            return [{"Guid": [{"id": "tvdb://200"}, {"id": "tmdb://300"}]}]
        raise AssertionError("artist section must never be walked")
    monkeypatch.setattr(plex_api, "_section_items", _items)

    idx = build_library_provider_index()
    assert idx.movie_tmdb_ids == frozenset({"100"})
    assert idx.movie_imdb_ids == frozenset({"tt1"})
    assert idx.show_tvdb_ids == frozenset({"200"})
    assert idx.show_tmdb_ids == frozenset({"300"})


# ---------------------------------------------------------------------------
# _discover_from_history — seeding, blending, exclusions, thin-history bail
# ---------------------------------------------------------------------------

def _seed_history(now, titles):
    """N distinct watched titles, most-recent first (so seed ranking is
    deterministic: titles[0] is the strongest seed)."""
    day = 86400.0
    return [
        {"type": "movie", "ratingKey": str(i), "title": t, "viewedAt": now - i * day}
        for i, t in enumerate(titles)
    ]


def test_discover_from_history_returns_empty_when_seeds_too_thin():
    now = 1_700_000_000.0
    history = _seed_history(now, ["Only One", "Only Two"])  # < _DISCOVER_MIN_SEEDS
    out = _discover_from_history(
        history, watched_titles=set(), provider_index=LibraryProviderIndex(),
        requested_keys=set(), limit=10, now=now)
    assert out == []


def test_discover_from_history_blends_seeds_excludes_watched_owned_requested(monkeypatch):
    now = 1_700_000_000.0
    history = _seed_history(now, ["Seed A", "Seed B", "Seed C"])

    monkeypatch.setattr(plex_api, "_seed_provider_id",
                        lambda rk: ("tmdb", f"seed-{rk}"))

    def _related(media_kind, tmdb_id):
        if tmdb_id == "seed-0":
            return [
                {"id": "900", "title": "Fresh Pick", "vote_average": 8.0,
                 "vote_count": 500},
                {"id": "901", "title": "Already Watched This", "vote_average": 9.0,
                 "vote_count": 1000},
                {"id": "902", "title": "Owned Already", "vote_average": 7.0,
                 "vote_count": 300},
                {"id": "903", "title": "Already Requested", "vote_average": 6.0,
                 "vote_count": 300},
            ]
        return []
    monkeypatch.setattr(plex_api, "_tmdb_related", _related)
    monkeypatch.setattr(plex_api, "check_item_in_library",
                        lambda title, kind, **k: title == "Owned Already")

    out = _discover_from_history(
        history, watched_titles={"already watched this"},
        provider_index=LibraryProviderIndex(),
        requested_keys={("tmdb", "903")}, limit=10, now=now)

    titles = {r.title for r in out}
    assert titles == {"Fresh Pick"}
    assert out[0].tmdb_id == "900"
    assert out[0].in_library is False


def test_discover_candidate_tagged_anime_by_language_and_genre(monkeypatch):
    now = 1_700_000_000.0
    history = _seed_history(now, ["Seed A", "Seed B", "Seed C"])
    monkeypatch.setattr(plex_api, "_seed_provider_id", lambda rk: ("tmdb", rk))

    def _related(media_kind, tmdb_id):
        if tmdb_id == "0":
            return [{"id": "1", "name": "Some Anime Show", "vote_average": 8.0,
                     "vote_count": 100, "original_language": "ja",
                     "genre_ids": [16]}]
        return []
    monkeypatch.setattr(plex_api, "_tmdb_related", _related)

    out = _discover_from_history(
        history, watched_titles=set(), provider_index=LibraryProviderIndex(),
        requested_keys=set(), limit=10, now=now)
    assert len(out) == 1
    assert out[0].item_type == "anime"


# ---------------------------------------------------------------------------
# get_recommendations — the full two-section orchestration
# ---------------------------------------------------------------------------

def _plex_meta(title, *, tmdb=None, tvdb=None, genres=(), view_count=0, year=None):
    guid = []
    if tmdb:
        guid.append({"id": f"tmdb://{tmdb}"})
    if tvdb:
        guid.append({"id": f"tvdb://{tvdb}"})
    return {
        "title": title, "year": year, "viewCount": view_count,
        "Genre": [{"tag": g} for g in genres], "Guid": guid,
    }


def test_get_recommendations_never_mixes_library_and_discover(monkeypatch):
    monkeypatch.setattr(plex_api.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(plex_api, "_account_name_map", lambda: {1: "Cole"})
    now = 1_700_000_000.0
    history = [
        {"type": "movie", "ratingKey": "0", "title": "Seed A", "accountID": 1,
         "viewedAt": now, "grandparentTitle": None},
        {"type": "movie", "ratingKey": "1", "title": "Seed B", "accountID": 1,
         "viewedAt": now - 86400},
        {"type": "movie", "ratingKey": "2", "title": "Seed C", "accountID": 1,
         "viewedAt": now - 2 * 86400},
    ]
    monkeypatch.setattr(plex_api, "_history_entries", lambda limit: history)
    monkeypatch.setattr(plex_api, "_request_json", lambda path, **k: {"MediaContainer": {}})

    sections = [{"type": "movie", "key": "m1", "Location": [{"path": "/lib/Movies"}]}]
    monkeypatch.setattr(plex_api, "_library_sections", lambda: sections)
    monkeypatch.setattr(
        plex_api, "_section_items",
        lambda key: [_plex_meta("Owned Unwatched Action", tmdb="500",
                                genres=("Action",), view_count=0, year=2020)])
    monkeypatch.setattr(plex_api, "_seed_provider_id", lambda rk: ("tmdb", rk))

    def _related(media_kind, tmdb_id):
        if tmdb_id == "0":
            return [{"id": "500", "title": "Owned Unwatched Action",
                     "vote_average": 8.0, "vote_count": 300,
                     "genre_ids": [28]}]
        return []
    monkeypatch.setattr(plex_api, "_tmdb_related", _related)
    monkeypatch.setattr(plex_api, "_requested_provider_keys", lambda: set())

    result = get_recommendations("Cole", genre_filter="Action")

    assert isinstance(result, RecommendationsResult)
    lib_titles = {r.title for r in result.library}
    disc_titles = {r.title for r in result.discover}
    assert "Owned Unwatched Action" in lib_titles
    # The TMDB candidate resolves to the SAME owned title -> excluded from
    # discover, never mixed into both sections.
    assert "Owned Unwatched Action" not in disc_titles
    assert lib_titles.isdisjoint(disc_titles)


def test_get_recommendations_includes_anime_section_with_its_own_item_type(monkeypatch, tmp_path):
    from config import MediaLibraryPath
    anime_root = tmp_path / "Anime"
    anime_root.mkdir()
    monkeypatch.setattr(plex_api.config, "MEDIA_LIBRARY_PATHS",
                        [MediaLibraryPath(path=str(anime_root), media_type="anime")])
    monkeypatch.setattr(plex_api.config, "TMDB_API_KEY", "")
    monkeypatch.setattr(plex_api, "_account_name_map", lambda: {})
    monkeypatch.setattr(plex_api, "_history_entries", lambda limit: [])
    monkeypatch.setattr(plex_api, "_request_json", lambda path, **k: {"MediaContainer": {}})

    sections = [{"type": "show", "key": "a1", "Location": [{"path": str(anime_root)}]}]
    monkeypatch.setattr(plex_api, "_library_sections", lambda: sections)
    monkeypatch.setattr(
        plex_api, "_section_items",
        lambda key: [_plex_meta("Some Anime", tvdb="900", genres=("Animation",))])

    result = get_recommendations(None, genre_filter="Animation")
    assert len(result.library) == 1
    assert result.library[0].item_type == "anime"


def test_get_recommendations_falls_back_to_popular_genre_when_history_thin(monkeypatch):
    monkeypatch.setattr(plex_api.config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(plex_api, "_account_name_map", lambda: {})
    monkeypatch.setattr(plex_api, "_history_entries", lambda limit: [])  # no seeds at all
    monkeypatch.setattr(plex_api, "_request_json", lambda path, **k: {"MediaContainer": {}})
    monkeypatch.setattr(plex_api, "_library_sections", lambda: [])
    monkeypatch.setattr(plex_api, "_requested_provider_keys", lambda: set())

    fallback_rec = Recommendation(
        title="Popular Fallback", year=2023, item_type="movie", genres=("Action",),
        note="popular Action", in_library=False, tmdb_id="777")
    monkeypatch.setattr(plex_api, "_tmdb_discover_recs",
                        lambda genre, *, exclude_titles, limit: [fallback_rec])

    result = get_recommendations(None, genre_filter="Action")
    assert [r.title for r in result.discover] == ["Popular Fallback"]


def test_requested_provider_keys_reads_real_queue_store():
    queue_store.add_request(
        "inception", "cole", media_type="movie", resolved_title="Inception",
        external_id="27205", identity_source="tmdb")
    queue_store.add_request(
        "no identity yet", "cole", media_type="unknown",
        status=queue_store.STATUS_NEEDS_IDENTITY)
    keys = plex_api._requested_provider_keys()
    assert ("tmdb", "27205") in keys
    assert len(keys) == 1  # the needs_identity row carries no id -> excluded


def test_get_recommendations_stamps_generated_at(monkeypatch):
    monkeypatch.setattr(plex_api.config, "TMDB_API_KEY", "")
    monkeypatch.setattr(plex_api, "_account_name_map", lambda: {})
    monkeypatch.setattr(plex_api, "_history_entries", lambda limit: [])
    monkeypatch.setattr(plex_api, "_request_json", lambda path, **k: {"MediaContainer": {}})
    monkeypatch.setattr(plex_api, "_library_sections", lambda: [])

    before = plex_api.time.time()
    result = get_recommendations(None)
    after = plex_api.time.time()
    assert before <= result.generated_at <= after


# ---------------------------------------------------------------------------
# watchlist_tab.pull_watchlist — token-only config must degrade, not crash.
#
# Regression: build_library_provider_index() needs PLEX_SERVER_URL (it walks
# the local Plex server's library sections) and raises RuntimeError when
# unset, even though get_watchlist() only needs PLEX_TOKEN (it talks to
# discover.provider.plex.tv) and the UI advertises token-only operation. The
# old code called the provider-index build unguarded, so a token-only setup
# failed the ENTIRE pull with "Watchlist unavailable: ..." instead of
# degrading to blank In-Library flags the way the old substring check used
# to. There was previously zero test coverage of pull_watchlist at all.
#
# Per house rule (see test_task_eh_fixsprint.py's header comment), DesktopApp
# / its tabs are Tk GUI classes that must never be INSTANTIATED headless (no
# display in CI). This drives the real, UNBOUND WatchlistTab.pull_watchlist
# against a minimal stand-in "self" carrying only the plain-Python attributes
# the method touches (no ttk widgets constructed anywhere), with
# threading.Thread replaced by a synchronous stand-in so the worker's result
# is observable without a real background thread or a Tk main loop.
# ---------------------------------------------------------------------------

class _SyncThread:
    """threading.Thread stand-in that runs its target synchronously on
    .start(), so a test can observe pull_watchlist's worker result without a
    real background thread or an event loop to marshal onto."""
    def __init__(self, target=None, name=None, daemon=None, **_kwargs):
        self._target = target

    def start(self) -> None:
        self._target()


class _FakeVar:
    def __init__(self, value=""):
        self._value = value

    def set(self, value) -> None:
        self._value = value

    def get(self):
        return self._value


class _FakeTree:
    """Minimal ttk.Treeview stand-in — records inserted rows without any
    real Tk widget."""
    def __init__(self):
        self._order: list[str] = []
        self.rows: dict[str, tuple] = {}

    def get_children(self):
        return list(self._order)

    def delete(self, iid) -> None:
        self._order.remove(iid)
        del self.rows[iid]

    def insert(self, _parent, _index, iid=None, values=None, **_kwargs):
        self._order.append(iid)
        self.rows[iid] = values


class _FakeApp:
    def __init__(self):
        # Task L: recorded rather than no-op'd so a future test can assert
        # on strip posts; existing tests here only care that pull_watchlist
        # doesn't crash calling it against a fake with no real Tk strip.
        self.activity_posts: list[tuple] = []

    def _post_to_ui(self, fn) -> None:
        # The real app marshals onto the Tk main loop; tests just run it
        # inline since _SyncThread already made the worker synchronous.
        fn()

    def _show_warning(self, *_a, **_k) -> None:
        raise AssertionError("pull_watchlist must not warn-dialog")

    def post_activity(self, source, message, level="working", tab=None) -> None:
        self.activity_posts.append((source, message, level, tab))


class _FakeWatchlistTab:
    """Only the plain-Python attributes WatchlistTab.pull_watchlist touches
    — no ttk widgets are ever constructed."""
    def __init__(self):
        self.app = _FakeApp()
        self._status_var = _FakeVar()
        self._wl_tree = _FakeTree()
        self._watchlist: list = []
        # Task L item 4: begin_busy(None, ...) is a documented no-op, so the
        # busy-guard in pull_watchlist is exercised without a real ttk.Button.
        self._pull_btn = None


def test_pull_watchlist_degrades_gracefully_token_only_no_server_url(monkeypatch):
    # The exact regression config: PLEX_TOKEN set, PLEX_SERVER_URL unset —
    # get_watchlist() itself is happy with this; build_library_provider_index
    # is NOT (plex_metrics_enabled() is False without a server URL).
    monkeypatch.setattr(plex_api.config, "PLEX_TOKEN", "token-only-user")
    monkeypatch.setattr(plex_api.config, "PLEX_SERVER_URL", "")

    items = [
        WatchlistItem(title="Some Movie", year=2020, item_type="movie",
                      tmdb_id="123"),
        WatchlistItem(title="Some Show", year=2021, item_type="show",
                      tvdb_id="456"),
    ]
    monkeypatch.setattr(plex_api, "get_watchlist", lambda limit=100: items)
    monkeypatch.setattr(watchlist_tab.threading, "Thread", _SyncThread)

    tab = _FakeWatchlistTab()
    watchlist_tab.WatchlistTab.pull_watchlist(tab)

    # No exception surfaced as a failed pull.
    assert "unavailable" not in tab._status_var.get().casefold()
    assert tab._status_var.get() == "2 watchlist item(s)"
    # Rows still render...
    assert tab._watchlist == items
    rendered_titles = [row[0] for row in tab._wl_tree.rows.values()]
    assert rendered_titles == ["Some Movie", "Some Show"]
    # ...with in_library flags defaulted False (blank checkmark column) since
    # the provider index build degraded and nothing is locally indexed.
    in_lib_flags = [row[3] for row in tab._wl_tree.rows.values()]
    assert in_lib_flags == ["", ""]


def test_pull_watchlist_provider_index_failure_does_not_skip_per_item_checks(monkeypatch):
    """Even when the provider-index build fails, a title that DOES match the
    identity-aware title fallback must still be flagged in-library — proving
    the degrade only drops the provider-id fast path, not presence checking
    entirely."""
    monkeypatch.setattr(plex_api.config, "PLEX_TOKEN", "token-only-user")
    monkeypatch.setattr(plex_api.config, "PLEX_SERVER_URL", "")

    items = [WatchlistItem(title="Owned Title", year=2019, item_type="movie")]
    monkeypatch.setattr(plex_api, "get_watchlist", lambda limit=100: items)
    monkeypatch.setattr("media_lookup.check_library_for_title",
                        lambda title, media_type, **k: (title == "Owned Title", []))
    monkeypatch.setattr(watchlist_tab.threading, "Thread", _SyncThread)

    tab = _FakeWatchlistTab()
    watchlist_tab.WatchlistTab.pull_watchlist(tab)

    assert tab._status_var.get() == "1 watchlist item(s)"
    in_lib_flags = [row[3] for row in tab._wl_tree.rows.values()]
    assert in_lib_flags == ["✓"]
