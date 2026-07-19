# =============================================================================
# Task F (fix sprint) — Telegram season guidance.
#
# _prompt_next_season now shows a one-line context (total seasons + airing
# status + whichever of have/missing is shorter), "Grab everything missing"
# is the one-tap default that only grabs unowned seasons, owned seasons show
# as marked/disabled labels, airing shows get a "Keep it updated" button that
# wires shows_store.set_show_auto_grab, and a fully-owned show skips the grid
# entirely. Anime/xanime get the same enrichment but degrade to a simpler
# add/keep-updated prompt since TMDB/TVDB season enumeration doesn't apply.
# =============================================================================

import asyncio

import pytest

import db
import maintenance
import media_lookup
import queue_store
import request_flow
import request_intake
import shows_store
from media_lookup import EpisodeInfo, LookupResult, MediaResult, ParsedRequest


# ---------------------------------------------------------------------------
# Cleanup — requests + everything shows_store touches (Task F introduces
# tracked-show reads/writes into the Telegram flow for the first time).
# ---------------------------------------------------------------------------

def _clean():
    queue_store.initialize_queue_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("requests", "episodes", "tracked_shows",
                      "show_folders", "season_targets"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_task_a_intake.py's minimal async Telegram fakes)
# ---------------------------------------------------------------------------

class _FakeChat:
    async def send_action(self, *_a, **_k):
        return None


class _FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.chat = _FakeChat()
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **k):
        self.replies.append(text)
        self.markups.append(k.get("reply_markup"))
        return self

    async def reply_html(self, text, **k):
        self.replies.append(text)
        self.markups.append(k.get("reply_markup"))
        return self


class _FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, *a, **_k):
        self.answers.append(a[0] if a else None)
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
    def __init__(self, user_data=None):
        self.user_data = {} if user_data is None else user_data


def _run(coro):
    return asyncio.run(coro)


def _rows(status="all"):
    return queue_store.list_requests(status=status, limit=200)


def _movie(title="Inception", year=2010, ext="27205", source="tmdb"):
    return MediaResult(title=title, year=year, external_id=ext,
                       external_url=f"https://x/{ext}", media_type="movie",
                       overview="", source=source)


def _show(title="Some Drama", year=2020, ext="tv-5", source="tmdb",
         countries=()):
    return MediaResult(title=title, year=year, external_id=ext,
                       external_url=f"https://x/{ext}", media_type="tv",
                       overview="", source=source, origin_countries=tuple(countries))


def _anime(title="Frieren", year=2023, ext="mal-1", source="jikan",
          media_type="anime"):
    return MediaResult(title=title, year=year, external_id=ext,
                       external_url=f"https://x/{ext}", media_type=media_type,
                       overview="", source=source)


def _lookup(match):
    req = ParsedRequest(original=match.title, title=match.title, year=match.year,
                        qualifier=None)
    return LookupResult(request=req, in_library=False, library_matches=[],
                        external_matches=[match], best_match=match,
                        search_attempted=True)


def _confirm_ctx(match, media_type):
    ud = {
        request_flow._UD_RESULTS: [_lookup(match)],
        request_flow._UD_MEDIA_TYPE: media_type,
        request_flow._UD_REMOVED: set(),
    }
    return _Ctx(ud)


def _track_show_with_seasons(match, *, media_type="tv", have_episodes: dict):
    """Track a show and give it real episode rows so shows_store.have_seasons /
    have_count reflect what's "owned". have_episodes maps season -> list of
    episode numbers that are ON DISK; every season present in the dict also
    gets one aired-but-missing episode (episode 99) unless explicitly given,
    so episode_count/have_count separate cleanly for anime-style assertions
    when the caller wants a partial season."""
    show_id = shows_store.upsert_show(
        title=match.title, media_type=media_type, source=match.source,
        external_id=str(match.external_id), external_url=match.external_url,
        year=match.year)
    episodes = []
    found = {}
    for season, have_eps in have_episodes.items():
        for ep in have_eps:
            episodes.append(EpisodeInfo(season=season, episode=ep, title="",
                                        air_date="2020-01-01"))
            found[(season, ep)] = f"/lib/S{season:02d}E{ep:02d}.mkv"
    shows_store.replace_episodes(show_id, episodes)
    shows_store.update_file_state(show_id, found)
    return show_id


# ===========================================================================
# Pure helpers
# ===========================================================================

def test_format_season_ranges_contiguous_and_gaps():
    assert request_flow._format_season_ranges([1, 2, 3]) == "S01-S03"
    assert request_flow._format_season_ranges([1, 3, 4, 5, 8]) == "S01, S03-S05, S08"
    assert request_flow._format_season_ranges([]) == ""


def test_season_context_line_states_shorter_of_have_missing():
    # have (1) shorter than missing (4) -> "You have"
    line = request_flow._season_context_line(
        total=5, status_label="ended", next_air="",
        have=[1], missing=[2, 3, 4, 5])
    assert "5 seasons" in line and "ended" in line
    assert "You have S01" in line
    assert "Missing" not in line

    # missing (1) shorter than have (4) -> "Missing"
    line2 = request_flow._season_context_line(
        total=5, status_label="airing", next_air="Aug 2",
        have=[1, 2, 3, 4], missing=[5])
    assert "airing, next episode Aug 2" in line2
    assert "Missing S05" in line2
    assert "You have" not in line2

    # nothing owned yet -> no have/missing clause forced, but total+status always present
    line3 = request_flow._season_context_line(
        total=3, status_label="ended", next_air="", have=[], missing=[1, 2, 3])
    assert line3.startswith("3 seasons, ended")


def test_anime_context_line_uses_episode_counts():
    line = request_flow._anime_context_line("airing", "Aug 2", 12, 24)
    assert "Airing, next episode Aug 2" in line
    assert "You have 12 of 24 episodes" in line

    line2 = request_flow._anime_context_line("ended", "", 0, 13)
    assert "Ended" in line2
    assert "don't have any of the 13 episodes yet" in line2

    # untracked (total 0) -> no episode clause at all, still no crash
    line3 = request_flow._anime_context_line("", "", 0, 0)
    assert line3 == ""


# ===========================================================================
# TV — context line, owned-season marking, grab-everything-missing
# ===========================================================================

def test_tv_prompt_shows_context_line_and_marks_owned_seasons(monkeypatch):
    show = _show(title="Long Runner", ext="tv-200", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3, 4, 5), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "Ended")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)

    _track_show_with_seasons(show, have_episodes={1: [1, 2], 2: [1, 2], 3: [1]})

    ctx = _confirm_ctx(show, "tv")
    msg = _FakeMessage()
    state = _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))
    assert state == request_flow.SELECTING_SEASONS

    text = msg.replies[-1]
    assert "5 seasons" in text and "ended" in text
    # have = {1,2,3} (3), missing = {4,5} (2) -> missing is shorter -> "Missing"
    assert "Missing S04-S05" in text

    keyboard = msg.markups[-1]
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    labels = {b.text: b.callback_data for b in buttons}
    assert "📥 Grab everything missing" in labels
    assert labels["✅ S01"] == "req_season_owned_1"
    assert labels["✅ S02"] == "req_season_owned_2"
    assert labels["✅ S03"] == "req_season_owned_3"
    assert labels["S04"] == "req_season_pick_4"
    assert labels["S05"] == "req_season_pick_5"
    # ended, not airing -> no Keep it updated button here
    assert "🔔 Keep it updated" not in labels


def test_grab_everything_missing_adds_only_missing_seasons(monkeypatch):
    show = _show(title="Long Runner", ext="tv-201", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)
    _track_show_with_seasons(show, have_episodes={1: [1]})

    ctx = _confirm_ctx(show, "tv")
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))
    state = _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_all", _FakeMessage())), ctx))
    assert state == request_flow.ConversationHandler.END

    rows = _rows()
    assert sorted(r.season for r in rows) == [2, 3]  # season 1 already owned, skipped


def test_untracked_show_grab_everything_missing_grabs_all(monkeypatch):
    """No prior tracked show -> have is empty -> 'missing' == every aired
    season, same as the legacy 'All currently available' behavior."""
    show = _show(title="Fresh Show", ext="tv-5", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3, 4, 5), has_specials=True, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)

    ctx = _confirm_ctx(show, "tv")
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))
    state = _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_all", _FakeMessage())), ctx))
    assert state == request_flow.ConversationHandler.END

    rows = _rows()
    assert sorted(r.season for r in rows) == [1, 2, 3, 4, 5]
    assert 0 not in [r.season for r in rows]


def test_owned_season_button_is_noop(monkeypatch):
    show = _show(title="Long Runner", ext="tv-202", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)
    _track_show_with_seasons(show, have_episodes={1: [1]})

    ctx = _confirm_ctx(show, "tv")
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))

    tap_msg = _FakeMessage()
    query = _FakeQuery("req_season_owned_1", tap_msg)
    state = _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=query), ctx))

    assert state == request_flow.SELECTING_SEASONS
    assert query.answers and "already have" in query.answers[0].lower()
    assert tap_msg.replies == []          # no re-prompt sent
    assert _rows() == []                  # nothing added to the queue


def test_fully_owned_show_offers_only_keep_updated(monkeypatch):
    show = _show(title="Complete Show", ext="tv-203", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "Ended")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)
    _track_show_with_seasons(show, have_episodes={1: [1, 2], 2: [1, 2]})

    ctx = _confirm_ctx(show, "tv")
    msg = _FakeMessage()
    state = _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))
    assert state == request_flow.SELECTING_SEASONS

    text = msg.replies[-1]
    assert "already have all of it" in text

    keyboard = msg.markups[-1]
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    labels = {b.text: b.callback_data for b in buttons}
    assert set(labels) == {"🔔 Keep it updated", "⏭️ Skip this show", "❌ Cancel"}


def test_keep_updated_sets_auto_grab_and_confirms(monkeypatch):
    show = _show(title="Airing Show", ext="tv-204", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1,), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "Returning Series")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air",
                        lambda *a, **k: EpisodeInfo(season=2, episode=1, title="",
                                                    air_date="2026-08-02"))

    ctx = _confirm_ctx(show, "tv")
    msg = _FakeMessage()
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))
    assert "airing, next episode Aug 2" in msg.replies[-1]
    keyboard = msg.markups[-1]
    labels = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert "🔔 Keep it updated" in labels

    finish_msg = _FakeMessage()
    state = _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_keep_updated", finish_msg)), ctx))
    assert state == request_flow.ConversationHandler.END

    tracked = shows_store.get_show_by_identity("tmdb", "tv-204")
    assert tracked is not None and tracked.auto_grab is True
    assert "keeping it updated" in finish_msg.replies[-1].lower()
    assert "automatically" in finish_msg.replies[-1].lower()
    assert _rows() == []  # keep-updated adds no request rows


def test_handle_season_text_missing_alias(monkeypatch):
    show = _show(title="Typed Show", ext="tv-205", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)
    _track_show_with_seasons(show, have_episodes={1: [1]})

    ctx = _confirm_ctx(show, "tv")
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))

    text_msg = _FakeMessage("missing")
    state = _run(request_flow.handle_season_text(_FakeUpdate(message=text_msg), ctx))
    assert state == request_flow.ConversationHandler.END

    rows = _rows()
    assert sorted(r.season for r in rows) == [2, 3]


def test_tv_prompt_degrades_gracefully_on_status_fetch_failure(monkeypatch):
    show = _show(title="Flaky Metadata Show", ext="tv-206", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)

    def _boom(*_a, **_k):
        raise RuntimeError("TMDB is down")
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", _boom)

    def _boom_owned(*_a, **_k):
        raise RuntimeError("shows_store hiccup")
    monkeypatch.setattr(request_flow, "_owned_seasons", _boom_owned)

    ctx = _confirm_ctx(show, "tv")
    msg = _FakeMessage()
    state = _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))

    # The picker must still work — status/owned lookups degrade to "no info"
    # rather than blocking the prompt.
    assert state == request_flow.SELECTING_SEASONS
    keyboard = msg.markups[-1]
    labels = {b.text for row in keyboard.inline_keyboard for b in row}
    assert "📥 Grab everything missing" in labels
    assert "S01" in labels and "S02" in labels


# ===========================================================================
# Anime/xAnime — simpler add/keep-updated prompt (no season grid)
# ===========================================================================

def test_anime_prompt_shows_context_and_add_to_queue(monkeypatch):
    anime = _anime(title="Frieren", ext="mal-1", source="jikan")
    monkeypatch.setattr(request_flow, "get_anime_airing",
                        lambda title, explicit=False: (
                            EpisodeInfo(season=1, episode=13, title="",
                                       air_date="2026-08-02"), "Airing"))
    _track_show_with_seasons(anime, media_type="anime",
                             have_episodes={1: list(range(1, 13))})
    # give it 24 known episodes total (12 owned + 12 aired-but-missing)
    show = shows_store.get_show_by_identity("jikan", "mal-1")
    extra = [EpisodeInfo(season=1, episode=e, title="", air_date="2020-01-01")
             for e in range(1, 25)]
    shows_store.replace_episodes(show.show_id, extra)
    shows_store.update_file_state(
        show.show_id, {(1, e): f"/lib/E{e:02d}.mkv" for e in range(1, 13)})

    ctx = _confirm_ctx(anime, "anime")
    msg = _FakeMessage()
    state = _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))
    assert state == request_flow.SELECTING_SEASONS

    text = msg.replies[-1]
    assert "Airing, next episode Aug 2" in text
    assert "You have 12 of 24 episodes" in text

    keyboard = msg.markups[-1]
    labels = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert labels["➕ Add to queue"] == "req_season_anime_add"
    assert labels["🔔 Keep it updated"] == "req_season_anime_keep_updated"

    add_msg = _FakeMessage()
    state2 = _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_anime_add", add_msg)), ctx))
    assert state2 == request_flow.ConversationHandler.END

    rows = _rows()
    assert len(rows) == 1
    assert rows[0].media_type == "anime"
    assert rows[0].season is None
    assert rows[0].identity_source == "jikan" and rows[0].external_id == "mal-1"


def test_anime_keep_updated_sets_auto_grab_without_adding_row(monkeypatch):
    anime = _anime(title="Ongoing Anime", ext="mal-2", source="jikan")
    monkeypatch.setattr(request_flow, "get_anime_airing",
                        lambda title, explicit=False: (None, "Airing"))

    ctx = _confirm_ctx(anime, "anime")
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))

    finish_msg = _FakeMessage()
    state = _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_anime_keep_updated", finish_msg)),
        ctx))
    assert state == request_flow.ConversationHandler.END

    tracked = shows_store.get_show_by_identity("jikan", "mal-2")
    assert tracked is not None and tracked.auto_grab is True
    assert tracked.media_type == "anime"
    assert "keeping it updated" in finish_msg.replies[-1].lower()
    assert _rows() == []


def test_xanime_follows_anime_path(monkeypatch):
    hentai = _anime(title="Adult Title", ext="anidb-9", source="anidb",
                    media_type="xanime")
    monkeypatch.setattr(request_flow, "get_anime_airing",
                        lambda title, explicit=False: (None, "Ended"))

    ctx = _confirm_ctx(hentai, "xanime")
    msg = _FakeMessage()
    state = _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))
    assert state == request_flow.SELECTING_SEASONS
    assert "🔞" in msg.replies[-1]
    keyboard = msg.markups[-1]
    labels = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert labels["➕ Add to queue"] == "req_season_anime_add"
    # ended -> no Keep it updated button
    assert "🔔 Keep it updated" not in labels

    add_msg = _FakeMessage()
    _run(request_flow.handle_season_selection(
        _FakeUpdate(callback_query=_FakeQuery("req_season_anime_add", add_msg)), ctx))
    rows = _rows()
    assert len(rows) == 1 and rows[0].media_type == "xanime"


def test_anime_prompt_degrades_gracefully_on_airing_fetch_failure(monkeypatch):
    anime = _anime(title="Flaky Anime", ext="mal-3", source="jikan")

    def _boom(*_a, **_k):
        raise RuntimeError("AniList unreachable")
    monkeypatch.setattr(request_flow, "get_anime_airing", _boom)

    ctx = _confirm_ctx(anime, "anime")
    msg = _FakeMessage()
    state = _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", msg)), ctx))

    assert state == request_flow.SELECTING_SEASONS
    keyboard = msg.markups[-1]
    labels = {b.text for row in keyboard.inline_keyboard for b in row}
    assert "➕ Add to queue" in labels


# ===========================================================================
# Independent-review follow-ups (dedupe honesty, plain-punctuation copy,
# and a state-map registration regression test)
# ===========================================================================

def test_typed_owned_season_reports_already_have_and_adds_no_new_row(monkeypatch):
    """The typed-season escape hatch (handle_season_text's numeric fallback)
    can reach a season that's already fulfilled and still in the library.
    find_existing_request (request_intake.py) correctly reuses that row
    instead of inserting a duplicate — the bot reply must say so plainly,
    not claim the season was added."""
    show = _show(title="Owned Already", ext="tv-300", source="tmdb")
    seasons = media_lookup.ShowSeasons(
        regular_seasons=(1, 2, 3), has_specials=False, resolved=True)
    monkeypatch.setattr(request_flow, "get_show_seasons", lambda *a, **k: seasons)
    monkeypatch.setattr(request_flow, "get_tmdb_tv_status", lambda *a, **k: "")
    monkeypatch.setattr(request_flow, "get_tmdb_next_air", lambda *a, **k: None)
    # No shows_store tracking here on purpose — the "already have" signal
    # under test comes from the request-dedupe layer (an existing fulfilled
    # row still in the library), independent of the season-context ownership
    # display, which is exactly the gap the reviewer flagged.
    existing = queue_store.add_request(
        "Owned Already", "cole", media_type="tv", resolved_title="Owned Already",
        external_id="tv-300", status=queue_store.STATUS_FULFILLED,
        identity_source="tmdb", canonical_year=2020, season=2)
    monkeypatch.setattr(maintenance, "request_present_in_library", lambda req: True)

    ctx = _confirm_ctx(show, "tv")
    _run(request_flow.handle_confirmation(
        _FakeUpdate(callback_query=_FakeQuery("req_confirm_yes", _FakeMessage())), ctx))

    # Force-grab the already-owned season by typing its number, bypassing the
    # keyboard's own owned/missing split.
    text_msg = _FakeMessage("2")
    state = _run(request_flow.handle_season_text(_FakeUpdate(message=text_msg), ctx))
    assert state == request_flow.ConversationHandler.END

    reply = text_msg.replies[-1].lower()
    assert "already" in reply and "s02" in reply
    assert "nothing new queued" in reply
    assert "queued 1" not in reply  # not reported as a fresh add

    rows = [r for r in _rows() if r.season == 2]
    assert len(rows) == 1 and rows[0].request_id == existing.request_id


def test_finish_season_flow_copy_has_no_em_dash():
    """House rule: no em dashes in user-facing copy. Covers both buckets of
    the season-flow closing message (added + already-have)."""
    ctx = _Ctx({
        request_flow._UD_MEDIA_TYPE: "tv",
        request_flow._UD_SEASON_ADDED: ["Show A: S02"],
        request_flow._UD_SEASON_ALREADY_HAVE: ["Show A S03: already in your library, nothing new queued."],
    })
    msg = _FakeMessage()
    state = _run(request_flow._finish_season_flow(
        _FakeUpdate(callback_query=_FakeQuery("noop", msg)), ctx))
    assert state == request_flow.ConversationHandler.END
    assert "—" not in msg.replies[-1]
    assert "Queued 1 season" in msg.replies[-1]
    assert "Already had 1 season" in msg.replies[-1]


def test_selecting_seasons_state_routes_new_callback_patterns_to_handler():
    """Dispatch through REQUEST_CONV_HANDLER's own state map (not a direct
    function call) so a dropped/narrowed pattern registration fails this test
    instead of silently dead-ending a button in production."""
    import telegram

    handlers = request_flow.REQUEST_CONV_HANDLER.states[request_flow.SELECTING_SEASONS]
    callback_handlers = [
        h for h in handlers
        if isinstance(h, request_flow.CallbackQueryHandler)
    ]

    def _route_for(data: str):
        user = telegram.User(id=1, first_name="cole", is_bot=False)
        cq = telegram.CallbackQuery(id="1", from_user=user, chat_instance="1", data=data)
        update = telegram.Update(update_id=1, callback_query=cq)
        matched = [h for h in callback_handlers if h.check_update(update)]
        return matched

    for data in ("req_season_owned_3", "req_season_keep_updated",
                "req_season_anime_add", "req_season_anime_keep_updated"):
        matched = _route_for(data)
        assert len(matched) == 1, f"{data!r} matched {len(matched)} handlers, expected exactly 1"
        assert matched[0].callback is request_flow.handle_season_selection, (
            f"{data!r} did not route to handle_season_selection")

    # The cancel button must NOT be swallowed by the season-callback handler.
    cancel_matches = _route_for("req_cancel")
    assert len(cancel_matches) == 1
    assert cancel_matches[0].callback is request_flow.cancel_request_flow
