# =============================================================================
# maint_jobs.py
# =============================================================================
# Maintenance run model (Task I): MaintJob + JobRegistry.
#
# Before this module, every Maintenance tool ran in a bare daemon thread: no
# progress, no completion record, no cancel, results clobbered by a tab
# switch, and a restart erased any evidence a run ever happened. Now:
#
#   - The registry is process-wide and owns ONE worker thread. Exactly one
#     job runs at a time; further submissions queue visibly
#     (RESOLVED DECISION 8).
#   - Every job function takes (progress_callback, cancel_check). Progress is
#     honest: counts where countable, phase text where not — the registry
#     never invents a percentage.
#   - Cancel is cooperative: a job that checks cancel_check() raises
#     JobCancelled with how far it got, and the summary says "cancelled after
#     N items". A job that cannot check mid-phase finishes its phase first —
#     the record reflects what actually happened.
#   - Every run is journalled in SQLite (maintenance_jobs). On startup, any
#     persisted 'running' or 'queued' row becomes 'interrupted' — a daemon
#     thread never survives a restart and the journal must not pretend it did.
#
# This module is UI-free on purpose: tests drive it without Tk, and the
# desktop app subscribes with a listener that marshals events onto the Tk
# thread itself.
# =============================================================================

import datetime
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import db

logger = logging.getLogger(__name__)

# Job lifecycle states, as persisted in maintenance_jobs.state:
#   queued -> running -> done | failed | cancelled
#   queued -> cancelled                  (cancelled before it ever started)
#   queued/running -> interrupted        (app restarted mid-run; set at init)
JOB_STATES = ("queued", "running", "done", "failed", "cancelled", "interrupted")

# Schema exactly per TORRENT_SELECTION_BOOTSTRAP_v2.md section 11 item 2.
# Timestamps are stored UTC ("%Y-%m-%d %H:%M:%S"); anything displayed goes
# through ui_helpers.local_ts like every other stored timestamp in the app.
_SCHEMA_JOBS = """
CREATE TABLE IF NOT EXISTS maintenance_jobs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key               TEXT NOT NULL,
    label                  TEXT NOT NULL,
    state                  TEXT NOT NULL DEFAULT 'queued',
    queued_at              TEXT NOT NULL,
    started_at             TEXT,
    finished_at            TEXT,
    progress_current       INTEGER,
    progress_total         INTEGER,
    phase                  TEXT,
    summary_json           TEXT,
    error_json             TEXT,
    cancellation_requested INTEGER NOT NULL DEFAULT 0
)
"""


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def initialize_maint_jobs_db() -> int:
    """Create the journal table and mark stale rows interrupted.

    Any job persisted as 'running' (or still 'queued') belongs to a previous
    process — its thread is gone. Returns how many rows were flipped.
    """
    with db.connect() as conn:
        conn.execute(_SCHEMA_JOBS)
        cur = conn.execute(
            "UPDATE maintenance_jobs SET state = 'interrupted', "
            "finished_at = COALESCE(finished_at, ?), "
            "summary_json = COALESCE(summary_json, ?) "
            "WHERE state IN ('running', 'queued')",
            (_utcnow(),
             json.dumps({"note": "app closed while this run was pending"})),
        )
        conn.commit()
        flipped = cur.rowcount or 0
    if flipped:
        logger.info("Maintenance journal: %d stale run(s) marked interrupted.",
                    flipped)
    return flipped


class JobCancelled(Exception):
    """Raised by a job function when cancel_check() came back True.

    items_done feeds the honest "cancelled after N items" summary; leave it
    None when the job had no countable progress yet.
    """

    def __init__(self, message: str = "cancelled", *,
                 items_done: int | None = None) -> None:
        super().__init__(message)
        self.items_done = items_done


@dataclass
class JobResult:
    """Optional richer return value for a job function.

    summary  — small JSON-able dict for the durable journal.
    result   — the full in-memory payload (result trees can be huge and
               are cached separately; the journal only keeps the summary).
    """
    summary: dict[str, Any] = field(default_factory=dict)
    result: Any = None


@dataclass
class MaintJob:
    """One maintenance run — live object while in memory, journalled row
    forever."""
    job_id: int
    tool_key: str
    label: str
    fn: Callable[[Callable[..., None], Callable[[], bool]], Any]
    meta: dict[str, Any] = field(default_factory=dict)
    state: str = "queued"
    queued_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    phase: str = ""
    summary: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancellation_requested: bool = False
    result: Any = None                    # in-memory only, never persisted
    started_monotonic: float | None = None  # for the elapsed ticker

    def elapsed_seconds(self) -> float | None:
        if self.started_monotonic is None:
            return None
        return time.monotonic() - self.started_monotonic


# Listener signature: listener(event: str, job: MaintJob).
# Events: "queued", "started", "progress", "cancel_requested", "finished",
# and "idle" (the queue drained after the given job — repaint to the resting
# state). Listeners are called on the REGISTRY WORKER THREAD (or the
# submitting thread for "queued"); UI code must marshal to its own thread.
Listener = Callable[[str, MaintJob], None]


class JobRegistry:
    """Process-wide maintenance run registry. One job at a time; the rest
    queue in submission order."""

    def __init__(self, *, progress_min_interval: float = 0.2,
                 persist_min_interval: float = 0.5) -> None:
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._queue: deque[MaintJob] = deque()
        self._current: MaintJob | None = None
        self._listeners: list[Listener] = []
        self._worker: threading.Thread | None = None
        self._stopping = False
        # Throttles: a 30k-item walk must not turn into 30k Tk callbacks or
        # 30k SQLite writes. Phase changes always go through.
        self._progress_min_interval = progress_min_interval
        self._persist_min_interval = persist_min_interval

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _emit(self, event: str, job: MaintJob) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event, job)
            except Exception:
                logger.exception("Maintenance job listener failed (%s).", event)

    # ------------------------------------------------------------------
    # Submission / cancel
    # ------------------------------------------------------------------

    def submit(self, tool_key: str, label: str,
               fn: Callable[[Callable[..., None], Callable[[], bool]], Any],
               *, meta: dict[str, Any] | None = None,
               dedupe: bool = True) -> MaintJob:
        """Queue a run. With dedupe (default), a tool already queued or
        running is returned as-is instead of queuing a twin — double-clicking
        a tool button must not schedule the walk twice."""
        with self._lock:
            if dedupe:
                for existing in ([self._current] if self._current else []) + \
                        list(self._queue):
                    if (existing.tool_key == tool_key
                            and existing.state in ("queued", "running")):
                        return existing
            queued_at = _utcnow()
            with db.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO maintenance_jobs "
                    "(tool_key, label, state, queued_at) "
                    "VALUES (?, ?, 'queued', ?)",
                    (tool_key, label, queued_at))
                conn.commit()
                job_id = int(cur.lastrowid or 0)
            job = MaintJob(job_id=job_id, tool_key=tool_key, label=label,
                           fn=fn, meta=dict(meta or {}), queued_at=queued_at)
            self._queue.append(job)
            self._ensure_worker()
            # Emit while still holding the lock: the worker can't pop the job
            # yet, so "queued" is guaranteed to precede "started". Listeners
            # must not block on the worker thread from inside a callback.
            self._emit("queued", job)
        self._wake.set()
        return job

    def request_cancel(self, job_id: int) -> bool:
        """Ask a job to stop. A queued job is cancelled immediately; a
        running one gets the flag and stops at its next cancel_check()."""
        with self._lock:
            if self._current is not None and self._current.job_id == job_id:
                job = self._current
                job.cancellation_requested = True
                self._persist(job, cancellation_requested=1)
                emit_evt, emit_job = "cancel_requested", job
            else:
                queued = next((j for j in self._queue if j.job_id == job_id),
                              None)
                if queued is None:
                    return False
                self._queue.remove(queued)
                queued.cancellation_requested = True
                queued.state = "cancelled"
                queued.finished_at = _utcnow()
                queued.summary = {"note": "cancelled before it started"}
                self._persist(queued, state="cancelled",
                              finished_at=queued.finished_at,
                              summary_json=json.dumps(queued.summary),
                              cancellation_requested=1)
                self._idle.notify_all()
                emit_evt, emit_job = "finished", queued
        self._emit(emit_evt, emit_job)
        return True

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def current_job(self) -> MaintJob | None:
        with self._lock:
            return self._current

    def queued_jobs(self) -> list[MaintJob]:
        with self._lock:
            return list(self._queue)

    def snapshot(self) -> tuple[MaintJob | None, list[MaintJob]]:
        """(running job, queued jobs) — what a rebuilt UI paints from."""
        with self._lock:
            return self._current, list(self._queue)

    def is_idle(self) -> bool:
        with self._lock:
            return self._current is None and not self._queue

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until nothing is running or queued (tests + shutdown)."""
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._current is not None or self._queue:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker thread once the current job finishes (tests)."""
        with self._lock:
            self._stopping = True
        self._wake.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    @staticmethod
    def history(limit: int = 100) -> list[dict[str, Any]]:
        """Journalled runs, newest first — the 'Run history' view reads this."""
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, tool_key, label, state, queued_at, started_at, "
                "finished_at, progress_current, progress_total, phase, "
                "summary_json, error_json, cancellation_requested "
                "FROM maintenance_jobs ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        cols = ("id", "tool_key", "label", "state", "queued_at", "started_at",
                "finished_at", "progress_current", "progress_total", "phase",
                "summary_json", "error_json", "cancellation_requested")
        return [dict(zip(cols, row)) for row in rows]

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop, name="maint-jobs", daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                job = self._queue.popleft() if self._queue else None
                if job is not None:
                    self._current = job
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            queue_drained = False
            try:
                self._run_job(job)  # emits "finished" while still current
            finally:
                with self._idle:
                    self._current = None
                    queue_drained = not self._queue
                    self._idle.notify_all()
            if queue_drained:
                # The UI resets its progress strip on this, not on
                # "finished" — the next job's "started" may already be
                # racing in when the queue is not empty.
                self._emit("idle", job)

    def _run_job(self, job: MaintJob) -> None:
        job.state = "running"
        job.started_at = _utcnow()
        job.started_monotonic = time.monotonic()
        self._persist(job, state="running", started_at=job.started_at)
        self._emit("started", job)

        last_emit = 0.0
        last_persist = 0.0

        def progress(current: int | None = None, total: int | None = None,
                     phase: str | None = None) -> None:
            nonlocal last_emit, last_persist
            phase_changed = phase is not None and phase != job.phase
            if current is not None:
                job.progress_current = int(current)
            if total is not None:
                job.progress_total = int(total)
            if phase is not None:
                job.phase = phase
            now = time.monotonic()
            if phase_changed or now - last_persist >= self._persist_min_interval:
                last_persist = now
                self._persist(job, progress_current=job.progress_current,
                              progress_total=job.progress_total,
                              phase=job.phase)
            if phase_changed or now - last_emit >= self._progress_min_interval:
                last_emit = now
                self._emit("progress", job)

        def cancel_check() -> bool:
            return job.cancellation_requested

        try:
            outcome = job.fn(progress, cancel_check)
        except JobCancelled as exc:
            items = exc.items_done
            if items is None:
                items = job.progress_current
            job.state = "cancelled"
            job.summary = {"cancelled_after": items,
                           "message": (f"cancelled after {items} item(s)"
                                       if items is not None
                                       else str(exc) or "cancelled")}
        except Exception as exc:
            logger.exception("Maintenance job %s (%s) failed.",
                             job.job_id, job.tool_key)
            job.state = "failed"
            job.error = {"type": type(exc).__name__, "message": str(exc)}
        else:
            if isinstance(outcome, JobResult):
                job.summary = dict(outcome.summary)
                job.result = outcome.result
            elif isinstance(outcome, dict):
                job.summary = outcome
                job.result = outcome
            else:
                job.summary = {}
                job.result = outcome
            # Cooperative cancel: if the flag was raised but the job ran to
            # the end anyway, the work IS done — record done, note the race.
            if job.cancellation_requested:
                job.summary.setdefault(
                    "note", "cancel was requested but the run had already "
                            "finished its work")
            job.state = "done"

        job.finished_at = _utcnow()
        self._persist(
            job,
            state=job.state,
            finished_at=job.finished_at,
            progress_current=job.progress_current,
            progress_total=job.progress_total,
            phase=job.phase,
            summary_json=(json.dumps(job.summary)
                          if job.summary is not None else None),
            error_json=(json.dumps(job.error)
                        if job.error is not None else None),
            cancellation_requested=int(job.cancellation_requested),
        )
        self._emit("finished", job)

    @staticmethod
    def _persist(job: MaintJob, **columns: Any) -> None:
        if not columns:
            return
        # Column names here are static internal identifiers (this module's
        # own keyword arguments) — never user input.
        sets = ", ".join(f"{col} = ?" for col in columns)
        try:
            with db.connect() as conn:
                conn.execute(
                    f"UPDATE maintenance_jobs SET {sets} WHERE id = ?",
                    (*columns.values(), job.job_id))
                conn.commit()
        except Exception:
            logger.debug("Maintenance journal update failed for job %s.",
                         job.job_id, exc_info=True)


# ---------------------------------------------------------------------------
# Process-wide singleton — the desktop app and the idle pass share it so the
# "one at a time" rule holds across every submission path.
# ---------------------------------------------------------------------------

_registry: JobRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> JobRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = JobRegistry()
        return _registry
