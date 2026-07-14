# =============================================================================
# Status-page counts: every user-actionable state is surfaced (Cole's ask —
# needs identity, needs attention, needs placement, plus what's waiting).
# Store-level so it is testable without Tk.
# =============================================================================

import db
import downloads_store
import grab_queue
import queue_store


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for t in ("requests", "downloads", "download_history",
                  "needs_placement", "selection_runs", "candidate_decisions"):
            try:
                conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        conn.commit()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh():
    _clean()
    yield
    _clean()


def _req(title, status, **kw):
    r = queue_store.add_request(title, "cole", media_type=kw.pop("mt", "movie"),
                                resolved_title=title, **kw)
    if status != queue_store.STATUS_OPEN:
        queue_store.set_status(r.request_id, status)
    return r


def test_counts_reflect_every_actionable_state():
    _req("A", queue_store.STATUS_OPEN, external_id="1", identity_source="tmdb")
    _req("B", queue_store.STATUS_OPEN, external_id="2", identity_source="tmdb")
    _req("C", queue_store.STATUS_NEEDS_IDENTITY, mt="unknown")
    _req("D", queue_store.STATUS_NEEDS_ATTENTION, external_id="4",
         identity_source="tmdb")
    _req("E", queue_store.STATUS_DEFERRED, external_id="5", identity_source="tmdb")

    c = grab_queue.status_counts()
    assert c.open_requests == 2
    assert c.needs_identity == 1
    assert c.needs_attention == 1
    assert c.deferred == 1
    # actionable = the ones that genuinely need the USER (not app-waiting).
    assert c.actionable == 2   # needs_identity + needs_attention (+placement=0)


def test_needs_placement_counted():
    did = downloads_store.create_download(
        title="X", magnet="magnet:?xt=urn:btih:" + "a" * 40, source="tpb",
        media_type="tv", request_id=None, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=True, auto_move=True, show_id=7, season=2)
    downloads_store.record_needs_placement(
        did, show_id=7, season=2, suggested_dir="/tv/Show/Season 02",
        reason="no confident route")
    c = grab_queue.status_counts()
    assert c.needs_placement == 1
    assert c.actionable >= 1


def test_summary_line_flags_needs_you():
    _req("C", queue_store.STATUS_NEEDS_IDENTITY, mt="unknown")
    line = grab_queue.status_summary_line()
    assert "Needs you" in line and "identity" in line


def test_summary_line_all_clear_when_nothing_pending():
    assert grab_queue.status_summary_line() == ""   # UI renders "All clear"


def test_active_downloads_counted():
    for i, st in enumerate(("downloading", "queued", "queued")):
        did = downloads_store.create_download(
            title=f"D{i}", magnet=f"magnet:?xt=urn:btih:{chr(97+i)*40}",
            source="tpb", media_type="movie", request_id=None,
            staging_dir="/tmp", planned_dest=None, planned_name=None,
            route_reason=None, auto_rename=False, auto_move=False)
        downloads_store.set_status(did, st)
    c = grab_queue.status_counts()
    assert c.downloading == 1 and c.queued == 2
