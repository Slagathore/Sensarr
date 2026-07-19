# =============================================================================
# Fix-sprint sections E + H (both live in desktop_app.py):
#
# E. Editable search in resolve identity. resolve_request_by_id used to
#    derive a query once and dead-end on "No matches"; the picker
#    (_pick_from_list) was click-only and never passed media_type, so the
#    anime/xanime branches of request_intake.search_candidates were
#    unreachable from any UI. Fixed with one shared dialog
#    (_resolve_identity_dialog) that both resolve_request_by_id and
#    _show_candidate_picker now drive, backed by the pure, Tk-free
#    _resolve_dialog_search retry helper.
#
# H. Unsquish the tabs. Default geometry/minsize widened, a tighter
#    TNotebook.Tab style configured after sv_ttk.set_theme, "Watchlist/Recs"
#    shortened to "Watchlist".
#
# DesktopApp is a Tk GUI class the sprint rules forbid instantiating headless
# (no display in CI). Importing desktop_app and inspecting the CLASS touches
# no Tk at all (test_maint_wiring.py established this pattern first) --
# these tests use three techniques from that file: (1) attribute/callable
# existence guards, (2) source-level assertions on method bodies proving
# wiring and ordering, and (3) driving an UNBOUND method against a minimal
# stand-in object (_FakePickerApp below) that stubs only the one Tk-touching
# call (_resolve_identity_dialog) so the surrounding decision logic -- which
# candidates and index get handed to request_intake -- runs for real. The
# dialog's actual on-screen behaviour (typing in the Entry, clicking Search
# again, seeing "No matches" render inline, tab strip pixel widths) needs a
# live display and is called out as unverified in the sprint report, not
# faked here.
# =============================================================================

import inspect
import json

import pytest

import desktop_app
import media_lookup
import queue_store
import request_intake
from media_lookup import MediaResult


def _src(name: str) -> str:
    return inspect.getsource(getattr(desktop_app.DesktopApp, name))


# ---------------------------------------------------------------------------
# E item 2 — search_candidates media-type routing (backend unchanged per the
# fix plan, but nothing exercised the anime/xanime branches before this
# sprint since no UI ever passed media_type). Pinned directly against
# request_intake.search_candidates with the provider calls mocked, matching
# the monkeypatch-on-media_lookup pattern used throughout test_task_a_intake.py.
# ---------------------------------------------------------------------------

def _boom(*_a, **_k):
    raise AssertionError("a provider outside the requested media_type ran")


def test_search_candidates_anime_routes_to_jikan_only(monkeypatch):
    calls = []

    def fake_jikan(title, *, explicit=False, limit=5):
        calls.append((title, explicit))
        return [MediaResult(title=title, year=None, external_id="1",
                            external_url="", media_type="anime",
                            overview="", source="jikan")]

    monkeypatch.setattr(media_lookup, "search_jikan_anime", fake_jikan)
    monkeypatch.setattr(media_lookup, "search_tmdb_movies", _boom)
    monkeypatch.setattr(media_lookup, "search_tvdb_shows", _boom)
    monkeypatch.setattr(media_lookup, "search_tmdb_shows", _boom)

    out = request_intake.search_candidates("Naruto", media_type="anime")

    assert calls == [("Naruto", False)]
    assert out and out[0].media_type == "anime"


def test_search_candidates_xanime_routes_to_jikan_explicit(monkeypatch):
    calls = []

    def fake_jikan(title, *, explicit=False, limit=5):
        calls.append((title, explicit))
        return []

    monkeypatch.setattr(media_lookup, "search_jikan_anime", fake_jikan)
    monkeypatch.setattr(media_lookup, "search_tmdb_movies", _boom)
    monkeypatch.setattr(media_lookup, "search_tvdb_shows", _boom)
    monkeypatch.setattr(media_lookup, "search_tmdb_shows", _boom)

    request_intake.search_candidates("Kanojo", media_type="xanime")

    assert calls == [("Kanojo", True)]


def test_search_candidates_movie_type_never_touches_tv_or_anime(monkeypatch):
    calls = []

    def fake_movies(title, year, *, limit=5):
        calls.append(title)
        return []

    monkeypatch.setattr(media_lookup, "search_tmdb_movies", fake_movies)
    monkeypatch.setattr(media_lookup, "search_tvdb_shows", _boom)
    monkeypatch.setattr(media_lookup, "search_tmdb_shows", _boom)
    monkeypatch.setattr(media_lookup, "search_jikan_anime", _boom)

    request_intake.search_candidates("Dune", media_type="movie")

    assert calls == ["Dune"]


def test_search_candidates_default_still_searches_movie_and_tv(monkeypatch):
    """Regression pin: media_type=None (the "auto" dialog choice) must keep
    searching both movie and tv, exactly as it did before media_type existed
    as a routable parameter."""
    seen = []
    monkeypatch.setattr(media_lookup, "search_tmdb_movies",
                        lambda title, year, *, limit=5: seen.append("movie") or [])
    monkeypatch.setattr(media_lookup, "search_tvdb_shows",
                        lambda title, year, *, limit=5: seen.append("tv") or [])
    monkeypatch.setattr(media_lookup, "search_jikan_anime", _boom)

    request_intake.search_candidates("Severance")

    assert seen == ["movie", "tv"]


# ---------------------------------------------------------------------------
# E — _resolve_dialog_search: the pure query/retry logic factored out of the
# Tk dialog so it's unit testable without a display.
# ---------------------------------------------------------------------------

def test_resolve_dialog_search_blank_query_short_circuits():
    calls = []
    cands, err = desktop_app._resolve_dialog_search(
        "   ", "movie", search_fn=lambda *a, **k: calls.append(a) or [])
    assert cands == [] and err is None
    assert calls == [], "blank query must never reach the network"


def test_resolve_dialog_search_forwards_query_and_media_type():
    seen = {}

    def fake(q, mt):
        seen["q"], seen["mt"] = q, mt
        return ["r1", "r2"]

    cands, err = desktop_app._resolve_dialog_search(
        "  Inception  ", "movie", search_fn=fake)
    # Caller strips before forwarding.
    assert seen == {"q": "Inception", "mt": "movie"}
    assert cands == ["r1", "r2"] and err is None


def test_resolve_dialog_search_none_media_type_is_the_auto_choice():
    seen = {}
    desktop_app._resolve_dialog_search(
        "Andor", None, search_fn=lambda q, mt: seen.setdefault("mt", mt))
    assert seen["mt"] is None


def test_resolve_dialog_search_empty_result_is_not_an_error():
    cands, err = desktop_app._resolve_dialog_search(
        "Nonexistent Title Xyz", "movie", search_fn=lambda q, mt: [])
    assert cands == [] and err is None


def test_resolve_dialog_search_none_result_normalises_to_empty_list():
    cands, err = desktop_app._resolve_dialog_search(
        "Andor", "tv", search_fn=lambda q, mt: None)
    assert cands == [] and err is None


def test_resolve_dialog_search_never_raises_on_provider_exception():
    def blows_up(q, mt):
        raise RuntimeError("network is down")

    cands, err = desktop_app._resolve_dialog_search(
        "Andor", "tv", search_fn=blows_up)
    assert cands == []
    assert err and "try again" in err


def test_resolve_dialog_search_defaults_to_request_intake_search_candidates(
        monkeypatch):
    """No explicit search_fn -> the dialog hits the same backend entry point
    the rest of intake uses (Task E item 4: backend unchanged)."""
    called = {}
    monkeypatch.setattr(request_intake, "search_candidates",
                        lambda q, mt: called.setdefault("args", (q, mt)) or [])
    desktop_app._resolve_dialog_search("Severance", "tv")
    assert called["args"] == ("Severance", "tv")


# ---------------------------------------------------------------------------
# E — wiring: both flows share the one dialog; no dead-end popup remains.
# ---------------------------------------------------------------------------

def test_resolve_identity_dialog_exists_and_is_callable():
    assert callable(desktop_app.DesktopApp._resolve_identity_dialog)


def test_resolve_request_by_id_uses_the_shared_dialog_not_pick_from_list():
    src = _src("resolve_request_by_id")
    assert "self._resolve_identity_dialog(" in src
    assert "self._pick_from_list(" not in src, (
        "resolve_request_by_id reverted to the click-only one-shot picker")


def test_resolve_request_by_id_never_dead_ends_on_no_matches():
    """The old body short-circuited with a 'No matches' info popup and
    returned before any picker ever opened -- Task E's core complaint.
    The dialog itself must be what's asked to search, not a pre-check that
    bails before it opens."""
    src = _src("resolve_request_by_id")
    assert "No matches" not in src
    assert "search_candidates(" not in src, (
        "resolve_request_by_id must delegate the lookup to the dialog "
        "(which loops on retry), not do a one-shot lookup up front")


def test_resolve_request_by_id_seeds_media_type_from_the_request():
    """Task E item 2 — passing the request's own media_type is what makes
    the anime/xanime branches reachable instead of always defaulting to
    movie+tv."""
    src = _src("resolve_request_by_id")
    assert "media_type=req.media_type" in src


def test_show_candidate_picker_uses_the_shared_dialog():
    src = _src("_show_candidate_picker")
    assert "self._resolve_identity_dialog(" in src
    assert "self._pick_from_list(" not in src
    assert "include_none_of_these=True" in src


def test_pick_from_list_removed_as_dead_code():
    """Both call sites now go through _resolve_identity_dialog -- the old
    click-only picker had zero remaining callers, so it's gone rather than
    left as unreferenced dead code."""
    assert not hasattr(desktop_app.DesktopApp, "_pick_from_list")


class _FakeVar:
    """Stand-in for a tk.StringVar -- only .set()/.get() are ever touched
    by _show_candidate_picker, so a plain recorder is a faithful substitute
    without a live Tk root (same rationale as test_maint_wiring.py's
    _FakeCheckVar)."""

    def __init__(self):
        self.values: list = []

    def set(self, value) -> None:
        self.values.append(value)

    def get(self):
        return self.values[-1] if self.values else ""


class _FakePickerApp:
    """Minimal stand-in for DesktopApp exercising the UNBOUND
    _show_candidate_picker method. Stubs only _resolve_identity_dialog (the
    one call that would otherwise open a real Tk Toplevel) with a canned
    return value; everything downstream of that -- which candidate list and
    index get hand to request_intake.add_picked_candidate, the status/last
    -action text, refresh_requests() -- runs as the real method body."""

    def __init__(self, dialog_result):
        self._dialog_result = dialog_result
        self.request_content_var = _FakeVar()
        self.last_action_var = _FakeVar()
        self.status_var = _FakeVar()
        self.refresh_called = False

    def _resolve_identity_dialog(self, *_args, **_kwargs):
        return self._dialog_result

    def refresh_requests(self) -> None:
        self.refresh_called = True


def _mafs_pair(media_type: str = "movie"):
    """Two same-title, different-country siblings -- the exact MAFS AU vs
    US shape build_aliases' disambiguation exists for. media_type defaults
    to 'movie' so the test doesn't also have to stub the TV season prompt
    (tkinter.simpledialog.askstring), which is orthogonal to this bug."""
    au = MediaResult(title="Married At First Sight", year=2015,
                     external_id="100", external_url="", media_type=media_type,
                     overview="", source="tvdb", origin_countries=("AU",))
    us = MediaResult(title="Married At First Sight", year=2014,
                     external_id="200", external_url="", media_type=media_type,
                     overview="", source="tvdb", origin_countries=("US",))
    return au, us


def test_show_candidate_picker_passes_full_sibling_list_and_correct_index(
        monkeypatch):
    """The regression this pins: a real pick among 2+ same-title candidates
    must reach add_picked_candidate with the FULL sibling list (not just the
    winner collapsed into a 1-element list) and the winner's real index in
    it. add_picked_candidate derives candidate_titles from every row of the
    list it's handed (request_intake.py ~511-525), and build_aliases only
    appends the disambiguating " <CC>" suffix when >=2 siblings share a base
    title (request_intake.py:95) -- collapsing to [match] made that
    unreachable from the desktop UI for every single pick, silently killing
    MAFS-AU-vs-US-style disambiguation."""
    au, us = _mafs_pair()
    siblings = [au, us]
    # The user picked the US edition (index 1 in the sibling list).
    app = _FakePickerApp((us, False, siblings))

    captured = {}

    def fake_add_picked_candidate(content, requester, candidates, choice_index,
                                  *, seasons=None):
        captured["candidates"] = candidates
        captured["choice_index"] = choice_index
        return request_intake.IntakeResult((1,), "open")

    monkeypatch.setattr(request_intake, "add_picked_candidate",
                        fake_add_picked_candidate)

    desktop_app.DesktopApp._show_candidate_picker(
        app, "Married At First Sight", "Admin", [au, us])  # type: ignore[arg-type]

    assert captured["candidates"] == siblings, (
        "add_picked_candidate must receive the full sibling list, not a "
        "single-element [match] list -- disambiguation needs every sibling")
    assert captured["choice_index"] == 1
    assert captured["candidates"][captured["choice_index"]] is us
    assert app.refresh_called


@pytest.fixture
def _clean_requests_table():
    """Scoped to just the one test below that needs a real DB round-trip --
    every other test in this file stays pure/mocked, matching the rest of
    the file's no-DB style."""
    import db
    queue_store.initialize_queue_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM requests")
        conn.commit()
    yield
    with db.connect() as conn:
        conn.execute("DELETE FROM requests")
        conn.commit()


def test_show_candidate_picker_disambiguation_actually_builds_country_alias(
        _clean_requests_table):
    """Belt-and-suspenders on the same regression, driven through the real
    (unmocked) add_picked_candidate -> add_matched_request -> build_aliases
    pipeline and a real DB round-trip, so the fix is proven at the alias
    string the sibling check exists to produce (request_intake.py:95-100),
    not just at the argument-shape level the test above pins."""
    au, us = _mafs_pair()  # movie: no TV season prompt to stub
    siblings = [au, us]
    app = _FakePickerApp((us, False, siblings))  # user picked the US edition

    desktop_app.DesktopApp._show_candidate_picker(
        app, "Married At First Sight", "Admin", [au, us])  # type: ignore[arg-type]

    rows = [r for r in queue_store.list_requests(status="all", limit=50)
           if r.external_id == "200"]
    assert rows, "picker never reached the store layer -- no request landed"
    aliases = json.loads(rows[0].aliases_json or "[]")
    assert any(a.endswith(" US") for a in aliases), (
        f"expected a country-disambiguated alias ending in ' US', got "
        f"{aliases!r} -- the >=2-sibling check never fired, meaning "
        "add_picked_candidate got a collapsed single-candidate list again")


def test_dialog_search_again_rewires_to_the_pure_retry_helper():
    src = _src("_resolve_identity_dialog")
    assert "_resolve_dialog_search(" in src
    assert "Search again" in src
    # Cancel via the window's [x] must resolve the same way as the Cancel
    # button -- never leave the dialog silently unresolved.
    assert 'win.protocol("WM_DELETE_WINDOW", _cancel)' in src
    # "No matches" is rendered into the dialog's own status label, never a
    # popup -- the literal Task E requirement that a miss never dead-ends.
    assert "No matches" in src
    assert "_show_info(" not in src and "_show_warning(" not in src


def test_dialog_offers_the_anime_and_xanime_type_choices():
    assert "anime" in desktop_app._RESOLVE_DIALOG_TYPES
    assert "xanime" in desktop_app._RESOLVE_DIALOG_TYPES


# ---------------------------------------------------------------------------
# H item 1 — default geometry / minsize.
# ---------------------------------------------------------------------------

def test_default_geometry_widened_past_the_old_squish_point():
    src = _src("__init__")
    assert 'self.root.geometry("1100x720")' in src
    assert "self.root.minsize(900, 600)" in src
    assert 'self.root.geometry("860x620")' not in src
    assert "self.root.minsize(760, 520)" not in src


# ---------------------------------------------------------------------------
# H item 2 — custom TNotebook.Tab style, configured after the theme so it
# isn't clobbered by sv_ttk.set_theme's own style setup.
# ---------------------------------------------------------------------------

def test_notebook_tab_style_configured_after_theme_is_set():
    src = _src("__init__")
    assert 'style.configure("TNotebook.Tab"' in src
    theme_idx = src.index('sv_ttk.set_theme("dark")')
    tab_style_idx = src.index('style.configure("TNotebook.Tab"')
    assert theme_idx < tab_style_idx, (
        "TNotebook.Tab padding must be configured AFTER sv_ttk.set_theme, "
        "or theme setup clobbers the override")


# ---------------------------------------------------------------------------
# H item 3 — "Watchlist/Recs" shortened to "Watchlist" everywhere it names
# the tab (the tab label itself, plus the two help-text references to it).
# ---------------------------------------------------------------------------

def test_watchlist_tab_renamed():
    src = _src("_build_ui")
    assert 'notebook.add(watchlist_tab, text="Watchlist")' in src


def test_no_stale_watchlist_recs_label_remains():
    import pathlib
    text = pathlib.Path(desktop_app.__file__).read_text(encoding="utf-8")
    assert "Watchlist/Recs" not in text
