# =============================================================================
# Task D — downloads stuck at 0%.
#
# Two behaviours are pinned here:
#   1. A download that makes no progress through DOWNLOAD_MAX_ROTATIONS rotations
#      is declared stalled (status 'error') instead of cycling
#      queued -> downloading -> 0% -> queued forever. The 'error' status engages
#      workstream A's resolver, so the request re-opens (below the attempt cap)
#      to grab a DIFFERENT release rather than the same dead magnet.
#   2. The queue-status display maps a bare status + 0% into an honest label
#      (fetching metadata / no peers / stalled / waiting for slot / probing).
# =============================================================================

import time

import pytest

import config
import db
import downloads_store
import queue_store
from download_manager import DownloadManager


def _clean_db():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "blocklist", "download_files",
                      "request_downloads"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    _clean_db()
    yield
    _clean_db()


def _stalled_pass(dm: DownloadManager, download_id: int) -> None:
    """Force one queue-monitor pass where `download_id` looks fully stalled:
    a 'downloading' row whose last-seen progress is stamped at the epoch, so the
    idle window is always exceeded regardless of the configured threshold."""
    downloads_store.set_status(download_id, "downloading")
    dm._progress_seen[download_id] = (0.0, 0.0)
    dm._queue_monitor_pass()


def test_stalled_download_errors_after_max_rotations_and_reopens_request(monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_MAX_ROTATIONS", 2)

    req = queue_store.add_request(
        "Inception", "cole", media_type="movie",
        status=queue_store.STATUS_GRABBING)
    did = downloads_store.create_download(
        title="Inception.2010.1080p.WEB.x264-DEAD",
        magnet="magnet:?xt=urn:btih:" + "d" * 40, source="tpb",
        media_type="movie", request_id=req.request_id, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False)

    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(dm, "_notify", lambda *a, **k: None)

    # First stall: rotates back to the queue (rotation 1 of 2), still recoverable.
    _stalled_pass(dm, did)
    assert downloads_store.get_download(did).status == "queued"
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_GRABBING

    # Second stall: DOWNLOAD_MAX_ROTATIONS reached — the download errors out.
    _stalled_pass(dm, did)
    row = downloads_store.get_download(did)
    assert row.status == "error"
    assert "stalled" in (row.error or "").lower()

    # Workstream A's resolver saw an 'error' terminal state and re-opened the
    # request (one failure, below the cap) so a different release can be tried.
    req_now = queue_store.get_request(req.request_id)
    assert req_now.status == queue_store.STATUS_OPEN
    # The dead magnet is remembered so the re-grab avoids it.
    assert downloads_store.request_grab_attempts(req.request_id) == 1


def test_rotate_window_never_preempts_the_runner_stall_timeout(monkeypatch):
    # The Node runner errors a truly dead download at TORRENT_STALL_TIMEOUT_SECONDS.
    # The monitor's rotate window must be at least that long (+ slack) so the
    # runner's own error fires first instead of a premature rotation masking it.
    monkeypatch.setattr(config, "DOWNLOAD_SLOW_ROTATE_MINUTES", 10)   # 600s
    monkeypatch.setattr(config, "TORRENT_STALL_TIMEOUT_SECONDS", 900)
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(dm, "_notify", lambda *a, **k: None)

    did = downloads_store.create_download(
        title="X", magnet="magnet:?xt=urn:btih:" + "e" * 40, source="tpb",
        media_type="movie", request_id=None, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False)
    downloads_store.set_status(did, "downloading")
    # Idle for 700s: past the raw 600s rotate knob, but under the 900s runner
    # stall timeout — so the monitor must NOT rotate yet (only one download, so
    # the no-contention window is 3x anyway, but even the base window holds).
    now = time.time()
    dm._progress_seen[did] = (0.0, now - 700)
    dm._queue_monitor_pass()
    assert downloads_store.get_download(did).status == "downloading"


# ---------------------------------------------------------------------------
# Honest queue-status labels instead of a bare status + 0%.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,progress,error,phase,expected", [
    ("queued", 0.0, None, None, "waiting for slot"),
    ("downloading", 0.0, None, "fetching_metadata", "fetching metadata"),
    ("downloading", 0.0, None, "no_peers", "no peers"),
    ("downloading", 0.42, None, None, "downloading"),
    ("downloading", 0.0, None, "probing", "probing"),
    ("queued", 0.0, None, "probing", "probing"),
    ("error", 0.0, "stalled: no progress after 2 rotations", None, "stalled"),
    ("error", 0.0, "permission denied", None, "error"),
    ("moved", 1.0, None, None, "moved"),
])
def test_display_status_labels(status, progress, error, phase, expected):
    assert downloads_store.display_status(
        status, progress, error=error, phase=phase) == expected
