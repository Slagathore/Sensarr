# =============================================================================
# Task A — identity-anchored requests + season selection, on every intake
# surface. Table-driven per surface (all six), plus the MAFS country-edition
# story, the bare /request needs_identity path, and the season-keyboard flow.
# =============================================================================

import asyncio

import pytest

import bot
import download_manager
import media_lookup
import queue_store
import request_flow
import request_intake
from media_lookup import LookupResult, MediaResult, ParsedRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_db():
    """Drop every request row so each test starts from an empty queue."""
    import db
    queue_store.initialize_queue_db()
    with db.connect(queue_store._db_path()) as conn:
        conn.execute("DELETE FROM requests")
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh_queue():
    _clean_db()
    yield
    _clean_db()


def _movie(title="Inception", year=2010, ext="27205", source="tmdb", countries=()):
    return MediaResult(
        title=title, year=year, external_id=ext,
        external_url=f"https://www.themoviedb.org/movie/{ext}",
        media_type="movie", overview="", source=source,
        origin_countries=tuple(countries))


def _show(title="Married at First Sight", year=2014, ext="272361",
          source="tvdb", countries=("US",)):
    return MediaResult(
        title=title, year=year, external_id=ext,
        external_url=f"https://thetvdb.com/series/{ext}",
        media_type="tv", overview="", source=source,
        origin_countries=tuple(countries))


def _lookup(match, *, in_library=False, external=None):
    req = ParsedRequest(original=match.title, title=match.title, year=match.year,
                        qualifier=None)
    return LookupResult(
        request=req, in_library=in_library, library_matches=[],
        external_matches=external if external is not None else [match],
        best_match=match, search_attempted=True)


# ---- minimal async Telegram fakes -----------------------------------------

class _FakeChat:
    async def send_action(self, *_a, **_k):
        return None


class _FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.chat = _FakeChat()
        self.replies = []

    async def reply_text(self, text, **_k):
        self.replies.append(text)
        return self

    async def reply_html(self, text, **_k):
        self.replies.append(text)
        return self


class _FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message

    async def answer(self, *_a, **_k):
        return None


class _FakeUser:
    def __init__(self, username="cole"):
        self.username = username
        self.full_name = "Cole"


class _FakeUpdate:
    def __init__(self, *, message=None, callback_query=None, user="cole"):
        self.message = message
        self.callback_query = callback_query
        self.effective_user = _FakeUser(user)


class _Ctx:
    def __init__(self, user_data=None, args=None):
        self.user_data = {} if user_data is None else user_data
        self.args = args or []


def _run(coro):
    return asyncio.run(coro)


def _rows(status="all"):
    return queue_store.list_requests(status=status, limit=200)


# ===========================================================================
# Surface-shared core (desktop tab + season expansion delegate to these)
# ===========================================================================

def test_add_matched_request_stores_full_identity():
    created = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie(countries=()))
    assert created.status == queue_store.STATUS_OPEN
    assert created.is_qualified
    assert created.identity_source == "tmdb"
    assert created.external_id == "27205"
    assert created.canonical_year == 2010
    assert created.aliases and created.aliases[0] == "Inception"


def test_add_matched_request_without_external_id_is_needs_identity():
    bad = _movie(ext="")  # a MediaResult with no id is NOT an identity
    created = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=bad)
    assert created.status == queue_store.STATUS_NEEDS_IDENTITY
    assert not created.is_qualified


def test_add_needs_identity_is_never_auto_grabbable():
    created = request_intake.add_needs_identity("mystery thing", "cole",
                                                media_type="unknown")
    assert created.status == queue_store.STATUS_NEEDS_IDENTITY
    assert created.status not in queue_store.AUTO_GRABBABLE_STATUSES
    assert not created.is_qualified


# ===========================================================================
# The MAFS story — country edition alias flows into the auto-grab query
# ===========================================================================

def test_mafs_country_edition_alias_and_query():
    us = _show(ext="us-13", countries=("US",))
    au = _show(ext="au-99", countries=("AU",))  # same base title, other edition
    candidates = [us.title, au.title]  # both render "Married at First Sight"

    outcome = request_intake.add_season_selection(
        "married at first sight", "cole", match=us, seasons=[1],
        candidate_titles=candidates)
    assert len(outcome.request_ids) == 1

    row = queue_store.get_request(outcome.request_ids[0])
    assert row.identity_source == "tvdb"
    assert row.external_id == "us-13"
    assert row.canonical_year == 2014
    assert row.origin_countries == ["US"]
    assert "Married at First Sight US" in row.aliases
    assert row.season == 1
    assert row.is_qualified

    # The auto-grab query for this row pins the US edition.
    query = download_manager._auto_grab_query(row, season=row.season)
    assert query == "Married at First Sight US S01"
    assert "Married at First Sight US" in query


def test_lone_country_show_is_not_country_appended():
    # A show with a country tag but NO same-title sibling keeps a clean alias,
    # so ordinary shows don't get "US" injected into their queries.
    sev = _show(title="Severance", ext="123", countries=("US",))
    outcome = request_intake.add_season_selection(
        "severance", "cole", match=sev, seasons=[1], candidate_titles=["Severance"])
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.aliases[0] == "Severance"
    assert download_manager._auto_grab_query(row, season=1) == "Severance S01"


# ===========================================================================
# Surface 2 — /request command (bare text => needs_identity, visible, skipped)
# ===========================================================================

def test_request_command_bare_text_needs_identity():
    msg = _FakeMessage("married at first sight")
    upd = _FakeUpdate(message=msg)
    _run(bot.request_handler(upd, _Ctx(args=["married", "at", "first", "sight"])))

    rows = _rows()
    assert len(rows) == 1
    assert rows[0].status == queue_store.STATUS_NEEDS_IDENTITY
    assert not rows[0].is_qualified


def test_auto_grab_skips_needs_identity(monkeypatch):
    request_intake.add_needs_identity("married at first sight", "cole")
    # Auto-grab must never query a needs_identity row. Fail loudly if it does.
    def _boom(*a, **k):
        raise AssertionError("searched a needs_identity row")
    monkeypatch.setattr(download_manager, "search_torrents", _boom)
    monkeypatch.setattr(download_manager, "search_collect", _boom)
    dm = download_manager.DownloadManager()
    assert dm.auto_grab_open_requests() == []


# ===========================================================================
# Surface 1 — structured Telegram confirmation stores source+year+country+alias
# ===========================================================================

def test_telegram_confirmation_stores_identity_for_movie():
    match = _movie(title="Dune Part Two", year=2024, ext="693134", countries=())
    ud = {
        request_flow._UD_RESULTS: [_lookup(match)],
        request_flow._UD_MEDIA_TYPE: "movie",
        request_flow._UD_REMOVED: set(),
    }
    msg = _FakeMessage()
    query = _FakeQuery("req_confirm_yes", msg)
    upd = _FakeUpdate(callback_query=query)
    state = _run(request_flow.handle_confirmation(upd, _Ctx(ud)))

    assert state == request_flow.ConversationHandler.END
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].media_type == "movie"
    assert rows[0].identity_source == "tmdb"
    assert rows[0].external_id == "693134"
    assert rows[0].is_qualified


# ===========================================================================
# Surface 4 — the Other flow: an LLM-typed media guess with no id is
# needs_identity, never an open typed row (the #85 shape)
# ===========================================================================

def test_other_flow_typed_media_becomes_needs_identity(monkeypatch):
    monkeypatch.setattr(request_flow, "categorize_other_request",
                        lambda _t: {"category": "movie", "title": "Some Movie",
                                    "reasoning": "looks filmish", "flagged": False})
    msg = _FakeMessage("that one movie about dreams")
    upd = _FakeUpdate(message=msg)
    ud = {request_flow._UD_MEDIA_TYPE: "other"}
    _run(request_flow.handle_content_input(upd, _Ctx(ud)))

    rows = _rows()
    assert len(rows) == 1
    assert rows[0].media_type == "movie"
    assert rows[0].status == queue_store.STATUS_NEEDS_IDENTITY


def test_other_flow_nonmedia_stays_open_exempt(monkeypatch):
    monkeypatch.setattr(request_flow, "categorize_other_request",
                        lambda _t: {"category": "other", "title": "Some Game",
                                    "reasoning": "a video game", "flagged": False})
    msg = _FakeMessage("that indie roguelike")
    upd = _FakeUpdate(message=msg)
    _run(request_flow.handle_content_input(upd, _Ctx({request_flow._UD_MEDIA_TYPE: "other"})))
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].media_type == "other"
    # 'other' is a deliberate exempt choice — open, but not identity-qualified.
    assert not rows[0].is_qualified


# ===========================================================================
# Surface 6 — season keyboard: "All" on a 5-season show creates 5 rows sharing
# one batch_id; S00 Specials is excluded
# ===========================================================================

def test_season_keyboard_all_creates_batch_rows_excludes_specials(monkeypatch):
    show = _show(title="Some Drama", ext="tv-5", source="tmdb", countries=("US",))
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3, 4, 5), has_specials=True, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)

    ud = {
        request_flow._UD_RESULTS: [_lookup(show)],
        request_flow._UD_MEDIA_TYPE: "tv",
        request_flow._UD_REMOVED: set(),
    }
    ctx = _Ctx(ud)

    # 1) Submit the TV request -> season picker shown.
    msg = _FakeMessage()
    upd = _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg))
    state = _run(request_flow.handle_confirmation(upd, ctx))
    assert state == request_flow.SELECTING_SEASONS

    # 2) Tap "All currently available".
    msg2 = _FakeMessage()
    upd2 = _FakeUpdate(callback_query=_FakeQuery("req_season_all", msg2))
    state2 = _run(request_flow.handle_season_selection(upd2, ctx))
    assert state2 == request_flow.ConversationHandler.END

    rows = _rows()
    assert len(rows) == 5
    assert sorted(r.season for r in rows) == [1, 2, 3, 4, 5]
    assert 0 not in [r.season for r in rows]  # S00 excluded from "All"
    batch_ids = {r.batch_id for r in rows}
    assert len(batch_ids) == 1 and None not in batch_ids
    assert all(r.is_qualified for r in rows)


def test_season_keyboard_single_pick_one_row(monkeypatch):
    show = _show(title="Some Drama", ext="tv-5", source="tmdb", countries=())
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3), has_specials=True, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    ud = {
        request_flow._UD_RESULTS: [_lookup(show)],
        request_flow._UD_MEDIA_TYPE: "tv",
        request_flow._UD_REMOVED: set(),
    }
    ctx = _Ctx(ud)
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))
    _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_pick_2", _FakeMessage())), ctx))
    rows = _rows()
    assert len(rows) == 1 and rows[0].season == 2 and rows[0].batch_id is None


def test_season_keyboard_specials_opt_in(monkeypatch):
    show = _show(title="Some Drama", ext="tv-5", source="tmdb", countries=())
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2), has_specials=True, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    ud = {
        request_flow._UD_RESULTS: [_lookup(show)],
        request_flow._UD_MEDIA_TYPE: "tv",
        request_flow._UD_REMOVED: set(),
    }
    ctx = _Ctx(ud)
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))
    _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_specials", _FakeMessage())), ctx))
    rows = _rows()
    assert len(rows) == 1 and rows[0].season == 0


# ===========================================================================
# Surface 5 — watchlist uses the parsed Plex GUID identity, not a title search
# ===========================================================================

class _WLItem:
    def __init__(self, title, year, item_type, tmdb=None, tvdb=None, imdb=None):
        self.title = title
        self.year = year
        self.item_type = item_type
        self.tmdb_id = tmdb
        self.tvdb_id = tvdb
        self.imdb_id = imdb

    @property
    def identity(self):
        if self.item_type == "show":
            if self.tvdb_id:
                return ("tvdb", self.tvdb_id)
            if self.tmdb_id:
                return ("tmdb", self.tmdb_id)
        elif self.tmdb_id:
            return ("tmdb", self.tmdb_id)
        if self.imdb_id:
            return ("omdb", self.imdb_id)
        return None


def test_watchlist_movie_uses_guid_identity():
    item = _WLItem("Inception", 2010, "movie", tmdb="27205")
    outcome = request_intake.queue_watchlist_item(item, "Watchlist")
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.status == queue_store.STATUS_OPEN
    assert row.identity_source == "tmdb" and row.external_id == "27205"
    assert row.is_qualified


def test_watchlist_show_expands_seasons_from_identity():
    item = _WLItem("Some Show", 2020, "show", tmdb="tv-9", tvdb="tvdb-9")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3), has_specials=True, resolved=True)
    outcome = request_intake.queue_watchlist_item(
        item, "Watchlist", get_seasons=lambda *a, **k: seasons)
    rows = [queue_store.get_request(i) for i in outcome.request_ids]
    assert len(rows) == 3
    assert sorted(r.season for r in rows) == [1, 2, 3]
    assert len({r.batch_id for r in rows}) == 1


def test_watchlist_no_guid_is_needs_identity():
    item = _WLItem("Some Movie", 2020, "movie")  # no GUID at all
    outcome = request_intake.queue_watchlist_item(item, "Watchlist")
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.status == queue_store.STATUS_NEEDS_IDENTITY


def test_watchlist_show_unresolvable_seasons_is_needs_identity():
    item = _WLItem("Some Show", 2020, "show", tvdb="tvdb-only")
    unresolved = media_lookup.ShowSeasons(resolved=False)
    outcome = request_intake.queue_watchlist_item(
        item, "Watchlist", get_seasons=lambda *a, **k: unresolved)
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.status == queue_store.STATUS_NEEDS_IDENTITY


# ===========================================================================
# Query builder + dedupe + Plex GUID parsing
# ===========================================================================

def test_auto_grab_query_movie_appends_year():
    row = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie(year=2010))
    assert download_manager._auto_grab_query(row) == "Inception 2010"


def test_seasonwise_grab_query_contains_alias(monkeypatch):
    row = request_intake.add_season_selection(
        "married at first sight", "cole",
        match=_show(ext="us-13", countries=("US",)), seasons=[1],
        candidate_titles=["Married at First Sight", "Married at First Sight"])
    req = queue_store.get_request(row.request_ids[0])

    captured = []
    from torrent_search import CollectedPool
    monkeypatch.setattr(
        download_manager, "search_collect",
        lambda q, *a, **k: captured.append(q) or CollectedPool(
            results=tuple(), pool_stats={}))
    dm = download_manager.DownloadManager()
    dm._grab_request_seasonwise(req)
    assert captured, "season-wise grab issued no query"
    assert any("Married at First Sight US" in q for q in captured)
    assert any("S01" in q for q in captured)


def test_find_duplicate_requests_keys_on_identity():
    # Intake now dedupes identical identities, so the two duplicate rows this
    # detection test needs are created via the insert-always primitive directly
    # (queue_store.add_request), which is exactly what find_duplicate_requests
    # exists to surface for pre-existing / legacy pileups.
    for content, who in (("dune", "a"), ("Dune!!", "b")):
        queue_store.add_request(
            content, who, media_type="movie", resolved_title="Dune",
            external_id="555",
            external_url="https://www.themoviedb.org/movie/555",
            status=queue_store.STATUS_OPEN, identity_source="tmdb")
    groups = queue_store.find_duplicate_requests()
    assert len(groups) == 1 and len(groups[0]) == 2


def test_find_duplicate_requests_separates_seasons():
    show = _show(title="Some Show", ext="s-1", source="tmdb", countries=())
    request_intake.add_matched_request("show", "a", media_type="tv", match=show, season=1)
    request_intake.add_matched_request("show", "b", media_type="tv", match=show, season=2)
    # Different seasons of the same show are NOT duplicates.
    assert queue_store.find_duplicate_requests() == []


def test_resolve_needs_identity_row_becomes_open():
    # A legacy needs_identity row gains identity and flips to open (the resolve
    # path required by the Phase 2 gate).
    legacy = request_intake.add_needs_identity("inception", "cole", media_type="movie")
    assert legacy.status == queue_store.STATUS_NEEDS_IDENTITY

    updated = request_intake.resolve_request(legacy.request_id, _movie(ext="27205"))
    assert updated.status == queue_store.STATUS_OPEN
    assert updated.is_qualified
    assert updated.identity_source == "tmdb" and updated.external_id == "27205"
    assert updated.aliases and updated.aliases[0] == "Inception"


def test_resolve_tv_row_records_season():
    legacy = request_intake.add_needs_identity("some show", "cole", media_type="tv")
    updated = request_intake.resolve_request(
        legacy.request_id, _show(title="Some Show", ext="s9", source="tmdb", countries=()),
        season=2)
    assert updated.status == queue_store.STATUS_OPEN
    assert updated.season == 2


def test_plex_guid_parsing():
    import plex_api
    guids = plex_api._parse_plex_guids({"Guid": [
        {"id": "tmdb://12345"}, {"id": "tvdb://678"}, {"id": "imdb://tt99"}]})
    assert guids == {"tmdb": "12345", "tvdb": "678", "imdb": "tt99"}


def test_media_result_carries_origin_country_from_tmdb(monkeypatch):
    payload = {"results": [{
        "id": 99, "name": "Married at First Sight", "first_air_date": "2014-07-08",
        "origin_country": ["US"], "overview": "x"}]}
    monkeypatch.setattr(media_lookup, "_tmdb_enabled", lambda: True)
    monkeypatch.setattr(media_lookup, "_get_json", lambda *a, **k: payload)
    monkeypatch.setattr(media_lookup.config, "TMDB_API_KEY", "k", raising=False)
    results = media_lookup.search_tmdb_shows("married at first sight")
    assert results and results[0].origin_countries == ("US",)


# ===========================================================================
# ROUND 2 must-fix regressions (cold-verification findings)
# ===========================================================================

# ---- Must-fix 1: the row's search alias always wins over a fuzzy tracked-
# show match for query construction -----------------------------------------

def test_seasonwise_alias_survives_competing_tracked_show(monkeypatch):
    """The verifier's reproduced bug: a tracked show under the plain base title
    ("Married at First Sight", e.g. the AU edition or a legacy Plex-scan entry)
    fuzzy-matches the request alias at >=0.85 and used to hijack the query back
    to the plain title. The request row's US alias must reach every query."""
    import shows_store
    show_id = shows_store.upsert_show(
        title="Married at First Sight", media_type="tv",
        source="tvdb", external_id="au-99")
    try:
        outcome = request_intake.add_season_selection(
            "married at first sight", "cole",
            match=_show(ext="us-13", countries=("US",)), seasons=[1],
            candidate_titles=["Married at First Sight", "Married at First Sight"])
        req = queue_store.get_request(outcome.request_ids[0])
        assert "Married at First Sight US" in req.aliases

        captured = []
        from torrent_search import CollectedPool
        monkeypatch.setattr(
            download_manager, "search_collect",
            lambda q, *a, **k: captured.append(q) or CollectedPool(
                results=tuple(), pool_stats={}))
        dm = download_manager.DownloadManager()
        # Sanity: the competing show DOES fuzzy-match (the bug's precondition).
        assert dm._match_tracked_show("Married at First Sight US") is not None
        dm._grab_request_seasonwise(req)

        assert captured, "season-wise grab issued no query"
        for q in captured:
            assert "Married at First Sight US" in q, (
                f"query lost the country alias: {q!r}")
    finally:
        shows_store.remove_show(show_id)


# ---- Must-fix 2: TVDB season resolution ------------------------------------

def test_get_show_seasons_tvdb_resolves_real_grid(monkeypatch):
    payload = {"data": {"episodes": [
        {"seasonNumber": 0, "aired": "2015-01-01"},
        {"seasonNumber": 1, "aired": "2014-07-08"},
        {"seasonNumber": 1, "aired": "2014-07-15"},
        {"seasonNumber": 2, "aired": "2015-03-01"},
        {"seasonNumber": 3, "aired": "2016-03-01"},
        {"seasonNumber": 4, "aired": "2099-01-01"},  # future — not yet aired
    ]}}
    monkeypatch.setattr(media_lookup, "_tvdb_get_token", lambda: "tok")
    monkeypatch.setattr(media_lookup, "_get_json", lambda *a, **k: payload)
    seasons = media_lookup.get_show_seasons("tvdb", "272361")
    assert seasons.resolved
    assert seasons.regular_seasons == (1, 2, 3)
    assert seasons.has_specials


def test_get_show_seasons_tvdb_unavailable_falls_back(monkeypatch):
    monkeypatch.setattr(media_lookup, "_tvdb_get_token", lambda: None)
    assert media_lookup.get_show_seasons("tvdb", "272361").resolved is False


def test_get_show_seasons_providers_without_season_data():
    # jikan/anidb model each season as its own entry — manual fallback only.
    assert media_lookup.get_show_seasons("jikan", "123").resolved is False
    assert media_lookup.get_show_seasons(None, None).resolved is False


# ---- Must-fix 3: TVDB alpha-3 country codes normalise to alpha-2 -----------

def test_tvdb_search_normalises_alpha3_country(monkeypatch):
    payload = {"data": [{
        "tvdb_id": "272361", "name": "Married at First Sight",
        "slug": "married-at-first-sight", "year": "2014",
        "country": "usa", "overview": "x"}]}
    monkeypatch.setattr(media_lookup, "_tvdb_get_token", lambda: "tok")
    monkeypatch.setattr(media_lookup, "_get_json", lambda *a, **k: payload)
    results = media_lookup.search_tvdb_shows("married at first sight")
    assert results and results[0].origin_countries == ("US",)


def test_normalize_country_table():
    import media_identity
    cases = [("usa", "US"), ("USA", "US"), ("US", "US"), ("gbr", "UK"),
             ("aus", "AU"), ("jpn", "JP"), ("kor", "KR"), ("", ""),
             (None, ""), ("ZZX", "ZZX")]  # unknown passes through, not guessed
    for raw, want in cases:
        assert media_identity.normalize_country(raw) == want, raw


def test_country_edition_mismatch_consistent_across_code_widths():
    import media_identity
    # alpha-3 vs alpha-2 of the same country never contradict...
    assert not media_identity.country_edition_mismatch(["USA"], ["US"])
    assert not media_identity.country_edition_mismatch(["US"], ["usa"])
    # ...and a real contradiction still rejects whichever width is used.
    assert media_identity.country_edition_mismatch(["USA"], ["AUS"])
    assert media_identity.country_edition_mismatch(["US"], ["AU"])


# ---- Must-fix 4: desktop picker decision logic (Tk glue excluded) ----------

def test_format_candidate_label():
    cases = [
        (_show(ext="us-13", countries=("US",)),
         "Married at First Sight (2014) [US] - TVDB"),
        (_movie(countries=()), "Inception (2010) - TMDB"),
        (MediaResult(title="No Year", year=None, external_id="1",
                     external_url="", media_type="movie", overview="",
                     source="tmdb"),
         "No Year - TMDB"),
    ]
    for match, want in cases:
        assert request_intake.format_candidate_label(match) == want


def test_seasons_for_answer_table():
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3), has_specials=True, resolved=True)
    get = lambda *a, **k: seasons
    show = _show()
    cases = [
        (None, None),          # dialog cancelled -> add nothing
        ("all", [1, 2, 3]),
        ("", [1, 2, 3]),       # blank = all (documented desktop behavior)
        ("3", [3]),
        ("season 2", [2]),
        ("junk", [1]),         # unparseable -> S1
    ]
    for answer, want in cases:
        assert request_intake.seasons_for_answer(answer, show, get_seasons=get) == want
    # Unresolvable provider list on 'all' -> S1 fallback.
    empty = media_lookup.ShowSeasons(resolved=False)
    assert request_intake.seasons_for_answer(
        "all", show, get_seasons=lambda *a, **k: empty) == [1]


def test_parse_single_season():
    assert request_intake.parse_single_season("3") == 3
    assert request_intake.parse_single_season("season 2") == 2
    assert request_intake.parse_single_season("") == 1
    assert request_intake.parse_single_season(None) == 1
    assert request_intake.parse_single_season("x", default=5) == 5


def test_add_picked_candidate_movie():
    candidates = [_movie(countries=())]
    outcome = request_intake.add_picked_candidate("inception", "Admin", candidates, 0)
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.status == queue_store.STATUS_OPEN and row.is_qualified
    assert row.identity_source == "tmdb"


def test_add_picked_candidate_tv_expands_seasons():
    candidates = [_show(ext="tv-7", countries=("US",))]
    outcome = request_intake.add_picked_candidate(
        "mafs", "Admin", candidates, 0, seasons=[1, 2])
    rows = [queue_store.get_request(i) for i in outcome.request_ids]
    assert sorted(r.season for r in rows) == [1, 2]
    assert len({r.batch_id for r in rows}) == 1 and rows[0].batch_id is not None


def test_add_picked_candidate_none_of_these():
    candidates = [_movie()]
    # index past the list = the "None of these" row.
    outcome = request_intake.add_picked_candidate("thing", "Admin", candidates,
                                                  len(candidates))
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.status == queue_store.STATUS_NEEDS_IDENTITY


def test_add_picked_candidate_tv_without_seasons_stays_needs_identity():
    # Season prompt skipped/empty: never a season=NULL whole-show TV row.
    candidates = [_show(ext="tv-8", countries=())]
    outcome = request_intake.add_picked_candidate(
        "show", "Admin", candidates, 0, seasons=None)
    row = queue_store.get_request(outcome.request_ids[0])
    assert row.status == queue_store.STATUS_NEEDS_IDENTITY
    assert row.season is None and row.media_type == "tv"
