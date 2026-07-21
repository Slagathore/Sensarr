# =============================================================================
# tests/test_status_strip.py
# =============================================================================
# Task L — Status visibility rework.
#
# Same split as tests/test_maint_wiring.py (its docstring explains why): a
# real Tk root cannot be created in this repo's tests (headless CI has no
# display — confirmed locally too: `tkinter.StringVar()` raises "Too early
# to create variable: no default root window" without one, and CI only
# installs the pytest-safe subset of requirements.txt). So this file proves
# three different things with three different techniques:
#
#   1. activity_strip.ActivityModel is plain Python with zero tkinter
#      import — genuine behavioral pure-unit tests, real fade/persist/level
#      logic, fake clocks instead of real sleeps.
#   2. ui_helpers' new helpers (ellipsize/mirror_ellipsized/begin_busy) are
#      exercised behaviorally against minimal fake stand-ins for the Tk
#      objects they touch (tk.StringVar, a ttk.Button) — the same
#      "duck-typed fake, never a real widget" approach test_maint_wiring.py
#      uses for tk.BooleanVar (_FakeCheckVar).
#   3. desktop_app.DesktopApp's Tk-rendering methods (_activity_render,
#      _activity_on_click, _on_maint_activity_mirror, post_activity) are
#      called UNBOUND against minimal fake `self` objects — real production
#      code, fake receiver — for everything reachable without an actual
#      widget; the two spots that truly need one (_build_ui's row layout,
#      _activity_open_recent's tk.Menu) fall back to source-level
#      assertions, exactly test_maint_wiring.py's technique.
#
# Anything only checkable on an actual rendered window (does the strip
# visually sit where expected, does the color read correctly on sv-ttk dark,
# does clicking really switch tabs) is out of reach here and is called out
# as unverified in the sprint report instead of faked.
# =============================================================================
import inspect
import re
from typing import Callable

import pytest

import activity_strip
import desktop_app
import grab_queue_tab
import shows_tab
import ui_helpers
import watchlist_tab


def _src(obj) -> str:
    return inspect.getsource(obj)


# =============================================================================
# 1. activity_strip.ActivityModel — pure unit tests (fade / persist / levels
#    / history). No tkinter import anywhere in activity_strip.py.
# =============================================================================

class _FakeClock:
    """Monotonic-clock stand-in a test can advance deliberately instead of
    sleeping for real."""
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _model(**kwargs) -> tuple[activity_strip.ActivityModel, _FakeClock]:
    clock = _FakeClock()
    model = activity_strip.ActivityModel(clock=clock, **kwargs)
    return model, clock


def test_post_returns_entry_with_the_given_fields():
    model, _clock = _model()
    entry = model.post("Shows", "Scanning…", level="working", tab="Shows")
    assert entry.source == "Shows"
    assert entry.message == "Scanning…"
    assert entry.level == "working"
    assert entry.tab == "Shows"
    assert model.current() is entry


def test_post_rejects_an_unknown_level():
    model, _clock = _model()
    with pytest.raises(ValueError):
        model.post("Shows", "oops", level="bogus")


def test_working_entry_never_auto_expires():
    model, clock = _model(fade_seconds=10.0)
    model.post("Shows", "Scanning…", level="working")
    clock.advance(10_000.0)  # absurdly long — a working entry never fades
    entry = model.current()
    assert entry is not None
    assert entry.level == "working"


def test_success_entry_fades_after_the_configured_window():
    model, clock = _model(fade_seconds=10.0)
    model.post("Shows", "Done", level="success")
    clock.advance(9.9)
    assert model.current() is not None, "must still be showing just under the fade window"
    clock.advance(0.2)
    assert model.current() is None, "must be gone once the fade window has passed"


def test_error_entry_persists_past_the_fade_window_until_dismissed():
    model, clock = _model(fade_seconds=10.0)
    model.post("Maintenance", "Reindex FAILED", level="error")
    clock.advance(10_000.0)
    assert model.current() is not None, "errors stay until clicked, not until they fade"
    model.dismiss_current()
    assert model.current() is None


def test_warning_entry_persists_like_error_until_dismissed():
    model, clock = _model(fade_seconds=10.0)
    model.post("Maintenance", "Cancelled", level="warning")
    clock.advance(10_000.0)
    assert model.current() is not None
    model.dismiss_current()
    assert model.current() is None


def test_dismiss_current_is_a_safe_no_op_with_nothing_showing():
    model, _clock = _model()
    model.dismiss_current()  # must not raise
    assert model.current() is None


def test_a_new_post_always_replaces_the_current_entry_even_a_sticky_one():
    model, clock = _model(fade_seconds=10.0)
    model.post("Maintenance", "Reindex FAILED", level="error")
    clock.advance(1.0)
    model.post("Shows", "Scanning…", level="working")
    current = model.current()
    assert current is not None
    assert current.source == "Shows"
    assert current.level == "working"


def test_history_is_newest_first_and_capped_at_the_limit():
    model, clock = _model(history_limit=12)
    for i in range(15):
        model.post("Shows", f"event {i}", level="working")
        clock.advance(1.0)
    history = model.history()
    assert len(history) == 12
    # Newest first: the very last post() call ("event 14") leads.
    assert history[0].message == "event 14"
    assert history[-1].message == "event 3"


def test_history_survives_after_the_entry_itself_has_faded_off_the_strip():
    """Item 5's whole point: a missed status is recoverable from history
    even after current() has gone back to None."""
    model, clock = _model(fade_seconds=10.0)
    model.post("Watchlist", "12 watchlist item(s)", level="success")
    clock.advance(20.0)
    assert model.current() is None
    history = model.history()
    assert len(history) == 1
    assert history[0].message == "12 watchlist item(s)"


def test_history_limit_argument_narrows_further():
    model, clock = _model(history_limit=12)
    for i in range(5):
        model.post("Shows", f"event {i}", level="working")
    assert len(model.history(limit=2)) == 2


# =============================================================================
# 2. ui_helpers — ellipsize / mirror_ellipsized / begin_busy / the
#    callable-tooltip support add_tooltip gained.
# =============================================================================

def test_ellipsize_short_text_passes_through_unchanged():
    assert ui_helpers.ellipsize("hello", 10) == "hello"


def test_ellipsize_text_at_exactly_the_limit_is_unchanged():
    assert ui_helpers.ellipsize("1234567890", 10) == "1234567890"


def test_ellipsize_long_text_is_truncated_with_a_single_ellipsis():
    result = ui_helpers.ellipsize("a" * 20, 10)
    assert result.endswith("…")
    assert len(result) == 10


def test_ellipsize_strips_trailing_whitespace_at_the_cut_point():
    result = ui_helpers.ellipsize("hello   world", 8)
    assert result == "hello…"


def test_ellipsize_handles_empty_and_none_input():
    assert ui_helpers.ellipsize("", 10) == ""
    assert ui_helpers.ellipsize(None, 10) == ""


def test_ellipsize_degenerate_max_chars():
    assert ui_helpers.ellipsize("hello", 1) == "…"
    assert ui_helpers.ellipsize("hello", 0) == ""


class _FakeTkVar:
    """Stand-in for tk.StringVar — no Tcl interpreter/root needed (see the
    module docstring: this repo's tests never construct real Tk objects)."""
    def __init__(self, value: str = "") -> None:
        self._value = value
        self._traces: list = []

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value

    def trace_add(self, mode, callback) -> None:
        self._traces.append((mode, callback))

    def fire_traces(self) -> None:
        for _mode, cb in self._traces:
            cb()


def test_mirror_ellipsized_initial_value_is_already_ellipsized(monkeypatch):
    monkeypatch.setattr(ui_helpers.tk, "StringVar", _FakeTkVar)
    source = _FakeTkVar("x" * 20)
    display = ui_helpers.mirror_ellipsized(source, 10)
    assert display.get().endswith("…")
    assert len(display.get()) == 10


def test_mirror_ellipsized_stays_in_sync_when_the_source_var_changes(monkeypatch):
    monkeypatch.setattr(ui_helpers.tk, "StringVar", _FakeTkVar)
    source = _FakeTkVar("short")
    display = ui_helpers.mirror_ellipsized(source, 10)
    assert display.get() == "short"

    source.set("a brand new, much longer status line than before")
    source.fire_traces()  # simulates what a real Tk trace fires automatically
    assert display.get() != "short"
    assert len(display.get()) <= 10
    assert display.get().endswith("…")


def test_mirror_ellipsized_registers_exactly_one_write_trace(monkeypatch):
    monkeypatch.setattr(ui_helpers.tk, "StringVar", _FakeTkVar)
    source = _FakeTkVar("short")
    ui_helpers.mirror_ellipsized(source, 10)
    assert len(source._traces) == 1
    assert source._traces[0][0] == "write"


class _FakeButton:
    """Duck-typed stand-in for ttk.Button — cget/state/configure only,
    exactly what begin_busy touches."""
    def __init__(self, text: str = "Go") -> None:
        self._text = text
        self.disabled = False

    def cget(self, key: str):
        assert key == "text"
        return self._text

    def state(self, statespec) -> None:
        for s in statespec:
            if s == "disabled":
                self.disabled = True
            elif s == "!disabled":
                self.disabled = False

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self._text = kwargs["text"]


class _DyingButton:
    """A widget that no longer exists (its dialog got closed mid-run) —
    every call raises TclError, exactly like a real destroyed Tk widget."""
    def cget(self, key):
        raise ui_helpers.tk.TclError("no such widget")

    def state(self, statespec):
        raise ui_helpers.tk.TclError("no such widget")

    def configure(self, **kwargs):
        raise ui_helpers.tk.TclError("no such widget")


def test_begin_busy_with_none_is_a_harmless_no_op():
    restore = ui_helpers.begin_busy(None, working_text="Busy…")
    restore()  # must not raise
    restore()  # idempotent


def test_begin_busy_disables_and_relabels_then_restores():
    btn = _FakeButton("Scan Folders")
    restore = ui_helpers.begin_busy([btn], working_text="Scanning…")  # type: ignore[arg-type]
    assert btn.disabled is True
    assert btn._text == "Scanning…"

    restore()
    assert btn.disabled is False
    assert btn._text == "Scan Folders"


def test_begin_busy_restore_is_idempotent():
    btn = _FakeButton("Go")
    restore = ui_helpers.begin_busy([btn], working_text="Busy…")  # type: ignore[arg-type]
    restore()
    restore()  # second call must not re-toggle or raise
    assert btn.disabled is False
    assert btn._text == "Go"


def test_begin_busy_without_working_text_only_disables():
    btn = _FakeButton("Go")
    restore = ui_helpers.begin_busy([btn])  # type: ignore[arg-type]
    assert btn.disabled is True
    assert btn._text == "Go"  # label untouched
    restore()
    assert btn.disabled is False


def test_begin_busy_covers_multiple_buttons_at_once():
    a, b = _FakeButton("A"), _FakeButton("B")
    restore = ui_helpers.begin_busy([a, b], working_text="Working…")  # type: ignore[arg-type]
    assert a.disabled and b.disabled
    assert a._text == b._text == "Working…"
    restore()
    assert not a.disabled and not b.disabled
    assert a._text == "A" and b._text == "B"


def test_begin_busy_swallows_tclerror_from_an_already_destroyed_widget():
    restore = ui_helpers.begin_busy([_DyingButton()], working_text="Busy…")  # type: ignore[arg-type]
    restore()  # neither call may raise


def test_begin_busy_accepts_a_single_button_via_isinstance(monkeypatch):
    """The non-list call shape (begin_busy(one_button, ...)) relies on
    isinstance(buttons, ttk.Button) — proven here by monkeypatching that one
    name so a fake object satisfies it, without constructing a real Tk
    widget."""
    monkeypatch.setattr(ui_helpers.ttk, "Button", _FakeButton)
    btn = _FakeButton("Pull Watchlist")
    restore = ui_helpers.begin_busy(btn, working_text="Pulling…")  # type: ignore[arg-type]
    assert btn.disabled is True
    assert btn._text == "Pulling…"
    restore()
    assert btn.disabled is False
    assert btn._text == "Pull Watchlist"


def test_add_tooltip_supports_a_callable_text_source():
    """Hovering re-reads a callable's current value instead of a frozen
    string (Task L item 3's dynamic tooltip for an ellipsized status label).
    Actually showing the tooltip needs a real Toplevel; the branch that
    decides whether to call text() is checked at the source level, the same
    boundary test_maint_wiring.py draws for anything needing a live widget.
    """
    src = _src(ui_helpers.add_tooltip)
    assert "callable(text)" in src


# =============================================================================
# 3a. desktop_app — _build_ui source-level placement assertions.
#
# Verification round 1 shipped a bug here: the strip's row renumbering
# moved the notebook to row=4 but missed `summary` (the bottom status bar,
# built at the very end of _build_ui), which was still hardcoded to row=4
# too — notebook and summary silently occupied the same grid cell. Round 2
# also corrected the strip's position: Cole's own words are "in between the
# overall app status action and right above the donate line", i.e. the
# strip sits ABOVE the ko-fi row, not below it as the bootstrap's paraphrase
# implied. Both fixes are pinned below against a HARDCODED expected order
# (not derived from the source under test) so this can't quietly re-agree
# with a future regression the way a same-source comparison could.
# =============================================================================

# Cole's own placement, hardcoded — this is the intent _build_ui's grid
# calls must match, top to bottom.
_EXPECTED_ROOT_ROW_ORDER = [
    ("header", 0),
    ("activity_row", 1),
    ("kofi_row", 2),
    ("actions", 3),
    ("notebook", 4),
    ("summary", 5),
]


def test_build_ui_root_rows_match_coles_exact_placement():
    """'in between the overall app status action and right above the
    donate line' -> header, THEN the strip, THEN the ko-fi row."""
    src = _src(desktop_app.DesktopApp._build_ui)
    snippets = [f"{name}.grid(row={row}, column=0"
                for name, row in _EXPECTED_ROOT_ROW_ORDER]
    for snippet in snippets:
        assert snippet in src, f"missing or renumbered: {snippet}"
    # Source order mirrors build/grid order here (each row is built in
    # sequence) — this pins the strip strictly between the header and the
    # ko-fi row, not merely present somewhere.
    indices = [src.index(snippet) for snippet in snippets]
    assert indices == sorted(indices), (
        "the six root-level rows are built out of Cole's intended "
        "top-to-bottom order")


def test_build_ui_root_rows_are_all_distinct_no_collisions():
    """Regression test for the exact bug verification caught: every direct
    grid child of self.root (header/activity_row/kofi_row/actions/notebook/
    summary) must own a UNIQUE row number, full stop — this would have
    failed against the shipped bug (notebook and summary both row=4)."""
    src = _src(desktop_app.DesktopApp._build_ui)
    rows_seen = []
    for name, _expected in _EXPECTED_ROOT_ROW_ORDER:
        match = re.search(rf"{name}\.grid\(row=(\d+), column=0", src)
        assert match, f"{name} has no grid(row=..., column=0) call"
        rows_seen.append(int(match.group(1)))
    assert len(rows_seen) == len(set(rows_seen)), (
        f"root grid rows collide: {list(zip(_EXPECTED_ROOT_ROW_ORDER, rows_seen))}")


def test_build_ui_activity_strip_is_a_root_sibling_not_inside_the_notebook():
    """Always visible regardless of active tab: activity_row grids straight
    onto self.root, the same parent as the notebook, header, and ko-fi
    row — never onto a notebook tab frame."""
    src = _src(desktop_app.DesktopApp._build_ui)
    assert "activity_row = ttk.Frame(self.root" in src


def test_build_ui_root_row_weight_follows_the_notebook_row():
    """The stretching row must track wherever the notebook actually is —
    row 4 in Cole's placement, same number whether or not the strip's own
    row happens to change again later."""
    src = _src(desktop_app.DesktopApp._build_ui)
    assert "self.root.rowconfigure(4, weight=1)" in src
    assert "notebook.grid(row=4, column=0" in src


def test_build_ui_captures_the_apply_selected_button_for_the_busy_guard():
    src = _src(desktop_app.DesktopApp._build_ui)
    assert 'if text == "Apply Selected":' in src
    assert "self._maint_apply_btn = btn" in src


def test_build_ui_recent_button_tooltip_has_no_em_dash():
    """Verification item 5: plain copy, no em dash."""
    src = _src(desktop_app.DesktopApp._build_ui)
    tooltip_start = src.index('"The last activity entries')
    tooltip_end = src.index(")", tooltip_start)
    assert "—" not in src[tooltip_start:tooltip_end]


# =============================================================================
# 3b. desktop_app — registry mirror subscription (item 2).
# =============================================================================

def test_maint_registry_mirror_is_subscribed_exactly_once_alongside_the_original():
    src = _src(desktop_app.DesktopApp.__init__)
    assert "self._maint_registry.subscribe(self._on_maint_job_event)" in src
    assert src.count("self._maint_registry.subscribe(self._on_maint_activity_mirror)") == 1
    # The mirror subscribes AFTER the original listener, same relative order
    # the bootstrap describes ("a SECOND listener").
    assert (src.index("self._maint_registry.subscribe(self._on_maint_job_event)")
            < src.index("self._maint_registry.subscribe(self._on_maint_activity_mirror)"))


def test_post_activity_marshals_onto_the_ui_thread():
    src = _src(desktop_app.DesktopApp.post_activity)
    assert "self._post_to_ui(" in src
    assert "_activity_post_ui" in src


class _FakeJob:
    def __init__(self, label: str, state: str | None = None) -> None:
        self.label = label
        self.state = state


class _ActivityFakeApp:
    """Runs the REAL post_activity/_activity_post_ui against a REAL
    ActivityModel; only the two leaves that touch actual Tk widgets
    (_activity_render/_activity_start_tick) are stubbed. Same technique as
    test_maint_wiring.py's _FakeMaintApp: call the unbound production method
    with a minimal fake `self`."""
    post_activity = desktop_app.DesktopApp.post_activity
    _activity_post_ui = desktop_app.DesktopApp._activity_post_ui

    def __init__(self) -> None:
        self._activity_model = activity_strip.ActivityModel()
        self.render_calls = 0
        self.tick_started = False

    def _post_to_ui(self, fn, *, delay_ms=0):
        fn()
        return True

    def _activity_render(self) -> None:
        self.render_calls += 1

    def _activity_start_tick(self) -> None:
        self.tick_started = True


def test_post_activity_end_to_end_updates_the_real_model_and_renders():
    app = _ActivityFakeApp()
    app.post_activity("Shows", "Scanning…", level="working", tab="Shows")
    entry = app._activity_model.current()
    assert entry is not None
    assert (entry.source, entry.message, entry.level, entry.tab) == (
        "Shows", "Scanning…", "working", "Shows")
    assert app.render_calls == 1
    assert app.tick_started is True


def test_maint_activity_mirror_posts_working_on_started():
    app = _ActivityFakeApp()
    desktop_app.DesktopApp._on_maint_activity_mirror(
        app, "started", _FakeJob("Clean Junk"))  # type: ignore[arg-type]
    entry = app._activity_model.current()
    assert entry is not None
    assert entry.level == "working"
    assert "Clean Junk" in entry.message
    assert entry.tab == "Maintenance"


@pytest.mark.parametrize("state, expected_level", [
    ("done", "success"),
    ("cancelled", "warning"),
    ("failed", "error"),
])
def test_maint_activity_mirror_maps_finished_states_to_levels(state, expected_level):
    app = _ActivityFakeApp()
    desktop_app.DesktopApp._on_maint_activity_mirror(
        app, "finished", _FakeJob("Sanitize Names", state=state))  # type: ignore[arg-type]
    entry = app._activity_model.current()
    assert entry is not None
    assert entry.level == expected_level
    assert entry.tab == "Maintenance"


def test_maint_activity_mirror_ignores_non_strip_worthy_events():
    """progress/queued/cancel_requested/idle stay on the Maintenance tab's
    own progress bar — mirroring every event would turn the strip into a
    firehose."""
    app = _ActivityFakeApp()
    job = _FakeJob("Library Inventory")
    for event in ("queued", "progress", "cancel_requested", "idle"):
        desktop_app.DesktopApp._on_maint_activity_mirror(app, event, job)  # type: ignore[arg-type]
    assert app._activity_model.current() is None


# =============================================================================
# 3c. desktop_app — _activity_render / _activity_on_click / _activity_jump_to_tab
#     called unbound against fake receivers (real logic, fake widgets).
# =============================================================================

class _FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def set(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


class _FakeActivityLabel:
    def __init__(self) -> None:
        self.configured: dict = {}

    def configure(self, **kwargs) -> None:
        self.configured.update(kwargs)


class _RenderFakeApp:
    _activity_render = desktop_app.DesktopApp._activity_render

    def __init__(self, model: activity_strip.ActivityModel) -> None:
        self._activity_model = model
        self._activity_label = _FakeActivityLabel()
        self._activity_var = _FakeStringVar()
        self._activity_full_text = ""


def test_activity_render_shows_the_full_source_message_and_error_color():
    model = activity_strip.ActivityModel()
    model.post("Maintenance", "Reindex FAILED", level="error", tab="Maintenance")
    app = _RenderFakeApp(model)

    desktop_app.DesktopApp._activity_render(app)  # type: ignore[arg-type]

    assert app._activity_var.get() == "Maintenance: Reindex FAILED"
    assert app._activity_full_text == "Maintenance: Reindex FAILED"
    assert app._activity_label.configured["foreground"] == desktop_app._DOT_RED
    assert app._activity_label.configured["cursor"] == "hand2"


def test_activity_render_ellipsizes_a_very_long_combined_message():
    model = activity_strip.ActivityModel()
    model.post("Shows", "x" * 300, level="working", tab="Shows")
    app = _RenderFakeApp(model)

    desktop_app.DesktopApp._activity_render(app)  # type: ignore[arg-type]

    assert app._activity_var.get().endswith("…")
    assert len(app._activity_var.get()) <= desktop_app._ACTIVITY_MAX_CHARS
    # The untruncated text is still available for the tooltip.
    assert app._activity_full_text == f"Shows: {'x' * 300}"


def test_activity_render_clears_and_uses_no_cursor_when_nothing_current():
    app = _RenderFakeApp(activity_strip.ActivityModel())
    desktop_app.DesktopApp._activity_render(app)  # type: ignore[arg-type]
    assert app._activity_var.get() == ""
    assert app._activity_full_text == ""
    assert app._activity_label.configured["cursor"] == ""


def test_activity_render_uses_no_cursor_when_the_entry_has_no_tab():
    model = activity_strip.ActivityModel()
    model.post("Maintenance", "housekeeping done", level="success", tab=None)
    app = _RenderFakeApp(model)
    desktop_app.DesktopApp._activity_render(app)  # type: ignore[arg-type]
    assert app._activity_label.configured["cursor"] == ""


class _FakeNotebook:
    def __init__(self, tabs: list) -> None:
        self._tabs = tabs  # list of (tab_id, text)
        self.selected = None

    def tabs(self):
        return [tab_id for tab_id, _text in self._tabs]

    def tab(self, tab_id, _key):
        for t, text in self._tabs:
            if t == tab_id:
                return text
        return ""

    def select(self, tab_id) -> None:
        self.selected = tab_id


class _ClickFakeApp:
    _activity_on_click = desktop_app.DesktopApp._activity_on_click
    _activity_jump_to_tab = desktop_app.DesktopApp._activity_jump_to_tab
    _activity_render = desktop_app.DesktopApp._activity_render

    def __init__(self, model: activity_strip.ActivityModel, notebook) -> None:
        self._activity_model = model
        self._activity_label = None  # _activity_render must no-op with this
        self._notebook = notebook


def test_activity_click_dismisses_a_sticky_error_and_jumps_to_its_tab():
    model = activity_strip.ActivityModel()
    model.post("Maintenance", "Reindex FAILED", level="error", tab="Maintenance")
    nb = _FakeNotebook([("t1", "Status"), ("t2", "Maintenance")])
    app = _ClickFakeApp(model, nb)

    desktop_app.DesktopApp._activity_on_click(app)  # type: ignore[arg-type]

    assert model.current() is None, "clicking must dismiss the sticky entry"
    assert nb.selected == "t2"


def test_activity_click_with_nothing_current_is_a_no_op():
    app = _ClickFakeApp(activity_strip.ActivityModel(), _FakeNotebook([]))
    desktop_app.DesktopApp._activity_on_click(app)  # type: ignore[arg-type]  # must not raise
    assert app._notebook.selected is None


def test_activity_jump_to_tab_tolerates_a_missing_notebook():
    app = _ClickFakeApp(activity_strip.ActivityModel(), None)
    desktop_app.DesktopApp._activity_jump_to_tab(app, "Shows")  # type: ignore[arg-type]  # must not raise


def test_activity_jump_to_tab_tolerates_an_unknown_tab_name():
    nb = _FakeNotebook([("t1", "Status")])
    app = _ClickFakeApp(activity_strip.ActivityModel(), nb)
    desktop_app.DesktopApp._activity_jump_to_tab(app, "Nonexistent Tab")  # type: ignore[arg-type]
    assert nb.selected is None


def test_activity_open_recent_reads_history_and_offers_tab_jumps_source_check():
    """Building the actual dropdown constructs a real tk.Menu — out of
    reach without a display. The wiring that matters (reads
    ActivityModel.history(), offers a per-entry jump) is pinned here."""
    src = _src(desktop_app.DesktopApp._activity_open_recent)
    assert "self._activity_model.history()" in src
    assert "self._activity_jump_to_tab(t)" in src


# =============================================================================
# 3d. desktop_app — "Apply Selected" busy-guard coverage (item 4): every
#     bare-thread branch disables the button, every completion restores it.
# =============================================================================

def test_apply_maint_selection_busy_guards_every_dispatch_branch():
    src = _src(desktop_app.DesktopApp._apply_maint_selection)
    # sanitize, movie_migration, clean_junk, custom_rename all start their
    # own worker thread directly in this method; find_duplicates and
    # missing_episodes delegate to _confirm_and_delete_duplicates /
    # _grab_missing_selected (checked separately below).
    assert src.count("begin_busy(self._maint_apply_btn") == 4
    assert "self._maint_apply_restore()" in src  # the clean_junk done() closure


def test_confirm_and_delete_duplicates_busy_guards_its_thread():
    src = _src(desktop_app.DesktopApp._confirm_and_delete_duplicates)
    assert "begin_busy(self._maint_apply_btn" in src


def test_grab_missing_selected_busy_guards_its_thread():
    src = _src(desktop_app.DesktopApp._grab_missing_selected)
    assert "begin_busy(self._maint_apply_btn" in src
    assert "self._maint_apply_restore()" in src  # its own done() closure


@pytest.mark.parametrize("method_name", [
    "_handle_apply_sanitize_result",
    "_handle_movie_migration_result",
    "_handle_duplicate_delete_result",
])
def test_apply_completion_handlers_restore_the_busy_button(method_name):
    src = _src(getattr(desktop_app.DesktopApp, method_name))
    assert "self._maint_apply_restore()" in src
    # restore() must run before anything else in the handler could bail out
    # early on a branch — pinned as "near the top" by checking it appears
    # before the method's own primary status-line update.
    assert src.index("self._maint_apply_restore()") < src.index("self._maint_status_var.set(")


def test_maint_apply_restore_defaults_to_a_safe_no_op():
    src = _src(desktop_app.DesktopApp.__init__)
    assert "self._maint_apply_restore: Callable[[], None] = lambda: None" in src


def test_maint_apply_failed_helper_restores_sets_status_and_posts_error():
    """The shared failure path itself: restore, status line, sticky error
    entry — exercised directly against a minimal fake self (no need to
    drive a whole worker thread just to prove this one helper's body)."""
    src = _src(desktop_app.DesktopApp._maint_apply_failed)
    assert "self._maint_apply_restore()" in src
    assert 'self.post_activity("Maintenance"' in src
    assert 'level="error"' in src
    assert "self._post_to_ui(" in src


# ---------------------------------------------------------------------------
# Verification item 3 — exception-safety on the six Apply-Selected worker
# threads. Before this fix, none of the six had a try/except around their
# body at all (sanitize/movie_migration/clean_junk/custom_rename/find_
# duplicates-delete) or only had a PER-ITEM try that couldn't catch a
# blowup in the surrounding setup code (missing_episodes) — an escaped
# exception left the button permanently disabled (_maint_apply_restore
# never ran) and the failure was invisible outside the log.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method_name, worker_count", [
    ("_apply_maint_selection", 4),   # sanitize, movie_migration, clean_junk, custom_rename
    ("_confirm_and_delete_duplicates", 1),
    ("_grab_missing_selected", 1),
])
def test_every_apply_selected_worker_calls_the_shared_failure_helper(
        method_name, worker_count):
    src = _src(getattr(desktop_app.DesktopApp, method_name))
    assert src.count("except Exception as exc:") == worker_count
    assert src.count("self._maint_apply_failed(") == worker_count


class _SyncThread:
    """threading.Thread stand-in that runs its target synchronously on
    .start() — same technique tests/test_task_c_fixsprint.py uses for
    pull_watchlist — so a worker thread's failure path can be observed
    without a real background thread or an event loop to marshal onto."""
    def __init__(self, target: Callable[[], None] | None = None,
                name: str | None = None, daemon: bool | None = None,
                **_kwargs: object) -> None:
        self._target = target

    def start(self) -> None:
        if self._target is not None:
            self._target()


class _ApplyFailureFakeApp:
    """Drives the REAL, unbound _confirm_and_delete_duplicates (and the
    REAL _maint_apply_failed it falls through to) against a minimal fake
    self — same technique test_maint_wiring.py's _FakeMaintApp uses for
    _apply_maint_selection. Deliberately reuses the production
    _maint_apply_failed rather than faking it too, so this test proves the
    real integration: restore() really gets called from inside the real
    failure helper, not just that the fake recorded a call the test itself
    wired up."""
    _confirm_and_delete_duplicates = desktop_app.DesktopApp._confirm_and_delete_duplicates
    _maint_apply_failed = desktop_app.DesktopApp._maint_apply_failed
    # _fmt_bytes is a @staticmethod on DesktopApp -- re-wrapping in
    # staticmethod() here too is required, not decorative: a plain function
    # object assigned as a class attribute becomes a bound method through
    # the normal descriptor protocol, which would silently smuggle a `self`
    # argument into a function that only takes one (n: int).
    _fmt_bytes = staticmethod(desktop_app.DesktopApp._fmt_bytes)

    def __init__(self) -> None:
        self._maint_results: list = []
        self._maint_status_var = _FakeStringVar()
        self._maint_apply_btn = None
        self._maint_apply_restore = lambda: None
        self.posted: list = []

    def _ask_yes_no(self, *_a, **_k) -> bool:
        return True

    def _post_to_ui(self, fn, *, delay_ms=0):
        fn()
        return True

    def post_activity(self, source, message, level="working", tab=None) -> None:
        self.posted.append((source, message, level, tab))


def test_apply_selected_worker_restores_the_busy_button_when_its_helper_raises(
        monkeypatch):
    """Verification item 3, driven end to end: delete_files_with_cleanup
    blows up inside _confirm_and_delete_duplicates' worker thread (one of
    the six Apply Selected workers) — restore() must still run, the status
    line must still update, and the failure must still reach the strip as
    a sticky error. threading.Thread is replaced with a synchronous
    stand-in so the worker body — including its except branch — runs
    inline instead of on a background thread this test would have to wait
    on."""
    restore_calls: list[int] = []

    def fake_begin_busy(_buttons, working_text=None):
        return lambda: restore_calls.append(1)

    def boom(_paths):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(desktop_app, "begin_busy", fake_begin_busy)
    monkeypatch.setattr(desktop_app, "delete_files_with_cleanup", boom)
    monkeypatch.setattr(desktop_app.threading, "Thread", _SyncThread)

    app = _ApplyFailureFakeApp()
    desktop_app.DesktopApp._confirm_and_delete_duplicates(
        app, ["/lib/a.mkv"])  # type: ignore[arg-type]

    assert restore_calls == [1], (
        "an escaped exception must never leave Apply Selected permanently "
        "disabled")
    assert app.posted, "the failure must also reach the activity strip"
    source, message, level, tab = app.posted[-1]
    assert level == "error"
    assert "disk on fire" in message
    assert "disk on fire" in app._maint_status_var.get()


# =============================================================================
# 3e. shows_tab.ShowsTab — toolbar-label move + strip posting + busy buttons.
# =============================================================================

def test_shows_tab_status_label_is_no_longer_toolbar_packed():
    src = _src(shows_tab.ShowsTab.__init__)
    assert "textvariable=self._status_var" not in src, (
        "the old toolbar-packed status label must be gone (Task L item 3)")
    assert "textvariable=self._status_display_var" in src
    assert 'status_row.grid(row=1, column=0, sticky="ew"' in src
    assert "add_tooltip(status_label, self._status_var.get)" in src


def test_shows_tab_body_canvas_shifted_down_for_the_new_status_row():
    src = _src(shows_tab.ShowsTab.__init__)
    assert 'body_canvas.grid(row=2, column=0, sticky="nsew")' in src
    assert 'body_scroll.grid(row=2, column=1, sticky="ns")' in src


def test_shows_tab_captures_toolbar_buttons_for_the_busy_guard():
    src = _src(shows_tab.ShowsTab.__init__)
    assert 'self._scan_btn = self._toolbar_buttons["Scan Folders"]' in src
    assert 'self._sync_btn = self._toolbar_buttons["Sync Episodes"]' in src
    assert 'self._grab_btn = self._toolbar_buttons["⬇ Grab Missing Now"]' in src


def test_shows_tab_run_guarded_posts_to_the_strip_and_supports_a_busy_button():
    src = _src(shows_tab.ShowsTab._run_guarded)
    assert 'self.app.post_activity("Shows"' in src
    assert "begin_busy(button" in src


def test_shows_tab_finish_operation_restores_the_button_and_posts_the_result():
    src = _src(shows_tab.ShowsTab._finish_operation)
    assert "restore()" in src
    assert 'self.app.post_activity("Shows"' in src


@pytest.mark.parametrize("method_name, button_attr", [
    ("scan_folders", "self._scan_btn"),
    ("sync_all", "self._sync_btn"),
    ("grab_missing_now", "self._grab_btn"),
])
def test_shows_tab_toolbar_actions_pass_their_own_button(method_name, button_attr):
    src = _src(getattr(shows_tab.ShowsTab, method_name))
    assert f"button={button_attr}" in src


# =============================================================================
# 3f. watchlist_tab.WatchlistTab — same three checks.
# =============================================================================

def test_watchlist_tab_status_label_is_no_longer_toolbar_packed():
    src = _src(watchlist_tab.WatchlistTab.__init__)
    assert "textvariable=self._status_var" not in src, (
        "the old toolbar-packed status label must be gone (Task L item 3)")
    assert "textvariable=self._status_display_var" in src
    assert 'status_row.grid(row=1, column=0, sticky="ew"' in src
    assert "add_tooltip(status_label, self._status_var.get)" in src


def test_watchlist_tab_panes_shifted_down_for_the_new_status_row():
    src = _src(watchlist_tab.WatchlistTab.__init__)
    assert 'panes.grid(row=2, column=0, sticky="nsew")' in src


def test_watchlist_tab_captures_pull_and_recs_buttons_for_the_busy_guard():
    init_src = _src(watchlist_tab.WatchlistTab.__init__)
    assert 'self._pull_btn = self._toolbar_buttons["Pull Watchlist"]' in init_src
    assert "self._recs_btn = get_btn" in init_src


def test_watchlist_tab_pull_watchlist_posts_to_strip_and_uses_busy_guard():
    src = _src(watchlist_tab.WatchlistTab.pull_watchlist)
    assert 'self.app.post_activity("Watchlist"' in src
    assert "begin_busy(self._pull_btn" in src
    assert "restore()" in src


def test_watchlist_tab_get_recs_posts_to_strip_and_uses_busy_guard():
    src = _src(watchlist_tab.WatchlistTab.get_recs)
    assert 'self.app.post_activity("Watchlist"' in src
    assert "begin_busy(self._recs_btn" in src
    assert "restore()" in src


# =============================================================================
# 3g. grab_queue_tab.GrabQueueTab — verification item 4: this Requests
# subtab had the identical toolbar-packed-right status label and none of
# its three async actions (_grab_now, _create_placement, _grab_missing)
# had a busy guard or posted to the strip. Same three checks as shows_tab
# and watchlist_tab, adapted for the one wrinkle this tab has: there is no
# per-action toolbar button (grab/place/grab-missing all come off a
# right-click menu that's gone before its command runs), so Refresh — the
# only persistent button here — is what begin_busy disables instead.
# =============================================================================

def test_grab_queue_tab_status_label_is_no_longer_toolbar_packed():
    src = _src(grab_queue_tab.GrabQueueTab.__init__)
    assert "textvariable=self._status_var" not in src, (
        "the old toolbar-packed status label must be gone (Task L item 3)")
    assert "textvariable=self._status_display_var" in src
    assert 'status_row.grid(row=1, column=0, sticky="ew"' in src
    assert "add_tooltip(status_label, self._status_var.get)" in src


def test_grab_queue_tab_panes_shifted_down_for_the_new_status_row():
    src = _src(grab_queue_tab.GrabQueueTab.__init__)
    assert 'panes.grid(row=2, column=0, sticky="nsew")' in src


def test_grab_queue_tab_captures_refresh_button_for_the_busy_guard():
    src = _src(grab_queue_tab.GrabQueueTab.__init__)
    assert "self._refresh_btn = refresh_btn" in src


@pytest.mark.parametrize("method_name", [
    "_grab_now",
    "_create_placement",
    "_grab_missing",
])
def test_grab_queue_tab_named_actions_use_the_busy_guard_and_post_to_strip(
        method_name):
    src = _src(getattr(grab_queue_tab.GrabQueueTab, method_name))
    assert "begin_busy(self._refresh_btn" in src
    assert 'self.app.post_activity("Grab queue"' in src
    assert "restore()" in src


def test_grab_queue_tab_refresh_failure_reaches_the_strip():
    src = _src(grab_queue_tab.GrabQueueTab.refresh)
    assert 'self.app.post_activity("Grab queue"' in src
    assert 'level="error"' in src


def test_grab_queue_tab_working_messages_are_plain_prose_no_em_dash():
    """Verification item 5's spirit extended to the copy this rework itself
    introduces here — plain prose, no em dash, matching the sprint's own
    wording rule. Checked directly against the literal working-state
    strings this rework added (code comments elsewhere in the same
    methods legitimately use em dashes and aren't what's being asked for
    here) — asserting the exact plain-copy literal is present both proves
    the wording and rules out an em-dash variant slipping back in."""
    combined = "".join(_src(getattr(grab_queue_tab.GrabQueueTab, name)) for name in
                       ("_grab_now", "_create_placement", "_grab_missing"))
    for literal in ('"Request #{row.request_id} reopened, grabbing…"',
                    '"Creating folder and placing…"',
                    '"Searching missing episodes…"'):
        assert literal in combined, f"missing or reworded: {literal}"
