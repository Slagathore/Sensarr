# =============================================================================
# Task E (Phase 6) — grab-queue store layer + selection retention.
#
# Store-level queries feeding every grab-queue row type; the deferral columns
# (reason, next_attempt_at, candidate stats, last selection_run_id); the
# defer / grab-now / reopen-expired lifecycle; and the RESOLVED DECISION 5
# retention contract: the chosen receipt + reason-code histogram survive
# forever, per-loser detail rows prune after 90 days, keep_details exempts,
# and export JSON captures everything before the prune.
#
# The Tk subtab itself (grab_queue_tab.py) is manual-verify per the house
# rule; everything below the Tk layer is covered here.
# =============================================================================

import datetime as dt
import hashlib
import json
from datetime import date, timedelta

import pytest

import db
import download_manager
import downloads_store
import grab_queue
import queue_store
import shows_store
import torrent_select
from download_manager import DownloadManager
from torrent_search import CollectedPool, TorrentResult

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
def _fresh():
    _clean()
    yield
    _clean()


def _hash(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Deferral store — new columns + lifecycle
# ---------------------------------------------------------------------------

def test_deferral_carries_stats_and_run_id():
    downloads_store.set_grab_deferral(
        "req:7", wait_hours=24, reason="last viable candidate blocked",
        candidate_stats={"blocklisted": 2, "cam_or_trash": 1},
        selection_run_id=42)
    d = downloads_store.get_grab_deferral("req:7")
    assert d["reason"] == "last viable candidate blocked"
    assert d["candidate_stats"] == {"blocklisted": 2, "cam_or_trash": 1}
    assert d["selection_run_id"] == 42
    assert d["next_attempt_at"] is not None
    assert [x["key"] for x in downloads_store.list_grab_deferrals()] == ["req:7"]


def test_check_grab_deferral_refreshes_evidence_without_resetting_clock():
    assert downloads_store.check_grab_deferral(
        "req:9", reason="first pass") is False
    first = downloads_store.get_grab_deferral("req:9")
    # Second pass supplies fresher stats; the clock (first_seen) must hold.
    assert downloads_store.check_grab_deferral(
        "req:9", candidate_stats={"oversize": 3}, selection_run_id=5) is False
    second = downloads_store.get_grab_deferral("req:9")
    assert second["first_seen"] == first["first_seen"]
    assert second["candidate_stats"] == {"oversize": 3}
    assert second["selection_run_id"] == 5


def test_deferral_expiry_logic():
    downloads_store.set_grab_deferral("req:1", wait_hours=24)
    assert downloads_store.deferral_expired(
        downloads_store.get_grab_deferral("req:1")) is False
    downloads_store.set_grab_deferral("req:2", wait_hours=-0.01)  # already past
    assert downloads_store.deferral_expired(
        downloads_store.get_grab_deferral("req:2")) is True
    assert downloads_store.deferral_expired(None) is True


def test_defer_and_grab_now_lifecycle():
    req = queue_store.add_request(
        "some movie", "cole", media_type="movie", resolved_title="Some Movie",
        external_id="99", identity_source="tmdb")
    grab_queue.defer_request(req.request_id, hours=24, reason="not tonight")

    row = queue_store.get_request(req.request_id)
    assert row.status == queue_store.STATUS_DEFERRED
    d = downloads_store.get_grab_deferral(f"req:{req.request_id}")
    assert d["reason"] == "not tonight" and d["next_attempt_at"]

    grab_queue.grab_now_request(req.request_id)
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN
    assert downloads_store.get_grab_deferral(f"req:{req.request_id}") is None


def test_reopen_expired_deferrals():
    fresh = queue_store.add_request(
        "fresh", "cole", media_type="movie", resolved_title="Fresh",
        external_id="1", identity_source="tmdb")
    stale = queue_store.add_request(
        "stale", "cole", media_type="movie", resolved_title="Stale",
        external_id="2", identity_source="tmdb")
    grab_queue.defer_request(fresh.request_id, hours=24)
    grab_queue.defer_request(stale.request_id, hours=-0.01)

    reopened = grab_queue.reopen_expired_deferrals()

    assert reopened == [stale.request_id]
    assert queue_store.get_request(stale.request_id).status == queue_store.STATUS_OPEN
    assert queue_store.get_request(fresh.request_id).status == queue_store.STATUS_DEFERRED


def test_auto_grab_pass_reopens_expired_deferrals(monkeypatch):
    req = queue_store.add_request(
        "expired movie", "cole", media_type="movie",
        resolved_title="Expired Movie", external_id="3",
        identity_source="tmdb")
    grab_queue.defer_request(req.request_id, hours=-0.01)

    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(
        download_manager, "search_collect",
        lambda *a, **k: CollectedPool(results=(), pool_stats={}))
    monkeypatch.setattr(download_manager, "_request_movie_minutes",
                        lambda *a, **k: None)

    dm.auto_grab_open_requests()

    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN


def test_blocked_pass_defers_with_stats_and_run_id(monkeypatch):
    req = queue_store.add_request(
        "blocked movie", "cole", media_type="movie",
        resolved_title="Blocked Movie", external_id="500",
        identity_source="tmdb", aliases=["Blocked Movie"])
    ih = _hash("blocked-release")
    downloads_store.add_blocklist_entry(
        subject_type="request_identity", subject_key="tmdb:500",
        reason_code=downloads_store.BLOCK_REASON_USER_WRONG_PICK, infohash=ih)
    res = TorrentResult(
        title="Blocked.Movie.2020.1080p.BluRay.x264-GRP",
        magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=1400 * MB,
        seeders=40, source="tpb", media_type="movie")
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(
        download_manager, "search_collect",
        lambda *a, **k: CollectedPool(results=(res,),
                                      pool_stats={"per_source": {"tpb": 1}}))
    monkeypatch.setattr(download_manager, "_request_movie_minutes",
                        lambda *a, **k: None)

    started = dm.auto_grab_open_requests()

    assert started == []
    d = downloads_store.get_grab_deferral(f"req:{req.request_id}")
    assert d is not None
    assert d["reason"] == "last viable candidate blocked"
    assert d["candidate_stats"] == {"blocklisted": 1}
    assert d["selection_run_id"] is not None
    run = downloads_store.get_selection_run(d["selection_run_id"])
    assert run is not None and run.request_id == req.request_id


# ---------------------------------------------------------------------------
# Retention (RESOLVED DECISION 5)
# ---------------------------------------------------------------------------

def _decision(*, created_at, chosen_seed="winner", losers=3):
    """A SelectionDecision with one chosen candidate + N reason-coded losers."""
    chosen_ih = _hash(chosen_seed)
    verdicts = [torrent_select.GateVerdict(
        infohash=chosen_ih, title="Winner.2020.1080p.BluRay.x264",
        passed=True, reason_code="ok")]
    scores = [torrent_select.ScoreBreakdown(
        infohash=chosen_ih, title="Winner.2020.1080p.BluRay.x264",
        total=100.0, components={"rtn_quality": 100.0}, seeders=10,
        size_bytes=1400 * MB, size_distance=0.0,
        quality_label="bluray-1080p")]
    codes = ["cam_or_trash", "oversize", "title_mismatch"]
    for i in range(losers):
        verdicts.append(torrent_select.GateVerdict(
            infohash=_hash(f"loser{i}"), title=f"Loser.{i}.CAM",
            passed=False, reason_code=codes[i % len(codes)],
            detail="rejected"))
    return torrent_select.SelectionDecision(
        chosen_infohash=chosen_ih,
        chosen_title="Winner.2020.1080p.BluRay.x264",
        mode="automatic-single", profile=torrent_select.PROFILE,
        rtn_version="1.11.1", verdicts=tuple(verdicts), scores=tuple(scores),
        pool_stats={"candidates": losers + 1}, created_at=created_at)


_OLD = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).isoformat()
_NOW = dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc).isoformat()   # +193d


def test_prune_keeps_receipt_and_histogram_deletes_losers():
    run_id = downloads_store.record_selection_run(_decision(created_at=_OLD))
    assert len(downloads_store.list_candidate_decisions(run_id)) == 4

    summary = downloads_store.prune_selection_run_details(days=90, now=_NOW)

    assert summary["runs_pruned"] == 1 and summary["rows_deleted"] == 3
    remaining = downloads_store.list_candidate_decisions(run_id)
    # The chosen candidate's own row is never pruned.
    assert len(remaining) == 1
    assert remaining[0].infohash == _hash("winner")
    assert remaining[0].passed is True
    # The receipt (selection_runs row) is untouched and the histogram intact.
    run = downloads_store.get_selection_run(run_id)
    assert run is not None
    assert run.chosen_title == "Winner.2020.1080p.BluRay.x264"
    hist = json.loads(run.verdict_histogram_json)
    assert hist == {"ok": 1, "cam_or_trash": 1, "oversize": 1,
                    "title_mismatch": 1}


def test_prune_respects_keep_details_and_age():
    kept = downloads_store.record_selection_run(_decision(created_at=_OLD))
    downloads_store.set_keep_details(kept, True)
    fresh = downloads_store.record_selection_run(
        _decision(created_at=_NOW, chosen_seed="fresh-winner"))

    downloads_store.prune_selection_run_details(days=90, now=_NOW)

    # keep_details exempts the old run; the fresh run is inside the window.
    assert len(downloads_store.list_candidate_decisions(kept)) == 4
    assert len(downloads_store.list_candidate_decisions(fresh)) == 4


def test_prune_backfills_histogram_for_pre_histogram_runs():
    run_id = downloads_store.record_selection_run(_decision(created_at=_OLD))
    with db.connect() as conn:   # simulate a run written before histograms
        conn.execute("UPDATE selection_runs SET verdict_histogram_json = NULL"
                     " WHERE id = ?", (run_id,))
        conn.commit()

    downloads_store.prune_selection_run_details(days=90, now=_NOW)

    run = downloads_store.get_selection_run(run_id)
    hist = json.loads(run.verdict_histogram_json)
    assert hist == {"ok": 1, "cam_or_trash": 1, "oversize": 1,
                    "title_mismatch": 1}


def test_prune_run_with_no_chosen_deletes_all_details_keeps_run():
    decision = torrent_select.SelectionDecision(
        chosen_infohash=None, chosen_title=None, mode="automatic-single",
        profile=torrent_select.PROFILE, rtn_version="1.11.1",
        verdicts=(torrent_select.GateVerdict(
            infohash=_hash("x"), title="X.CAM", passed=False,
            reason_code="cam_or_trash"),),
        scores=(), pool_stats={}, created_at=_OLD,
        reason="all candidates rejected")
    run_id = downloads_store.record_selection_run(decision)

    downloads_store.prune_selection_run_details(days=90, now=_NOW)

    assert downloads_store.list_candidate_decisions(run_id) == []
    run = downloads_store.get_selection_run(run_id)
    assert run is not None                      # the receipt survives
    assert json.loads(run.verdict_histogram_json) == {"cam_or_trash": 1}


def test_export_selection_run_json_captures_everything():
    run_id = downloads_store.record_selection_run(
        _decision(created_at=_OLD), request_id=12)
    text = downloads_store.export_selection_run_json(run_id)
    data = json.loads(text)
    assert data["selection_run_id"] == run_id
    assert data["request_id"] == 12
    assert data["chosen_title"] == "Winner.2020.1080p.BluRay.x264"
    assert len(data["candidates"]) == 4
    assert data["verdict_histogram"]["cam_or_trash"] == 1
    winner = next(c for c in data["candidates"] if c["passed"])
    assert winner["score_components"] == {"rtn_quality": 100.0}
    assert downloads_store.export_selection_run_json(999999) is None


# ---------------------------------------------------------------------------
# Grab-queue row queries — every row type
# ---------------------------------------------------------------------------

def test_open_request_row_with_deferral_reason():
    req = queue_store.add_request(
        "movie a", "cole", media_type="movie", resolved_title="Movie A",
        external_id="10", identity_source="tmdb")
    downloads_store.check_grab_deferral(
        f"req:{req.request_id}", reason="oversize: every result is >120%",
        candidate_stats={"oversize": 4}, selection_run_id=None)

    rows = grab_queue.request_rows()
    row = next(r for r in rows if r.request_id == req.request_id)
    assert row.row_type == grab_queue.ROW_REQUEST
    assert row.state == "open"
    assert row.subject_key == "tmdb:10"
    assert "oversize" in row.reason
    assert row.next_attempt_at is not None
    assert row.detail["candidate_stats"] == {"oversize": 4}


def test_needs_identity_row_and_season_subject_key():
    ni = queue_store.add_request("mystery show", "cole", media_type="tv",
                                 status=queue_store.STATUS_NEEDS_IDENTITY)
    tv = queue_store.add_request(
        "known show", "cole", media_type="tv", resolved_title="Known Show",
        external_id="55", identity_source="tvdb", season=2)

    rows = grab_queue.request_rows()
    ni_row = next(r for r in rows if r.request_id == ni.request_id)
    assert ni_row.row_type == grab_queue.ROW_NEEDS_IDENTITY
    assert "resolve" in ni_row.reason
    tv_row = next(r for r in rows if r.request_id == tv.request_id)
    assert tv_row.subject_key == "tvdb:55:s2"
    assert tv_row.display_title == "Known Show S02"


def test_needs_attention_row_carries_verification_reason():
    req = queue_store.add_request(
        "wrong grab", "cole", media_type="movie", resolved_title="Wrong Grab",
        external_id="77", identity_source="tmdb")
    did = downloads_store.create_download(
        title="Wrong.Grab.2.2020.1080p", magnet=f"magnet:?xt=urn:btih:{_hash('wg')}",
        source="tpb", media_type="movie", request_id=req.request_id,
        staging_dir="C:/staging", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    downloads_store.set_verification(
        did, "quarantined", reason="sequel_mismatch: wanted 'Wrong Grab'")
    queue_store.set_status(req.request_id, queue_store.STATUS_NEEDS_ATTENTION)

    rows = grab_queue.request_rows()
    row = next(r for r in rows if r.request_id == req.request_id)
    assert row.row_type == grab_queue.ROW_NEEDS_ATTENTION
    assert "sequel_mismatch" in row.reason


class _Ep:
    def __init__(self, season, episode, air_date):
        self.season, self.episode = season, episode
        self.title = f"E{episode}"
        self.air_date = air_date


def test_keep_at_100_row_lists_missing_episodes():
    show_id = shows_store.upsert_show(title="Keeper", media_type="tv",
                                      source="tvdb", external_id="k1")
    shows_store.set_show_auto_grab(show_id, True)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    shows_store.replace_episodes(show_id, [_Ep(1, 1, yesterday),
                                           _Ep(1, 2, yesterday)])
    shows_store.set_episode_file(show_id, 1, 1, "C:/tv/keeper-e1.mkv")

    rows = grab_queue.keep_at_100_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.row_type == grab_queue.ROW_KEEP_AT_100
    assert row.show_id == show_id
    assert row.detail["missing_count"] == 1
    assert row.detail["episodes"] == ["S01E02"]


def test_follow_new_row_upcoming_and_aired_unfetched():
    show_id = shows_store.upsert_show(title="Follower", media_type="tv",
                                      source="tvdb", external_id="f1")
    shows_store.set_show_follow_new(show_id, True)   # follow_since = today
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    shows_store.replace_episodes(show_id, [_Ep(3, 1, today)])  # aired, no file
    shows_store.set_show_airing(show_id, next_air_date=tomorrow,
                                next_season=3, next_episode=2)

    rows = grab_queue.follow_new_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.row_type == grab_queue.ROW_FOLLOW_NEW
    assert row.detail["aired_unfetched"] == ["S03E01"]
    assert "S03E02 airs" in row.reason
    assert row.next_attempt_at == tomorrow


def test_active_download_and_needs_placement_and_blocklist_rows():
    did = downloads_store.create_download(
        title="Active.Show.S01E01.720p", magnet=f"magnet:?xt=urn:btih:{_hash('act')}",
        source="tpb", media_type="tv", request_id=None,
        staging_dir="C:/staging", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False,
        quality_label="webrip-720p")
    downloads_store.set_progress(did, 0.42)

    stuck = downloads_store.create_download(
        title="Homeless.Show.S01E01", magnet=f"magnet:?xt=urn:btih:{_hash('hm')}",
        source="tpb", media_type="tv", request_id=None,
        staging_dir="C:/staging", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    downloads_store.record_needs_placement(
        stuck, show_id=None, season=1, suggested_dir="D:/TV/Homeless/Season 01",
        reason="no tv root configured")

    downloads_store.add_blocklist_entry(
        subject_type="request_identity", subject_key="tmdb:153518",
        reason_code="identity_mismatch", parsed_title="Angry.Birds.2",
        reason_detail="sequel for the movie-1 subject")

    rows = grab_queue.list_grab_queue_rows()
    by_type = {}
    for r in rows:
        by_type.setdefault(r.row_type, []).append(r)

    active = next(r for r in by_type[grab_queue.ROW_ACTIVE_DOWNLOAD]
                  if r.download_id == did)
    assert active.state == "downloading" and active.reason == "42%"
    assert active.detail["quality_label"] == "webrip-720p"

    placement = next(r for r in by_type[grab_queue.ROW_NEEDS_PLACEMENT]
                     if r.download_id == stuck)
    assert placement.detail["suggested_dir"] == "D:/TV/Homeless/Season 01"

    block = by_type[grab_queue.ROW_BLOCKLIST][0]
    assert block.subject_key == "tmdb:153518"
    assert block.state == "identity_mismatch"
    assert block.detail["blocklist_id"] > 0


# ---------------------------------------------------------------------------
# Phase 6 migrations — idempotent, fresh DB and upgraded live-DB copy
# ---------------------------------------------------------------------------

_PHASE6_COLUMNS = {
    "downloads": {"quality_label"},
    "selection_runs": {"keep_details", "verdict_histogram_json"},
    "grab_deferrals": {"candidate_stats_json", "selection_run_id"},
    "tracked_shows": {"size_mode", "size_mb_min_override"},
    "media_quality": {"identity_key", "quality_label", "source", "file_path",
                      "updated_at"},
    "media_quality_history": {"identity_key", "old_label", "new_label",
                              "cause", "at"},
}


def _assert_phase6_schema(path):
    import sqlite3
    con = sqlite3.connect(str(path))
    try:
        for table, expected in _PHASE6_COLUMNS.items():
            cols = {r[1] for r in con.execute(
                f"PRAGMA table_info({table})").fetchall()}
            missing = expected - cols
            assert not missing, f"{table} missing columns: {missing}"
    finally:
        con.close()


def _init_all():
    import media_quality
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()
    media_quality.initialize_media_quality_db()


def test_phase6_migrations_fresh_db_run_twice(monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "APP_DB_PATH", str(tmp_path / "fresh.db"))
    _init_all()
    _init_all()   # run twice — the ALTERs must be no-ops the second time
    _assert_phase6_schema(tmp_path / "fresh.db")


def test_phase6_migrations_upgraded_live_copy(monkeypatch, tmp_path):
    import shutil
    from pathlib import Path
    import config
    backup = Path("C:/Users/Cole/CodeStuff/_backups/PlexResetButton-20260713/"
                  "plex_reset_button.upgrade-test.db")
    if not backup.exists():
        pytest.skip(f"upgrade-test backup not present at {backup}")
    dbfile = tmp_path / "upgrade.db"
    shutil.copy(backup, dbfile)
    monkeypatch.setattr(config, "APP_DB_PATH", str(dbfile))
    _init_all()
    _init_all()
    _assert_phase6_schema(dbfile)
    # The upgraded rows still read back through the widened dataclasses.
    assert downloads_store.list_downloads(limit=5) is not None
    assert shows_store.list_shows() is not None


def test_decision_detail_renders_run_and_histogram():
    run_id = downloads_store.record_selection_run(_decision(created_at=_NOW))
    detail = grab_queue.decision_detail(run_id)
    assert detail is not None
    assert detail["run"].selection_run_id == run_id
    assert detail["verdict_histogram"]["ok"] == 1
    assert len(detail["candidates"]) == 4
    assert detail["details_pruned"] is False
    assert grab_queue.decision_detail(999999) is None
