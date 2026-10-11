# =============================================================================
# Worker threads must never call into Tk. On 2026-10-10 five download threads
# calling root.after() at once hung the whole window (py-spy showed every
# torrent-dl thread parked in tkinter's _register). _post_to_ui now queues
# from workers and the Tk thread drains the queue.
# =============================================================================
import queue
import threading
import time

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture()
def shim():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display for Tk")
    root.withdraw()
    import desktop_app

    class Shim:
        _post_to_ui = desktop_app.DesktopApp._post_to_ui
        _drain_ui_queue = desktop_app.DesktopApp._drain_ui_queue

    s = Shim()
    s.root = root
    s._ui_thread = threading.current_thread()
    s._ui_queue = queue.Queue()
    s._quitting = False
    yield s
    s._quitting = True
    root.destroy()


def _pump(root, until, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline and not until():
        root.update()
        time.sleep(0.01)


def test_worker_callbacks_run_on_the_tk_thread(shim):
    ran_on: list = []
    calls = [threading.Thread(target=lambda: shim._post_to_ui(
        lambda: ran_on.append(threading.current_thread()))) for _ in range(20)]
    for t in calls:
        t.start()
    for t in calls:
        t.join(5)
    assert not ran_on, "a worker ran Tk work itself"
    assert shim._ui_queue.qsize() == 20
    shim.root.after(0, shim._drain_ui_queue)
    _pump(shim.root, lambda: len(ran_on) == 20)
    assert len(ran_on) == 20
    assert all(t is shim._ui_thread for t in ran_on)


def test_a_failing_callback_does_not_stop_the_rest(shim):
    done: list = []

    def boom():
        raise ValueError("x")
    worker = threading.Thread(target=lambda: (shim._post_to_ui(boom),
                                              shim._post_to_ui(lambda: done.append(1))))
    worker.start()
    worker.join(5)
    shim.root.after(0, shim._drain_ui_queue)
    _pump(shim.root, lambda: done)
    assert done == [1]
