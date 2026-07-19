# =============================================================================
# tests/test_maint_wiring.py
# =============================================================================
# Task B — headless wiring regression tests for the Maintenance tab.
#
# DesktopApp is a Tk GUI class; the sprint rules forbid running the app GUI
# and this repo has no Tk-faking harness (test_maint_jobs.py tests the
# UI-free job registry directly; test_maintenance_dupes.py tests the pure
# maintenance.py functions — neither instantiates DesktopApp). Importing
# desktop_app and inspecting its CLASS does not touch Tk at all: no tk.Tk()
# runs until DesktopApp() is constructed. That gives us two honest checks
# without a display:
#   1. the callback exists and is callable (an AttributeError here means a
#      button's `command=` would crash the instant it's clicked);
#   2. source-level assertions on the method bodies (same technique as
#      tests/test_task_s.py's pickle/shell-usage guards) proving the ORDER
#      of operations — e.g. that the cache is popped before the re-run call,
#      not after — and that specific tool keys are wired in, without needing
#      to fake a Tk widget tree to observe it.
# Anything that needs an actual rendered widget (checkbox state, tree
# selection, a popup appearing) is out of reach here and is called out as
# unverified in the sprint report instead of faked.
# =============================================================================
import inspect
import re

import desktop_app


def _src(name: str) -> str:
    return inspect.getsource(getattr(desktop_app.DesktopApp, name))


# ---------------------------------------------------------------------------
# B item 1 — Reindex is a real registry job that reports into the
# maintenance tab, not a bare thread that only touches Library-tab widgets.
# ---------------------------------------------------------------------------

def test_reindex_callback_exists_and_is_callable():
    assert callable(desktop_app.DesktopApp.rebuild_library_index_from_ui)
    assert callable(desktop_app.DesktopApp._handle_library_reindex_result)


def test_reindex_submits_a_registry_job_not_a_bare_thread():
    src = _src("rebuild_library_index_from_ui")
    assert "self._maint_submit(" in src, (
        "Reindex must run through the job registry so the maintenance "
        "progress strip/status line actually moves — B item 1")
    assert "threading.Thread" not in src, (
        "Reindex reverted to a bare background thread invisible to the "
        "maintenance tab's progress strip")


def test_reindex_result_handler_touches_the_maintenance_status_line():
    src = _src("_handle_library_reindex_result")
    assert "_maint_status_var" in src
    assert "_populate_maint_tree" in src, (
        "Reindex must visibly render into the maintenance grid, per B item 1")


def test_reindex_is_registered_in_the_job_dispatch_table():
    src = _src("_maint_dispatch_result")
    assert '"reindex": self._handle_library_reindex_result' in src


# ---------------------------------------------------------------------------
# B item 2 — Re-run covers every tool that renders into the maintenance
# grid, not just the original 6-key allowlist.
# ---------------------------------------------------------------------------

def test_rerun_callback_exists_and_is_callable():
    assert callable(desktop_app.DesktopApp._maint_rerun_current)
    assert callable(desktop_app.DesktopApp._maint_rerun_custom_rename)


def test_rerun_covers_every_grid_tool():
    src = _src("_maint_rerun_current")
    for key in ("library_inventory", "find_duplicates", "sanitize",
               "clean_junk", "missing_episodes", "unindexed",
               "daily_check", "movie_migration", "reindex"):
        assert f'"{key}"' in src, f"_maint_rerun_current dropped {key!r}"
    # custom_rename (which covers both the dialog and Combo Clean, since
    # both render into the same shared grid) is special-cased rather than
    # living in the plain zero-arg runners dict.
    assert '"custom_rename"' in src
    assert "_maint_rerun_custom_rename" in src


def test_custom_rename_replay_covers_both_producers():
    """The 'custom_rename' grid is fed by two different flows (the Custom
    Rename dialog and Combo Clean); the replay dict must be able to name
    either one so Re-run can reconstruct the right job."""
    rerun_src = _src("_maint_rerun_custom_rename")
    assert '"combo_clean"' in rerun_src
    # Falls through to the dialog-replay branch for anything else, and that
    # branch calls the exact worker the dialog itself calls.
    assert "_build_custom_rename_pairs(rule, pattern, allowed_tags)" in rerun_src

    combo_src = _src("_handle_combo_clean_result")
    assert "_maint_custom_rename_replay" in combo_src
    assert '"kind": "combo_clean"' in combo_src

    dialog_src = _src("_open_custom_rename")
    assert "_maint_custom_rename_replay" in dialog_src
    assert '"kind": "custom_dialog"' in dialog_src


def test_rerun_with_no_stored_replay_prompts_instead_of_crashing():
    """Re-run on a custom_rename grid nobody has built yet (fresh app,
    nothing in _maint_custom_rename_replay) must degrade to an info popup,
    never an AttributeError/KeyError."""
    src = _src("_maint_rerun_custom_rename")
    assert "replay is None" in src
    assert "_show_info(" in src


# ---------------------------------------------------------------------------
# B item 3 — status line always stamps a rescan marker after a completed
# grid-rendering run.
# ---------------------------------------------------------------------------

def test_rescan_stamp_callback_exists_and_is_callable():
    assert callable(desktop_app.DesktopApp._maint_stamp_rescanned)
    assert callable(desktop_app.DesktopApp._maint_append_rescan_stamp)


def test_rescan_stamp_wording_matches_existing_style():
    src = _src("_maint_append_rescan_stamp")
    assert "rescanned" in src and "%H:%M" in src


def test_rescan_stamp_is_invoked_after_every_completed_job():
    src = _src("_maint_job_finished_ui")
    assert "_maint_stamp_rescanned(job)" in src
    # Must run AFTER dispatch renders the grid, not before (the row count
    # the stamp reports comes from what dispatch just populated).
    assert src.index("_maint_dispatch_result(job)") < src.index(
        "_maint_stamp_rescanned(job)")


def test_grid_noun_map_covers_every_re_runnable_tool():
    nouns = desktop_app.DesktopApp._MAINT_GRID_NOUN
    for key in ("daily_check", "library_inventory", "find_duplicates",
               "sanitize", "clean_junk", "missing_episodes", "unindexed",
               "movie_migration", "combo_clean", "reindex"):
        assert key in nouns
    assert nouns["find_duplicates"] == "groups"


# ---------------------------------------------------------------------------
# B item 4 — cache popped + tool name cleared BEFORE the post-apply re-run
# in the duplicate-delete and sanitize-apply paths (the Clean Junk pattern).
# ---------------------------------------------------------------------------

def _assert_pop_precedes_rerun(method_name: str, cache_key: str,
                               rerun_call: str) -> None:
    src = _src(method_name)
    pop_pat = re.compile(
        r'_maint_cache\.pop\(\s*["\']' + re.escape(cache_key) + r'["\']')
    pop_matches = list(pop_pat.finditer(src))
    assert pop_matches, (
        f"{method_name} never pops the '{cache_key}' cache entry")
    rerun_idx = src.index(rerun_call)
    assert pop_matches[0].start() < rerun_idx, (
        f"{method_name} calls {rerun_call} before popping the "
        f"'{cache_key}' cache -- stale results would render (B item 4)")
    # The Clean Junk pattern this copies also clears the tool name so the
    # subsequent _run_* call can't short-circuit via _maint_render_cached.
    clear_pat = re.compile(r'_maint_tool_name\s*=\s*["\']["\']')
    clear_matches = list(clear_pat.finditer(src))
    assert clear_matches and clear_matches[0].start() < rerun_idx


def test_clean_junk_reference_pattern_pops_cache_before_rerun():
    """The pattern everything else is copying from (B item 4 cites this
    exact site) — pinned here so a future refactor can't quietly break the
    thing the other two are modeled on."""
    _assert_pop_precedes_rerun(
        "_apply_maint_selection", "clean_junk", "self._run_clean_junk()")


def test_duplicate_delete_pops_cache_before_rerunning_find_duplicates():
    _assert_pop_precedes_rerun(
        "_handle_duplicate_delete_result", "find_duplicates",
        "self._run_find_duplicates()")


def test_sanitize_apply_pops_cache_before_rerunning():
    """Covers both branches _handle_apply_sanitize_result can take: plain
    Sanitize Names re-runs directly; the custom_rename branch delegates to
    _maint_rerun_custom_rename(), which itself pops its cache first (checked
    separately by test_rerun_covers_every_grid_tool's sibling below)."""
    src = _src("_handle_apply_sanitize_result")
    pop_idx = src.index('_maint_cache.pop("sanitize"')
    rerun_idx = src.index("self._run_sanitize()")
    assert pop_idx < rerun_idx
    assert "_maint_rerun_custom_rename()" in src


def test_ignored_pairs_unignore_invalidates_the_dupes_cache():
    src = _src("_maint_open_ignored_pairs")
    assert '_maint_cache.pop("find_duplicates"' in src


# ---------------------------------------------------------------------------
# G item 2 — "Not a duplicate" wiring.
# ---------------------------------------------------------------------------

def test_not_duplicate_callbacks_exist_and_are_callable():
    assert callable(desktop_app.DesktopApp._maint_mark_not_duplicate)
    assert callable(desktop_app.DesktopApp._maint_open_ignored_pairs)


def test_not_duplicate_pops_cache_before_rescanning():
    _assert_pop_precedes_rerun(
        "_maint_mark_not_duplicate", "find_duplicates",
        "self._run_find_duplicates()")


def test_not_duplicate_uses_the_dedicated_ignore_store_not_maint_cache():
    src = _src("_maint_mark_not_duplicate")
    assert "import dupe_ignore" in src
    assert "dupe_ignore.ignore_folder_pair" in src
    assert "dupe_ignore.ignore_file_pair" in src
    # It must NOT write verdicts into maintenance_cache.json's structures --
    # that file is wiped wholesale by Re-run (G item 2's explicit warning).
    assert "self._maint_cache[" not in src


def test_find_duplicates_result_rows_carry_ignorable_payloads():
    """Header rows used to carry payload=None (never actionable, and
    filtered out of selected_payloads entirely). They now carry a
    distinguishable ('folder_pair', a, b) / ('group', paths) tuple so 'Not a
    duplicate' can resolve a selected header back to what to ignore --
    Apply Selected (which only understands str / ('paths', ...) payloads
    for deletion) still contributes nothing to paths_to_delete for them, but
    -- unlike plain None -- these payloads now SURVIVE the `payload is not
    None` filter, so Apply Selected must explicitly re-warn when that
    leaves nothing deletable (verification finding 3) rather than silently
    returning. That warning path is exercised directly below, not just
    grepped for."""
    src = _src("_handle_duplicates_result")
    assert '("folder_pair", da, db)' in src
    assert '("group", list(group.candidates))' in src

    apply_src = _src("_apply_maint_selection")
    assert 'p[0] == "paths"' in apply_src
    # Apply Selected's find_duplicates branch may mention "folder_pair" in an
    # explanatory comment, but it must never BRANCH on it as an action --
    # the only tuple shape it acts on for deletion is ("paths", [...]).
    assert 'p[0] == "folder_pair"' not in apply_src
    assert 'p[0] == "group"' not in apply_src


class _FakeCheckVar:
    """Stand-in for tk.BooleanVar's .get() -- _apply_maint_selection never
    touches anything else on the row-state var, so this is a faithful
    substitute without needing a live Tk root."""

    def __init__(self, value: bool) -> None:
        self._value = value

    def get(self) -> bool:
        return self._value


class _FakeMaintApp:
    """Minimal stand-in for DesktopApp exercising the UNBOUND
    _apply_maint_selection method. The method only ever reads self.* state
    and calls self._show_info / self._show_warning / self._confirm_and_delete_
    duplicates -- no real Tk widget is touched, so a plain object with those
    attributes stubbed drives the exact same code path a real button click
    would, without launching the GUI."""

    def __init__(self, row_state: dict) -> None:
        self._maint_tool_name = "find_duplicates"
        self._maint_row_state = row_state
        self.warnings: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []
        self.confirmed_deletes: list[str] | None = None

    def _show_info(self, title, msg):
        self.infos.append((title, msg))

    def _show_warning(self, title, msg):
        self.warnings.append((title, msg))

    def _confirm_and_delete_duplicates(self, paths):
        self.confirmed_deletes = paths


def test_apply_selected_warns_when_only_header_rows_are_ticked():
    """Verification finding 3, reproduced and fixed: ticking ONLY a
    group/folder-pair header checkbox and clicking Apply Selected must warn
    'Nothing selected', not silently do nothing (it used to warn before
    Task G item 2 gave headers a non-None payload)."""
    row_state = {
        "hdr_group": (_FakeCheckVar(True), ("group", ["/lib/A/1.mkv", "/lib/A/2.mkv"])),
        "hdr_folder_pair": (_FakeCheckVar(True), ("folder_pair", "/lib/A", "/lib/B")),
    }
    app = _FakeMaintApp(row_state)

    desktop_app.DesktopApp._apply_maint_selection(app)  # type: ignore[arg-type]

    assert app.confirmed_deletes is None, (
        "header-only selection must never reach the delete confirmation")
    assert app.warnings, "Apply Selected silently no-op'd -- finding 3 regression"
    assert app.warnings[-1][0] == "Nothing selected"


def test_apply_selected_still_deletes_when_a_file_row_is_ticked():
    """Sanity check the finding-3 fix doesn't over-correct: a real file-row
    selection (alongside an untouched header) still reaches the normal
    delete-confirmation path with exactly the ticked file."""
    row_state = {
        "hdr_group": (_FakeCheckVar(False), ("group", ["/lib/A/1.mkv", "/lib/A/2.mkv"])),
        "file_row": (_FakeCheckVar(True), "/lib/A/1.mkv"),
    }
    app = _FakeMaintApp(row_state)

    desktop_app.DesktopApp._apply_maint_selection(app)  # type: ignore[arg-type]

    assert app.confirmed_deletes == ["/lib/A/1.mkv"]
    assert not app.warnings
