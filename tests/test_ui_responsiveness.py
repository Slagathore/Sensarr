"""A busy database must not prevent Tk from servicing input and timers."""

import concurrent.futures
import contextlib
import gc
import queue
import threading
import time
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk

import desktop_app
import auth_store
import downloads_store
import grab_queue
import queue_store
from grab_queue_tab import GrabQueueTab
from media_lookup import MediaResult


@pytest.fixture(scope="module", autouse=True)
def collect_tk_objects_on_main_thread():
    # These tests create and destroy multiple Tcl interpreters. A worker's
    # allocation must not collect a previous test's cyclic Tk objects; their
    # destructors, like all other Tk calls, belong on the main thread.
    enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    yield
    gc.collect()
    if enabled:
        gc.enable()


def pump(root, until, seconds=2):
    deadline = time.monotonic() + seconds
    while not until() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.003)
    assert until(), "Tk did not deliver the expected callback"


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root):
    root = tk_root
    instance = desktop_app.DesktopApp.__new__(desktop_app.DesktopApp)
    instance.root = root
    instance._quitting = False
    instance._ui_thread = threading.current_thread()
    instance._ui_queue = queue.Queue()
    instance._refresh_lock = threading.Lock()
    instance._refresh_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    instance._refresh_futures = {}
    instance._snapshot_running = set()
    instance._snapshot_pending = {}
    instance._download_refresh_queued = False
    instance._download_rows_by_id = {}
    instance._request_rows_by_id = {}
    instance._request_add_pending = False
    instance.requester_var = tk.StringVar(root, value="Example user")
    instance.request_content_var = tk.StringVar(root)
    instance.status_var = tk.StringVar(root)
    instance.last_action_var = tk.StringVar(root)
    instance.status_text = tk.Text(root)
    instance._request_add_btn = ttk.Button(root, text="Add")
    instance._apply_dark_widget_styles = lambda _window: None
    instance._activity_tree = ttk.Treeview(root, columns=("at", "state", "title"))
    instance._activity_summary_var = tk.StringVar(root)
    instance._needs_you_var = tk.StringVar(root)
    instance._needs_you_label = ttk.Label(root)
    instance._dl_downloads_tree = ttk.Treeview(root, columns=("id", "title", "status", "progress", "route"))
    instance._dl_history_tree = ttk.Treeview(root, columns=("at", "action", "before", "after"))
    instance._dl_status_var = tk.StringVar(root)
    instance.requests_tree = ttk.Treeview(root, columns=tuple(str(n) for n in range(7)))
    instance.request_detail_text = tk.Text(root)
    instance.queue_var = tk.StringVar(root)
    instance._users_pending_tree = ttk.Treeview(root, columns=tuple(str(n) for n in range(5)))
    instance._users_allowed_tree = ttk.Treeview(root, columns=tuple(str(n) for n in range(7)))
    instance._notebook = None
    instance._users_tab = None
    instance.download_manager = SimpleNamespace(phase_for=lambda _id: None)
    instance._last_idle_cache_date = ""
    instance._run_idle_cache_pass = lambda: None
    instance.post_activity = lambda *args, **kwargs: None
    instance.bot_status_var = tk.StringVar(root)
    instance._bot_dot = ttk.Label(root)
    downloads_store.initialize_downloads_db()
    queue_store.initialize_queue_db()
    auth_store.initialize_auth_db()
    root.after(0, instance._drain_ui_queue)
    yield instance
    instance._quitting = True
    instance._refresh_pool.shutdown(wait=True, cancel_futures=True)
    for timer in root.tk.call("after", "info"):
        root.after_cancel(timer)
    for widget in root.winfo_children():
        widget.destroy()
    root.update_idletasks()


@contextlib.contextmanager
def held_store_lock(lock=downloads_store._DL_LOCK):
    """Use the exact process lock background writes and the UI contend on."""
    acquired = threading.Event()
    release = threading.Event()
    unlocked = threading.Event()

    def holder():
        with lock:
            acquired.set()
            # A regression must fail quickly instead of hanging the suite.
            release.wait(2)
        unlocked.set()

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(1)
    try:
        yield unlocked
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize("method", [
    "refresh_activity_feed", "refresh_downloads", "refresh_download_history",
    "_refresh_needs_you", "_idle_cache_tick",
])
def test_database_lock_does_not_block_tk_heartbeat(app, monkeypatch, method):
    # The data read remains real; only unrelated count queries are replaced.
    monkeypatch.setattr(grab_queue, "status_counts", lambda: (
        downloads_store.list_downloads(limit=1), grab_queue.StatusCounts())[1])
    app._activity_tree.insert("", "end", iid="existing", values=("", "", "Previous data"))
    heartbeat = []
    render_threads = []
    original = app._render_activity_feed

    def render(snapshot):
        render_threads.append(threading.current_thread())
        original(snapshot)

    app._render_activity_feed = render
    with held_store_lock() as unlocked:
        app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
        getattr(app, method)()
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert heartbeat == [True], "database contention blocked the Tk event loop"
        assert app._activity_tree.exists("existing"), "pending read cleared visible data"
    pump(app.root, lambda: not app._snapshot_running)
    assert all(thread is app._ui_thread for thread in render_threads)


def test_progress_burst_posts_one_refresh_signal(app):
    calls = []
    app.refresh_downloads = lambda: calls.append("downloads")
    app.refresh_activity_feed = lambda: calls.append("activity")
    thread = threading.Thread(target=lambda: [app._on_download_update(n) for n in range(1000)])
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert app._ui_queue.qsize() == 1
    pump(app.root, lambda: len(calls) == 2)
    assert calls == ["downloads", "activity"]
    assert not app._download_refresh_queued


@pytest.mark.parametrize("method,lock,tree_name", [
    ("refresh_requests", queue_store._DB_LOCK, "requests_tree"),
    ("refresh_users_tab", auth_store._AUTH_LOCK, "_users_pending_tree"),
])
def test_other_periodic_store_reads_do_not_block_tk(app, method, lock, tree_name):
    tree = getattr(app, tree_name)
    tree.insert("", "end", iid="existing", values=("Previous data",))
    heartbeat = []
    with held_store_lock(lock) as unlocked:
        app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
        getattr(app, method)()
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert heartbeat == [True]
        assert tree.exists("existing")
    pump(app.root, lambda: not app._snapshot_running)
    assert not tree.exists("existing")


def test_request_selection_uses_rendered_snapshot_while_store_is_busy(app):
    row = queue_store.QueueRequest(
        1, "Example user", "Example title", "open", "2026-11-07", None,
        "movie", "Example title", None, None, False, None)
    app._render_requests(([row], 1))
    app.requests_tree.selection_set("1")
    heartbeat = []
    with held_store_lock(queue_store._DB_LOCK) as unlocked:
        app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
        app._on_request_selected(None)
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert heartbeat == [True]
        assert "Example title" in app.request_detail_text.get("1.0", "end")


@pytest.mark.parametrize("action", ["cancel", "keep_details"])
def test_download_store_actions_do_not_block_tk(app, action):
    finished = []
    if action == "cancel":
        app._dl_downloads_tree.insert("", "end", iid="9999", values=(9999,))
        app._dl_downloads_tree.selection_set("9999")
        app.download_manager.cancel = lambda download_id: bool(
            downloads_store.get_download(download_id))
        app.refresh_downloads = lambda: finished.append(True)
        invoke = app.cancel_selected_download
    else:
        tab = GrabQueueTab(ttk.Frame(app.root), app)
        tab.refresh = lambda: finished.append(True)
        row = grab_queue.GrabQueueRow(
            "request", "tmdb:1", "Example", "open", selection_run_id=9999)
        invoke = lambda: tab._keep_details(row)
    heartbeat = []
    with held_store_lock() as unlocked:
        app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
        invoke()
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert heartbeat == [True]
        assert not finished
    pump(app.root, lambda: bool(finished))


def test_snapshot_burst_retains_only_latest_pending_read(app):
    started = threading.Event()
    release = threading.Event()
    loaded = []
    rendered = []

    def first():
        started.set()
        assert release.wait(2)
        loaded.append(0)
        return 0

    app._load_ui_snapshot("burst", first, rendered.append)
    assert started.wait(1)
    try:
        for n in range(1, 1001):
            app._load_ui_snapshot("burst", lambda n=n: (loaded.append(n), n)[1], rendered.append)
        assert len(app._snapshot_running) == 1
        assert len(app._snapshot_pending) == 1
    finally:
        release.set()
    pump(app.root, lambda: not app._snapshot_running)
    assert loaded == [0, 1000]
    assert rendered == [0, 1000]


def test_renderer_failure_does_not_leave_snapshot_stuck(app):
    def fail(_value):
        raise RuntimeError("widget render failed")

    app._load_ui_snapshot("render", lambda: 1, fail)
    rendered = []
    app._load_ui_snapshot("render", lambda: 2, rendered.append)
    pump(app.root, lambda: not app._snapshot_running)
    assert rendered == [2]


def test_failed_activity_load_preserves_rows_and_can_retry(app, monkeypatch):
    app._activity_tree.insert("", "end", iid="existing", values=("", "", "Previous data"))

    def fail(**kwargs):
        raise RuntimeError("database temporarily busy")

    monkeypatch.setattr(downloads_store, "list_downloads", fail)
    app.refresh_activity_feed()
    pump(app.root, lambda: not app._snapshot_running)
    assert app._activity_tree.exists("existing")
    monkeypatch.setattr(downloads_store, "list_downloads", lambda **kwargs: [])
    monkeypatch.setattr(downloads_store, "list_history", lambda **kwargs: [])
    monkeypatch.setattr(grab_queue, "status_counts", lambda: grab_queue.StatusCounts())
    app.refresh_activity_feed()
    pump(app.root, lambda: not app._snapshot_running)
    assert not app._activity_tree.get_children()
    assert "no active downloads" in app._activity_summary_var.get()


def test_queue_drain_yields_when_callbacks_use_up_time_budget(app):
    callbacks = []
    for number in range(10):
        app._ui_queue.put((0, lambda n=number: (time.sleep(0.006), callbacks.append(n))))
    app._drain_ui_queue()
    assert 0 < len(callbacks) < 10
    assert app._ui_queue.qsize() > 0
    pump(app.root, lambda: len(callbacks) == 10)


def test_grab_queue_refresh_remains_responsive_during_database_lock(app, monkeypatch):
    tab = GrabQueueTab(ttk.Frame(app.root), app)
    row = grab_queue.GrabQueueRow("request", "tmdb:1", "Example", "open", request_id=1)
    tab._apply_rows([row])

    def load():
        downloads_store.list_downloads(limit=1)
        return [row]

    monkeypatch.setattr(grab_queue, "list_grab_queue_rows", load)
    heartbeat = []
    with held_store_lock() as unlocked:
        app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
        tab.refresh()
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert heartbeat == [True]
        assert tab._tree.get_children() == ("0",)
    pump(app.root, lambda: not app._snapshot_running)
    assert tab._rows == [row]


def test_slow_decision_does_not_overwrite_new_selection(app, monkeypatch):
    tab = GrabQueueTab(ttk.Frame(app.root), app)
    rows = [grab_queue.GrabQueueRow("request", f"tmdb:{n}", f"Example {n}", "open",
                                  request_id=n, selection_run_id=n) for n in (1, 2)]
    tab._apply_rows(rows)

    def decision(run):
        downloads_store.list_downloads(limit=1)
        return f"Decision {run}"

    monkeypatch.setattr(GrabQueueTab, "_decision_text", staticmethod(decision))
    heartbeat = []
    with held_store_lock() as unlocked:
        tab._tree.selection_set("0")
        tab._render_detail()
        app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert heartbeat == [True]
        tab._tree.selection_set("1")
        tab._render_detail()
    pump(app.root, lambda: not app._snapshot_running)
    text = tab._detail.get("1.0", "end")
    assert "Decision 2" in text
    assert "Decision 1" not in text


def test_telegram_start_wait_does_not_block_tk(app):
    started = threading.Event()
    release = threading.Event()
    finished = []

    def start():
        started.set()
        assert release.wait(2)
        return True

    app.bot_service = SimpleNamespace(start=start, stop=lambda: None)
    app._refresh_bot_indicator = lambda: finished.append(threading.current_thread())
    heartbeat = []
    app.root.after(10, lambda: heartbeat.append(True))
    app._start_bot_async()
    assert started.wait(1)
    try:
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert not finished
    finally:
        release.set()
    pump(app.root, lambda: bool(finished))
    assert finished == [app._ui_thread]


def dialog_controls(root):
    window = next(child for child in root.winfo_children() if isinstance(child, tk.Toplevel))

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    widgets = list(descendants(window))
    return (window, next(w for w in widgets if isinstance(w, ttk.Entry)),
            next(w for w in widgets if isinstance(w, tk.Listbox)),
            {w.cget("text"): w for w in widgets if isinstance(w, ttk.Button)})


def close_dialogs(root):
    for child in root.winfo_children():
        if isinstance(child, tk.Toplevel):
            child.destroy()


def test_request_add_lookup_does_not_block_or_duplicate(app, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls, picked, heartbeat = [], [], []
    candidate = object()

    def search(content):
        calls.append(content)
        started.set()
        assert release.wait(2)
        return [candidate]

    monkeypatch.setattr(desktop_app.request_intake, "search_candidates", search)
    def show_picker(*args, on_finished):
        picked.append((args, threading.current_thread()))
        on_finished()

    app._show_candidate_picker = show_picker
    app.request_content_var.set("Example")
    app.root.after(10, lambda: heartbeat.append(True))
    app.add_request_from_ui()
    assert started.wait(1)
    try:
        app.add_request_from_ui()
        app.request_content_var.set("Next request")
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert not picked
    finally:
        release.set()
    pump(app.root, lambda: not app._request_add_pending)
    assert calls == ["Example"]
    assert picked == [(("Example", "Example user", [candidate]), app._ui_thread)]
    assert app.request_content_var.get() == "Next request"
    assert not app._request_add_btn.instate(["disabled"])


def test_request_add_failure_releases_busy_state_for_retry(app, monkeypatch):
    monkeypatch.setattr(desktop_app.request_intake, "search_candidates", lambda _q: [])

    def fail(*args):
        raise RuntimeError("database busy")

    monkeypatch.setattr(desktop_app.request_intake, "add_needs_identity", fail)
    app.request_content_var.set("Example")
    app.add_request_from_ui()
    pump(app.root, lambda: not app._request_add_pending)
    assert "Could not add request" in app.status_var.get()
    assert not app._request_add_btn.instate(["disabled"])
    created = SimpleNamespace(request_id=123, content="Example")
    monkeypatch.setattr(desktop_app.request_intake, "add_needs_identity", lambda *args: created)
    app.add_request_from_ui()
    app.request_content_var.set("Next request")
    pump(app.root, lambda: not app._request_add_pending)
    assert "Queued request #123" in app.status_var.get()
    assert app.request_content_var.get() == "Next request"


def test_modal_search_keeps_tk_responsive_and_rejects_stale_result(app, monkeypatch):
    release, first_finished = threading.Event(), threading.Event()
    calls, rendered, heartbeat = [], [], []
    old, new = SimpleNamespace(title="Old result"), SimpleNamespace(title="New result")

    def search(query, media_type):
        calls.append((query, media_type))
        if query == "old":
            release.wait(2)
            first_finished.set()
            return [old]
        return [new]

    monkeypatch.setattr(desktop_app.request_intake, "format_candidate_label",
                        lambda row: (rendered.append(row.title), row.title)[1])

    def retry():
        _window, entry, results, buttons = dialog_controls(app.root)
        heartbeat.append(not first_finished.is_set())
        entry.delete(0, "end")
        entry.insert(0, "new")
        buttons["Search again"].invoke()
        release.set()

        def pick_when_ready():
            if not results.winfo_exists():
                return
            if results.get(0, "end") == ("New result",):
                buttons["OK"].invoke()
            else:
                app.root.after(10, pick_when_ready)
        pick_when_ready()

    app.root.after(20, retry)
    watchdog = app.root.after(2500, lambda: close_dialogs(app.root))
    try:
        result = app._resolve_identity_dialog("Test search", "old", search_fn=search)
    finally:
        release.set()
        app.root.after_cancel(watchdog)
    pump(app.root, lambda: not app._snapshot_running)
    assert heartbeat == [True]
    assert calls == [("old", None), ("new", None)]
    assert rendered == ["New result"]
    assert result == (new, False, [new])


def test_add_picker_can_receive_an_async_search_result(app, monkeypatch):
    release, retry_finished = threading.Event(), threading.Event()
    heartbeat, shown = [], []
    initial, fresh = SimpleNamespace(title="Initial"), SimpleNamespace(title="Fresh")

    def search(query, _media_type=None):
        if query == "retry":
            release.wait(2)
            retry_finished.set()
            return [fresh]
        return [initial]

    monkeypatch.setattr(desktop_app.request_intake, "search_candidates", search)
    monkeypatch.setattr(desktop_app.request_intake, "format_candidate_label", lambda row: row.title)
    app.request_content_var.set("Example")

    def drive_picker():
        try:
            _window, entry, results, buttons = dialog_controls(app.root)
        except StopIteration:
            app.root.after(10, drive_picker)
            return
        entry.delete(0, "end")
        entry.insert(0, "retry")
        buttons["Search again"].invoke()

        def unblock():
            heartbeat.append(not retry_finished.is_set())
            release.set()

        def close_when_ready():
            if not results.winfo_exists():
                return
            if results.get(0, "end")[0:1] == ("Fresh",):
                shown.append(True)
                buttons["Cancel"].invoke()
            else:
                app.root.after(10, close_when_ready)
        app.root.after(20, unblock)
        close_when_ready()

    app.root.after(20, drive_picker)
    watchdog = app.root.after(2500, lambda: close_dialogs(app.root))
    try:
        app.add_request_from_ui()
        pump(app.root, lambda: not app._request_add_pending, seconds=3)
    finally:
        release.set()
        app.root.after_cancel(watchdog)
    pump(app.root, lambda: not app._snapshot_running)
    assert heartbeat == [True]
    assert shown == [True], "the modal picker prevented queued results from being delivered"


def test_modal_close_discards_inflight_lookup(app, monkeypatch):
    release = threading.Event()
    rendered = []

    def search(*args):
        release.wait(2)
        return [SimpleNamespace(title="Too late")]

    monkeypatch.setattr(desktop_app.request_intake, "format_candidate_label",
                        lambda row: (rendered.append(row.title), row.title)[1])
    app.root.after(20, lambda: dialog_controls(app.root)[3]["Cancel"].invoke())
    watchdog = app.root.after(2500, lambda: close_dialogs(app.root))
    try:
        result = app._resolve_identity_dialog("Test close", "Example", search_fn=search)
    finally:
        release.set()
        app.root.after_cancel(watchdog)
    pump(app.root, lambda: not app._snapshot_running)
    assert result == (None, False, [])
    assert rendered == []


def test_action_status_lookup_does_not_block_completion_or_tk(app, monkeypatch):
    release = threading.Event()
    heartbeat, refreshes = [], []

    def status():
        assert release.wait(2)
        return "Plex status result"

    monkeypatch.setattr(desktop_app, "get_status", status)
    app.refresh_requests = lambda: refreshes.append("requests")
    app.refresh_library_summary = lambda: refreshes.append("summary")
    app.refresh_library_metrics = lambda: refreshes.append("metrics")
    app.root.after(10, lambda: heartbeat.append(True))
    app._handle_action_result("Example action", "Action completed",
                              show_popup=False, update_status_only=False)
    try:
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert refreshes == ["requests", "summary", "metrics"]
        assert app.status_var.get() == "Action completed"
        assert not app.status_text.get("1.0", "end").strip()
    finally:
        release.set()
    pump(app.root, lambda: not app._snapshot_running)
    assert app.status_text.get("1.0", "end").strip() == "Plex status result"


def configure_tv_pick(app, monkeypatch):
    from tkinter import simpledialog

    candidate = MediaResult(
        title="UI responsiveness example", year=2026, external_id="999999",
        external_url="", media_type="tv", overview="", source="tmdb")
    monkeypatch.setattr(desktop_app.request_intake, "search_candidates", lambda _q: [candidate])
    app._resolve_identity_dialog = lambda *args, **kwargs: (candidate, False, [candidate])
    monkeypatch.setattr(simpledialog, "askstring", lambda *args, **kwargs: "all")
    app.request_content_var.set(candidate.title)
    return candidate


def test_confirmed_tv_add_stays_responsive_and_busy_through_filing(app, monkeypatch):
    candidate = configure_tv_pick(app, monkeypatch)
    season_started, season_release, filing_started = (threading.Event() for _ in range(3))
    worker_threads, heartbeat, searches = [], [], []
    real_filing = desktop_app.request_intake.add_picked_candidate

    def search(_query):
        searches.append(True)
        return [candidate]

    def seasons(answer, match):
        worker_threads.append(threading.current_thread())
        season_started.set()
        assert season_release.wait(2)
        assert answer == "all" and match is candidate
        return [1, 2]

    def file_request(*args, **kwargs):
        worker_threads.append(threading.current_thread())
        filing_started.set()
        return real_filing(*args, **kwargs)

    monkeypatch.setattr(desktop_app.request_intake, "search_candidates", search)
    monkeypatch.setattr(desktop_app.request_intake, "seasons_for_answer", seasons)
    monkeypatch.setattr(desktop_app.request_intake, "add_picked_candidate", file_request)
    try:
        with held_store_lock(queue_store._DB_LOCK) as unlocked:
            app.add_request_from_ui()
            pump(app.root, season_started.is_set)
            app.root.after(10, lambda: heartbeat.append(not season_release.is_set()))
            pump(app.root, lambda: len(heartbeat) == 1, seconds=0.5)
            assert app._request_add_pending
            assert app._request_add_btn.instate(["disabled"])
            app.add_request_from_ui()
            app.request_content_var.set("Next request")
            season_release.set()
            pump(app.root, filing_started.is_set)
            app.root.after(10, lambda: heartbeat.append(not unlocked.is_set()))
            pump(app.root, lambda: len(heartbeat) == 2, seconds=0.5)
            assert app._request_add_pending, "Add was re-enabled before filing completed"
            assert app._request_add_btn.instate(["disabled"])
    finally:
        season_release.set()
    pump(app.root, lambda: not app._request_add_pending)
    assert heartbeat == [True, True]
    assert searches == [True], "a second Add click started another operation"
    assert all(thread is not app._ui_thread for thread in worker_threads)
    assert app.request_content_var.get() == "Next request"
    assert "Queued 2 request(s)" in app.status_var.get()
    rows = [row for row in queue_store.list_requests(status="all", limit=200)
            if row.external_id == candidate.external_id]
    assert sorted(row.season for row in rows) == [1, 2]


@pytest.mark.parametrize("stage", ["seasons", "filing"])
def test_confirmed_add_failure_keeps_tk_alive_and_allows_retry(app, monkeypatch, stage):
    candidate = configure_tv_pick(app, monkeypatch)
    started, release = threading.Event(), threading.Event()
    heartbeat = []

    def fail(*args, **kwargs):
        assert threading.current_thread() is not app._ui_thread
        started.set()
        assert release.wait(2)
        raise RuntimeError(f"{stage} unavailable")

    monkeypatch.setattr(desktop_app.request_intake, "seasons_for_answer",
                        fail if stage == "seasons" else lambda *args: [1])
    monkeypatch.setattr(desktop_app.request_intake, "add_picked_candidate", fail)
    app.add_request_from_ui()
    try:
        pump(app.root, started.is_set)
        app.root.after(10, lambda: heartbeat.append(True))
        pump(app.root, lambda: bool(heartbeat), seconds=0.5)
        assert app._request_add_pending
        assert app._request_add_btn.instate(["disabled"])
    finally:
        release.set()
    pump(app.root, lambda: not app._request_add_pending)
    assert f"Could not add request: {stage} unavailable" in app.status_var.get()
    assert app.request_content_var.get() == candidate.title
    assert not app._request_add_btn.instate(["disabled"])
    monkeypatch.setattr(desktop_app.request_intake, "seasons_for_answer", lambda *args: [1])
    monkeypatch.setattr(desktop_app.request_intake, "add_picked_candidate",
                        lambda *args, **kwargs: desktop_app.request_intake.IntakeResult((123,), "open"))
    app.add_request_from_ui()
    pump(app.root, lambda: not app._request_add_pending)
    assert "Queued 1 request(s)" in app.status_var.get()
    assert app.request_content_var.get() == ""


@pytest.mark.parametrize("stage", ["picker", "season_prompt"])
def test_cancelled_add_never_expands_seasons_or_files_request(app, monkeypatch, stage):
    from tkinter import simpledialog

    candidate = configure_tv_pick(app, monkeypatch)
    if stage == "picker":
        app._resolve_identity_dialog = lambda *args, **kwargs: (None, False, [candidate])
    else:
        monkeypatch.setattr(simpledialog, "askstring", lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(desktop_app.request_intake, "seasons_for_answer",
                        lambda *args: calls.append("seasons"))
    monkeypatch.setattr(desktop_app.request_intake, "add_picked_candidate",
                        lambda *args, **kwargs: calls.append("filing"))
    app.add_request_from_ui()
    pump(app.root, lambda: not app._request_add_pending)
    assert calls == []
    assert app.status_var.get() == "Request not added."
    assert app.request_content_var.get() == candidate.title
    assert not app._request_add_btn.instate(["disabled"])
