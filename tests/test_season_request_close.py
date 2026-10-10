# =============================================================================
# Season requests close once the season is on disk.
#
# Dexter: Resurrection S1 (request 207) and Bloodhounds S1 (221) sat in
# needs_attention through 2026-10-09 with every episode already in the library:
# the reconcile only ever closed movies and whole-show requests.
# =============================================================================

import datetime as dt

import pytest

import db
import maintenance
import queue_store
import shows_store


@pytest.fixture(autouse=True)
def _fresh():
    queue_store.initialize_queue_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("requests", "episodes", "tracked_shows", "show_folders",
                      "season_targets"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    yield


def _ep(season, episode, air_date):
    return type("E", (), {"season": season, "episode": episode,
                          "title": f"E{episode}", "air_date": air_date})()


def _show_with_season(have: list[int], *, total=4, future=0):
    show_id = shows_store.upsert_show(
        title="Dexter: Resurrection", media_type="tv", source="tvdb",
        external_id="452595", status="Continuing")
    past = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    later = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    eps = [_ep(1, n, past) for n in range(1, total + 1)]
    eps += [_ep(1, total + n, later) for n in range(1, future + 1)]
    shows_store.replace_episodes(show_id, eps)
    for n in have:
        shows_store.set_episode_file(show_id, 1, n, f"I:/tv/Dexter/S01E{n:02d}.mkv")
    return show_id


def _season_request(status=queue_store.STATUS_NEEDS_ATTENTION, season=1):
    req = queue_store.add_request(
        "Dexter Resurrection season 1", "cole", media_type="tv",
        resolved_title="Dexter: Resurrection", identity_source="tvdb",
        external_id="452595", season=season)
    queue_store.set_status(req.request_id, status)
    return req.request_id


def test_complete_season_closes_a_needs_attention_request():
    _show_with_season(have=[1, 2, 3, 4], future=2)
    rid = _season_request()
    assert maintenance.season_request_complete(queue_store.get_request(rid))
    assert maintenance.close_complete_season_requests() == 1
    assert queue_store.get_request(rid).status == queue_store.STATUS_FULFILLED


def test_a_missing_aired_episode_keeps_it_open():
    _show_with_season(have=[1, 2, 4])
    rid = _season_request(status=queue_store.STATUS_OPEN)
    assert not maintenance.season_request_complete(queue_store.get_request(rid))
    assert maintenance.close_complete_season_requests() == 0
    assert queue_store.get_request(rid).status == queue_store.STATUS_OPEN


def test_unaired_season_never_counts_as_complete():
    _show_with_season(have=[1, 2, 3, 4])
    rid = _season_request(status=queue_store.STATUS_DEFERRED, season=2)
    assert not maintenance.season_request_complete(queue_store.get_request(rid))


def test_grabbing_requests_are_left_to_finish():
    _show_with_season(have=[1, 2, 3, 4])
    rid = _season_request(status=queue_store.STATUS_GRABBING)
    assert maintenance.close_complete_season_requests() == 0
    assert queue_store.get_request(rid).status == queue_store.STATUS_GRABBING


def test_watcher_reconcile_closes_it_too(monkeypatch):
    _show_with_season(have=[1, 2, 3, 4])
    rid = _season_request()
    monkeypatch.setattr(maintenance, "request_present_in_library",
                        lambda req: False)
    out = maintenance.reconcile_requests_with_library()
    assert out["closed"] == 1
    assert queue_store.get_request(rid).status == queue_store.STATUS_FULFILLED
