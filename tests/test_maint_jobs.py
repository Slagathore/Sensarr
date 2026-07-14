# =============================================================================
# tests/test_maint_jobs.py
# =============================================================================
# Task I — the maintenance job registry: honest progress, one-at-a-time with
# a visible queue, cooperative cancel, durable SQLite history, survival of a
# simulated tab switch, and interrupted-on-restart. No Tk anywhere: the
# registry is UI-free by design and these tests prove the UI can be rebuilt
# from registry state alone.
# =============================================================================

import json
import threading
import time

import pytest

import db
import maint_jobs
from maint_jobs import JobCancelled, JobRegistry, JobResult


@pytest.fixture(autouse=True)
def _jobs_db():
    maint_jobs.initialize_maint_jobs_db()


@pytest.fixture()
def registry():
    reg = JobRegistry(progress_min_interval=0, persist_min_interval=0)
    yield reg
    reg.stop(timeout=5)


class Recorder:
    """Thread-safe event log: [(event, job_id, state), ...]."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[tuple[str, int, str]] = []

    def __call__(self, event, job) -> None:
        with self.lock:
            self.events.append((event, job.job_id, job.state))

    def names(self) -> list[str]:
        with self.lock:
            return [e[0] for e in self.events]


def _db_row(job_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT state, progress_current, progress_total, phase, "
            "summary_json, error_json, started_at, finished_at, "
            "cancellation_requested FROM maintenance_jobs WHERE id = ?",
            (job_id,)).fetchone()
    assert row is not None, f"job {job_id} missing from the journal"
    keys = ("state", "progress_current", "progress_total", "phase",
            "summary_json", "error_json", "started_at", "finished_at",
            "cancellation_requested")
    return dict(zip(keys, row))


def test_job_emits_progress_and_journals_done(registry):
    rec = Recorder()
    registry.subscribe(rec)

    def job(progress, cancel_check):
        for i in range(5):
            progress(i + 1, 5, phase=f"item {i + 1}")
        return JobResult(summary={"items": 5}, result=["a", "b"])

    j = registry.submit("t_progress", "Progress job", job)
    assert registry.wait_idle(10)

    assert j.state == "done"
    assert j.result == ["a", "b"]
    assert j.progress_current == 5 and j.progress_total == 5
    names = rec.names()
    assert names[0] == "queued"
    assert "started" in names and "progress" in names
    # "finished" fires while the job is still current; a trailing "idle"
    # (queue drained) may or may not have landed yet.
    assert "finished" in names
    assert names.index("finished") > names.index("started")

    row = _db_row(j.job_id)
    assert row["state"] == "done"
    assert row["progress_current"] == 5 and row["progress_total"] == 5
    assert json.loads(row["summary_json"]) == {"items": 5}
    assert row["started_at"] and row["finished_at"]


def test_failing_job_is_recorded_as_loudly_as_success(registry):
    rec = Recorder()
    registry.subscribe(rec)

    def job(progress, cancel_check):
        raise ValueError("boom went the walk")

    j = registry.submit("t_fail", "Failing job", job)
    assert registry.wait_idle(10)

    assert j.state == "failed"
    assert j.error == {"type": "ValueError", "message": "boom went the walk"}
    row = _db_row(j.job_id)
    assert row["state"] == "failed"
    assert "boom went the walk" in row["error_json"]
    assert row["finished_at"]
    assert "finished" in rec.names()


def test_cancel_mid_run_reports_honest_item_count(registry):
    first_item = threading.Event()

    def job(progress, cancel_check):
        for i in range(10_000):
            if cancel_check():
                raise JobCancelled(items_done=i)
            progress(i + 1, 10_000, phase="crunching")
            first_item.set()
            time.sleep(0.002)
        return JobResult(summary={"items": 10_000})

    j = registry.submit("t_cancel", "Cancellable job", job)
    assert first_item.wait(10), "job never started producing items"
    assert registry.request_cancel(j.job_id)
    assert registry.wait_idle(10)

    assert j.state == "cancelled"
    assert j.summary is not None
    done = j.summary["cancelled_after"]
    assert isinstance(done, int) and 0 <= done < 10_000
    assert f"cancelled after {done} item(s)" == j.summary["message"]
    row = _db_row(j.job_id)
    assert row["state"] == "cancelled"
    assert row["cancellation_requested"] == 1


def test_second_job_queues_and_only_one_runs_at_a_time(registry):
    release = threading.Event()
    running = threading.Event()
    concurrency = {"now": 0, "max": 0}
    lock = threading.Lock()
    order: list[str] = []

    def make_job(name, blocking):
        def job(progress, cancel_check):
            with lock:
                concurrency["now"] += 1
                concurrency["max"] = max(concurrency["max"],
                                         concurrency["now"])
                order.append(name)
            if blocking:
                running.set()
                assert release.wait(10)
            with lock:
                concurrency["now"] -= 1
            return JobResult(summary={"name": name})
        return job

    a = registry.submit("t_queue_a", "Job A", make_job("a", True))
    assert running.wait(10)
    b = registry.submit("t_queue_b", "Job B", make_job("b", False))

    # While A runs, B is journalled and visible as queued — the UI's queue
    # label and the header indicator read exactly this.
    assert b.state == "queued"
    current, queued = registry.snapshot()
    assert current is a
    assert [j.job_id for j in queued] == [b.job_id]
    assert _db_row(b.job_id)["state"] == "queued"

    release.set()
    assert registry.wait_idle(10)
    assert a.state == "done" and b.state == "done"
    assert order == ["a", "b"]
    assert concurrency["max"] == 1


def test_cancel_of_a_queued_job_prevents_it_from_running(registry):
    release = threading.Event()
    running = threading.Event()
    ran = {"b": False}

    def job_a(progress, cancel_check):
        running.set()
        assert release.wait(10)
        return {}

    def job_b(progress, cancel_check):
        ran["b"] = True
        return {}

    registry.submit("t_precancel_a", "Blocker", job_a)
    assert running.wait(10)
    b = registry.submit("t_precancel_b", "Never runs", job_b)
    assert registry.request_cancel(b.job_id)
    release.set()
    assert registry.wait_idle(10)

    assert ran["b"] is False
    assert b.state == "cancelled"
    row = _db_row(b.job_id)
    assert row["state"] == "cancelled"
    assert "before it started" in row["summary_json"]


def test_dedupe_returns_the_existing_queued_job(registry):
    release = threading.Event()
    running = threading.Event()

    def blocker(progress, cancel_check):
        running.set()
        assert release.wait(10)
        return {}

    a = registry.submit("t_dedupe", "Blocker", blocker)
    assert running.wait(10)
    again = registry.submit("t_dedupe", "Blocker", blocker)
    assert again.job_id == a.job_id  # double-click does not queue a twin
    other = registry.submit("t_dedupe_other", "Other", lambda p, c: {})
    assert other.job_id != a.job_id
    release.set()
    assert registry.wait_idle(10)


def test_long_job_survives_a_simulated_tab_switch(registry):
    """Tearing down and rebuilding the UI (unsubscribe + resubscribe) loses
    nothing: the registry still holds the running job with live progress,
    and the new subscriber sees completion."""
    release = threading.Event()
    progressed = threading.Event()

    def job(progress, cancel_check):
        progress(3, 10, phase="halfway-ish")
        progressed.set()
        assert release.wait(10)
        progress(10, 10, phase="done")
        return JobResult(summary={"items": 10})

    first_listener = Recorder()
    registry.subscribe(first_listener)
    j = registry.submit("t_tabswitch", "Long job", job)
    assert progressed.wait(10)

    # "Tab switch": the old UI listener goes away entirely.
    registry.unsubscribe(first_listener)

    # "Tab return": a fresh view rebuilds purely from registry state.
    current, queued = registry.snapshot()
    assert current is j and queued == []
    assert current.state == "running"
    assert current.progress_current == 3 and current.progress_total == 10
    assert current.phase == "halfway-ish"
    assert current.elapsed_seconds() is not None

    second_listener = Recorder()
    registry.subscribe(second_listener)
    release.set()
    assert registry.wait_idle(10)
    assert j.state == "done"
    assert "finished" in second_listener.names()


def test_noncooperating_job_that_finishes_after_cancel_stays_honest(registry):
    """A job that never checks cancel_check() runs to completion — the
    journal says done (the work happened) and notes the cancel race."""
    release = threading.Event()
    running = threading.Event()

    def job(progress, cancel_check):
        running.set()
        assert release.wait(10)
        return JobResult(summary={"items": 7})

    j = registry.submit("t_noncoop", "Stubborn job", job)
    assert running.wait(10)
    registry.request_cancel(j.job_id)
    release.set()
    assert registry.wait_idle(10)

    assert j.state == "done"
    assert j.summary is not None and j.summary["items"] == 7
    assert "already finished" in j.summary["note"]


def test_interrupted_on_restart():
    """Rows persisted as running/queued by a dead process become
    'interrupted' at init — the journal never pretends a daemon thread
    survived a restart."""
    with db.connect() as conn:
        conn.execute(maint_jobs._SCHEMA_JOBS)
        cur = conn.execute(
            "INSERT INTO maintenance_jobs (tool_key, label, state, queued_at, "
            "started_at) VALUES ('t_restart', 'Zombie run', 'running', "
            "'2026-07-13 03:00:00', '2026-07-13 03:00:05')")
        running_id = int(cur.lastrowid or 0)
        cur = conn.execute(
            "INSERT INTO maintenance_jobs (tool_key, label, state, queued_at) "
            "VALUES ('t_restart', 'Never-started run', 'queued', "
            "'2026-07-13 03:00:01')")
        queued_id = int(cur.lastrowid or 0)
        conn.commit()

    flipped = maint_jobs.initialize_maint_jobs_db()
    assert flipped >= 2

    for job_id in (running_id, queued_id):
        row = _db_row(job_id)
        assert row["state"] == "interrupted"
        assert row["finished_at"], "interrupted rows must not look open-ended"


def test_history_lists_newest_first(registry):
    for n in range(3):
        registry.submit(f"t_hist_{n}", f"History job {n}", lambda p, c: {})
    assert registry.wait_idle(10)

    rows = registry.history(limit=200)
    ids = [r["id"] for r in rows]
    assert ids == sorted(ids, reverse=True)
    ours = [r for r in rows if r["tool_key"].startswith("t_hist_")]
    assert len(ours) == 3
    assert all(r["state"] == "done" for r in ours)
