# =============================================================================
# Oversize cooldown must RELEASE after a day, not re-arm forever.
#
# Chainsmoker Cat S01E09 (2026-09-04..10): every viable release was an 867 MB
# NF rip against a 663 MB (1.2x preferred) ceiling. The gate is meant to hold
# such an episode for 24h and then take the oversized release anyway (that is
# how S01E08 was grabbed on 08-22). Since 1.6.0 the pre-search skip in
# _deferral_active DELETED the expired hold, so the gate never saw an aged
# hold, wrote a fresh 24h one, and the episode was deferred every single day.
# =============================================================================

import datetime as dt
import hashlib

import pytest

import config
import db
import download_manager
import downloads_store
import queue_store
import shows_store
from download_manager import DownloadManager
from torrent_search import TorrentResult

MB = 1024 * 1024


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "blocklist", "download_files",
                      "request_downloads", "needs_placement", "episodes",
                      "tracked_shows", "show_folders", "season_targets"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    _clean()
    # The live anime defaults: 22.1 MB/min preferred (663 MB ceiling at 25 min
    # with the 1.2x tolerance), 41 MB/min hard max (1025 MB). An 867 MB
    # release clears the hard max and trips the oversize cooldown.
    monkeypatch.setattr(config, "SIZE_PREF_MB_PER_MIN_ANIME", 22.1)
    monkeypatch.setattr(config, "SIZE_MAX_MB_PER_MIN_ANIME", 41.0)
    yield
    _clean()


def _seed_show():
    show_id = shows_store.upsert_show(
        title="Chainsmoker Cat", media_type="anime",
        source="anidb", external_id="19873", status="Continuing")
    shows_store.set_show_runtime(show_id, 25.0)
    shows_store.replace_episodes(show_id, [
        type("E", (), {"season": 1, "episode": 9, "title": "E9",
                       "air_date": "2026-09-04"})()
    ])
    return shows_store.get_show(show_id)


def _release():
    ih = hashlib.sha1(b"toonshub-e9").hexdigest()
    return TorrentResult(
        title="[ToonsHub] Chainsmoker Cat S01E09 1080p NF WEB-DL AAC2.0 "
              "H.264 (Yani Neko, Multi-Subs, Uncensored)",
        magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=867 * MB,
        seeders=1662, source="nyaa", media_type="anime")


def _dm(monkeypatch, grabbed):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)

    class _Pool:
        results = (_release(),)
        pool_stats = {"per_source": {"nyaa": 1}}
    monkeypatch.setattr(download_manager, "search_collect",
                        lambda *a, **k: _Pool())
    monkeypatch.setattr(dm, "grab", lambda *a, **k: grabbed.append(k) or 999)
    return dm


def _age_hold(key: str, hours: float) -> None:
    """Pretend the hold was written `hours` ago (its clock has run out)."""
    now = dt.datetime.now(dt.timezone.utc)
    first = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    next_at = (now - dt.timedelta(hours=hours - 24)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE grab_deferrals SET first_seen = ?, "
                     "next_attempt_at = ? WHERE key = ?", (first, next_at, key))
        conn.commit()


def test_oversize_hold_releases_after_a_day(monkeypatch):
    show = _seed_show()
    ep = shows_store.missing_episodes(show.show_id)[0]
    key = f"ep:{show.show_id}:1:9"
    grabbed: list = []
    dm = _dm(monkeypatch, grabbed)

    # Day 1: only an oversized release exists -> hold for a day, no grab.
    assert dm._grab_one_episode(show, ep) == []
    assert not grabbed
    hold = downloads_store.get_grab_deferral(key)
    assert hold is not None
    assert hold["reason"] == download_manager.OVERSIZE_DEFER_REASON

    # Day 2 (the hold has expired): the same oversized release must be taken.
    _age_hold(key, hours=25)
    assert dm._grab_one_episode(show, ep) == [999], (
        "the oversize cooldown re-armed instead of releasing")
    assert len(grabbed) == 1
    assert downloads_store.get_grab_deferral(key) is None


def test_oversize_hold_still_skips_the_search_while_active(monkeypatch):
    show = _seed_show()
    ep = shows_store.missing_episodes(show.show_id)[0]
    grabbed: list = []
    dm = _dm(monkeypatch, grabbed)
    assert dm._grab_one_episode(show, ep) == []

    searches: list = []
    monkeypatch.setattr(download_manager, "search_collect",
                        lambda *a, **k: searches.append(a) or None)
    # Same day, hold still running: no provider search, no grab.
    assert dm._grab_one_episode(show, ep) == []
    assert searches == [] and not grabbed


def test_expired_non_oversize_hold_is_still_cleared_before_search():
    """The pre-search sweep keeps clearing every other expired hold, so a
    'no acceptable release' key gets a fresh bounded hold on its next pass."""
    key = "ep:424242:1:1"
    downloads_store.check_grab_deferral(
        key, wait_hours=24.0, reason="no acceptable episode release found")
    _age_hold(key, hours=25)
    assert DownloadManager._deferral_active(key) is False
    assert downloads_store.get_grab_deferral(key) is None


def test_request_oversize_hold_survives_reopen_and_then_releases():
    """The movie-request path had the same loop one layer up: at the start of
    every auto-grab pass reopen_expired_deferrals() reopened the request AND
    deleted its req:N hold, so _oversize_gate saw a blank slate and started a
    fresh day. Requests 195/199/201/203/212 sat in that loop from early
    September to 2026-10-09 ("Only oversized results ... waiting a day" daily,
    never "Oversize deferral expired")."""
    import grab_queue
    req = queue_store.add_request("Sea Fever", "cole", media_type="movie")
    rid = req.request_id
    key = f"req:{rid}"
    queue_store.set_status(rid, queue_store.STATUS_DEFERRED)
    downloads_store.check_grab_deferral(
        key, wait_hours=24.0, reason=downloads_store.OVERSIZE_DEFER_REASON)
    _age_hold(key, hours=25)

    assert grab_queue.reopen_expired_deferrals() == [rid]
    assert queue_store.get_request(rid).status == queue_store.STATUS_OPEN
    hold = downloads_store.get_grab_deferral(key)
    assert hold is not None, "reopen deleted the aged oversize hold"

    big = TorrentResult(
        title="Sea.Fever.2020.1080p.BluRay.x264", magnet="magnet:?xt=urn:btih:"
        + hashlib.sha1(b"sea-fever").hexdigest(), size_bytes=4000 * MB,
        seeders=40, source="tpb", media_type="movie")
    # The day has passed, so the gate lets the oversized copy through and
    # clears the hold itself.
    assert DownloadManager._oversize_gate([big], "movie", key, minutes=95.0)
    assert downloads_store.get_grab_deferral(key) is None


def test_reopen_still_clears_other_expired_request_holds():
    import grab_queue
    req = queue_store.add_request("Whalefall", "cole", media_type="movie")
    rid = req.request_id
    key = f"req:{rid}"
    queue_store.set_status(rid, queue_store.STATUS_DEFERRED)
    downloads_store.check_grab_deferral(
        key, wait_hours=6.0, reason="torrent providers temporarily unavailable")
    _age_hold(key, hours=25)
    assert grab_queue.reopen_expired_deferrals() == [rid]
    assert downloads_store.get_grab_deferral(key) is None


def test_a_different_hold_replaces_an_aged_oversize_hold():
    """Verifier finding: after the oversize day passed, a dead-swarm or
    no-result hold for the same key wrote nothing (the aged row was still
    there), so reopen kept reopening it and the key was searched every pass."""
    key = "req:777"
    downloads_store.check_grab_deferral(
        key, wait_hours=24.0, reason=downloads_store.OVERSIZE_DEFER_REASON)
    _age_hold(key, hours=25)
    ready = downloads_store.check_grab_deferral(
        key, wait_hours=6.0, reason="no seeded release available")
    assert ready is False, "the new hold must start its own clock"
    hold = downloads_store.get_grab_deferral(key)
    assert hold["reason"] == "no seeded release available"
    assert not downloads_store.deferral_expired(hold)


def test_the_oversize_reason_itself_still_reads_the_aged_clock():
    key = "req:778"
    downloads_store.check_grab_deferral(
        key, wait_hours=24.0, reason=downloads_store.OVERSIZE_DEFER_REASON)
    _age_hold(key, hours=25)
    assert downloads_store.check_grab_deferral(
        key, wait_hours=24.0, reason=downloads_store.OVERSIZE_DEFER_REASON) is True
