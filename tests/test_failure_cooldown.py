# =============================================================================
# Repeat failures stretch the cooldown instead of returning every week.
#
# Through 2026-10-09 a stalled release was skipped for exactly seven days and
# then came back as if it had never failed: Petals on the Wind stalled on
# 09-17, 09-24 and 10-01, one Panty & Stocking release stalled ten times.
# =============================================================================

import time

import pytest

import db
import download_manager
import downloads_store
from download_manager import DownloadManager, failure_cooldown_seconds

DAY = 24 * 3600.0
HASH_A = "a" * 40
HASH_B = "b" * 40


@pytest.fixture(autouse=True)
def _fresh():
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("downloads", "download_history", "failed_grabs"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    yield


def _age(key: str, ih: str, days: float) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE failed_grabs SET failed_at = ? WHERE"
                     " context_key = ? AND magnet_hash = ?",
                     (time.time() - days * DAY, key, ih))
        conn.commit()


def test_cooldown_escalates_with_each_failure():
    assert failure_cooldown_seconds(1) == 7 * DAY
    assert failure_cooldown_seconds(2) == 30 * DAY
    assert failure_cooldown_seconds(3) == 90 * DAY
    assert failure_cooldown_seconds(9) == 90 * DAY


def test_a_copy_that_got_far_keeps_the_short_cooldown():
    assert failure_cooldown_seconds(4, last_progress=0.93) == 7 * DAY
    assert failure_cooldown_seconds(4, last_progress=0.0) == 90 * DAY


def test_record_counts_repeat_failures():
    key = "req:1"
    downloads_store.record_failed_grab(key, HASH_A, progress=0.0)
    downloads_store.record_failed_grab(key, HASH_A.upper(), progress=0.1)
    info = downloads_store.failed_grab_info(key)
    at, count, progress = info[HASH_A]
    assert count == 2 and progress == pytest.approx(0.1)
    assert downloads_store.failed_grab_times(key) == {HASH_A: at}


def test_second_failure_stays_out_of_the_pool_past_a_week():
    key = "ep:5:6:12"
    downloads_store.record_failed_grab(key, HASH_A, progress=0.0)
    downloads_store.record_failed_grab(key, HASH_B, progress=0.0)
    downloads_store.record_failed_grab(key, HASH_B, progress=0.0)
    _age(key, HASH_A, days=10)
    _age(key, HASH_B, days=10)
    recent = DownloadManager._recent_failure_hashes(key)
    # Failed once, ten days ago: eligible again. Failed twice: still out.
    assert HASH_A not in recent
    assert HASH_B in recent


def test_backfill_seeds_counts_from_download_history():
    """Upgrading a database that predates fail_count must not reset every
    repeat offender to a first failure."""
    key = "ep:7:1:1"
    magnet = f"magnet:?xt=urn:btih:{HASH_A}&dn=x"
    for _ in range(3):
        did = downloads_store.create_download(
            title="Dead.Show.S01E01", magnet=magnet, source="tpb",
            media_type="tv", request_id=None, staging_dir="x",
            planned_dest=None, planned_name=None, route_reason=None,
            auto_rename=False, auto_move=False, failure_key=key)
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET status = 'error' WHERE id = ?",
                         (did,))
            conn.commit()
    # Recreate failed_grabs in its pre-upgrade shape.
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS failed_grabs")
        conn.execute("CREATE TABLE failed_grabs (context_key TEXT NOT NULL,"
                     " magnet_hash TEXT NOT NULL, failed_at REAL NOT NULL,"
                     " PRIMARY KEY (context_key, magnet_hash))")
        conn.commit()
    info = downloads_store.failed_grab_info(key)
    assert info[HASH_A][1] == 3


def test_module_constant_still_exported():
    # Older callers and tests read the base window by name.
    assert download_manager.FAILED_GRAB_RETRY_AFTER_S == 7 * DAY


def test_app_side_errors_are_not_held_against_the_release():
    """The webtorrent port bug ("permission denied") and Real-Debrid outages
    say nothing about the release; only release failures count."""
    key = "ep:9:1:2"
    did = downloads_store.create_download(
        title="Show.S01E02", magnet=f"magnet:?xt=urn:btih:{HASH_A}",
        source="tpb", media_type="tv", request_id=None, staging_dir="x",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False, failure_key=key)
    downloads_store.set_status(did, "error", error="permission denied")
    downloads_store.set_status(did, "error",
                               error="Real-Debrid failed: HTTP 503")
    assert downloads_store.failed_grab_info(key) == {}
    downloads_store.set_status(did, "error",
                               error="stalled: no data for 900s")
    assert downloads_store.failed_grab_info(key)[HASH_A][1] == 1


def test_backfill_keeps_progress_and_skips_app_faults():
    key = "ep:8:1:1"
    magnet = f"magnet:?xt=urn:btih:{HASH_B}&dn=x"
    for error, progress in (("permission denied", 0.0),
                            ("stalled: no data for 900s", 0.1),
                            ("stalled: no data for 900s", 0.93)):
        did = downloads_store.create_download(
            title="Show.S01E01", magnet=magnet, source="tpb", media_type="tv",
            request_id=None, staging_dir="x", planned_dest=None,
            planned_name=None, route_reason=None, auto_rename=False,
            auto_move=False, failure_key=key)
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET status = 'error', error = ?,"
                         " progress = ? WHERE id = ?", (error, progress, did))
            conn.commit()
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS failed_grabs")
        conn.execute("CREATE TABLE failed_grabs (context_key TEXT NOT NULL,"
                     " magnet_hash TEXT NOT NULL, failed_at REAL NOT NULL,"
                     " PRIMARY KEY (context_key, magnet_hash))")
        conn.commit()
    _at, count, progress = downloads_store.failed_grab_info(key)[HASH_B]
    assert count == 2                      # the port-bug failure is not counted
    assert progress == pytest.approx(0.93)  # so it keeps the 7-day cooldown
    assert failure_cooldown_seconds(count, progress) == 7 * DAY


@pytest.mark.parametrize("error", [
    "Node.js 20+ is required for downloads but npm was not found on PATH.",
    "npm install timed out after 300s in C:/x — run it by hand and try again.",
    "the torrent runner script is missing and no bundled copy was found",
    "qBittorrent state: error",
])
def test_more_app_side_failures_are_not_the_releases_fault(error):
    assert downloads_store.is_release_fault(error) is False


def test_release_failures_still_count():
    for error in ("stalled: no data for 900s",
                  "timed out: exceeded maximum runtime of 28800s",
                  "torrent metadata contains no selectable video file",
                  "stalled: Real-Debrid could not fetch it (dead)"):
        assert downloads_store.is_release_fault(error) is True
