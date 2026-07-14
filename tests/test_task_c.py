# =============================================================================
# Task C (Phase 4) — verify before move, provenance, scoped blocklist,
# aggregate fulfillment, placed vs verified, reconciliation, quarantine.
#
# These drive the real _post_process with an on-disk staging dir and a
# monkeypatched confident route, so a CONTRADICTORY file never reaches a
# library root — the binding gate.
# =============================================================================

import hashlib
import json

import pytest

import db
import download_manager
import downloads_store
import maintenance
import queue_store
import torrent_routing
from download_manager import DownloadManager

_MB = 1024 ** 2


def _clean():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    try:
        import shows_store
        shows_store.initialize_shows_db()
    except Exception:
        pass
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "blocklist", "download_files",
                      "request_downloads", "episodes", "tracked_shows",
                      "show_folders", "season_targets"):
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


def _write(path, size=2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _want(*, media_type="movie", title=None, year=None, source=None, ext=None,
          season=None, countries=()):
    return {
        "schema": 1, "media_type": media_type, "identity_source": source,
        "external_id": ext, "canonical_title": title, "canonical_year": year,
        "origin_countries": list(countries), "aliases": [],
        "search_alias": title, "season": season, "episode": None,
        "size_pref_mb_min": 0, "size_max_rate": 0, "runtime_minutes": None,
    }


def _make_download(*, want, staging, files, media_type="movie",
                   request_id=None, auto_move=True, seed=None):
    seed = seed or files[0]
    did = downloads_store.create_download(
        title=files[0].rsplit("/", 1)[-1].rsplit(".", 1)[0],
        magnet=f"magnet:?xt=urn:btih:{_hash(seed)}",
        source="tpb", media_type=media_type, request_id=request_id,
        staging_dir=str(staging), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=auto_move,
        want_json=json.dumps(want))
    downloads_store.set_files(did, files)
    downloads_store.set_status(did, "downloaded", completed=True)
    return did


def _dm(monkeypatch, *, dest_dir):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(
        "download_manager.torrent_routing.plan_route",
        lambda *a, **k: torrent_routing.RoutePlan(
            confident=True, dest_dir=str(dest_dir), reason="test-route"))
    return dm


# ---------------------------------------------------------------------------
# THE regression — a sequel payload never reaches the library root
# ---------------------------------------------------------------------------

def test_sequel_payload_quarantined_never_reaches_library(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    movie_root = tmp_path / "Movies"
    f = _write(staging / "The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GRP.mkv")
    req = queue_store.add_request(
        "angry birds", "cole", media_type="movie",
        resolved_title="The Angry Birds Movie", external_id="153518",
        external_url="https://www.themoviedb.org/movie/153518",
        identity_source="tmdb", canonical_year=2016, origin_countries=["US"],
        aliases=["The Angry Birds Movie"])
    want = _want(title="The Angry Birds Movie", year=2016, source="tmdb",
                 ext="153518", countries=["US"])
    did = _make_download(want=want, staging=staging, files=[f.name],
                         request_id=req.request_id)
    dm = _dm(monkeypatch, dest_dir=movie_root)

    out = dm._post_process(did)

    assert "quarantined" in out
    # NOTHING under the library root.
    assert not movie_root.exists() or not list(movie_root.rglob("*.mkv"))
    # The staged bytes are KEPT (quarantine is reversible).
    assert f.exists()
    # A subject-scoped blocklist row exists for the wanted identity.
    entries = downloads_store.blocklist_entries_for_subject("tmdb:153518")
    assert any(e.reason_code == downloads_store.BLOCK_REASON_IDENTITY_MISMATCH
               for e in entries)
    # Download quarantined + archived; request reopened for a different pick.
    row = downloads_store.get_download(did)
    assert row.verification_state == "quarantined"
    assert row.removed_at is not None
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN


def test_good_movie_payload_moves_and_fulfills(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    movie_root = tmp_path / "Movies"
    f = _write(staging / "The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE.mkv")
    req = queue_store.add_request(
        "angry birds", "cole", media_type="movie",
        resolved_title="The Angry Birds Movie", external_id="153518",
        identity_source="tmdb", canonical_year=2016)
    want = _want(title="The Angry Birds Movie", year=2016, source="tmdb",
                 ext="153518")
    did = _make_download(want=want, staging=staging, files=[f.name],
                         request_id=req.request_id)
    dm = _dm(monkeypatch, dest_dir=movie_root)

    out = dm._post_process(did)

    assert "moved to" in out
    assert list(movie_root.rglob("*.mkv"))            # it IS in the library
    row = downloads_store.get_download(did)
    assert row.verification_state == "verified"
    # Aggregate fulfillment: a movie is the whole thing -> fulfilled.
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_FULFILLED
    # Provenance recorded.
    files = downloads_store.list_download_files(did)
    assert files and files[0].verification_state == "verified"
    assert files[0].final_path and movie_root.name in files[0].final_path


# ---------------------------------------------------------------------------
# Role gate — samples/extras/unknowns never move to a library root (M1)
# ---------------------------------------------------------------------------

def test_samples_and_extras_never_reach_library_root(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    movie_root = tmp_path / "Movies"
    primary = _write(staging / "Inception.2010.1080p.BluRay.x264-GRP.mkv",
                     size=5000)
    sub = _write(staging / "Inception.2010.1080p.BluRay.x264-GRP.srt", size=100)
    sample = _write(staging / "sample.mkv", size=500)
    extra = _write(staging / "Extras" / "Behind.The.Scenes.mkv", size=800)
    want = _want(title="Inception", year=2010, source="tmdb", ext="27205")
    did = _make_download(
        want=want, staging=staging,
        files=[primary.name, sub.name, sample.name,
               "Extras/Behind.The.Scenes.mkv"])
    dm = _dm(monkeypatch, dest_dir=movie_root)

    out = dm._post_process(did)

    assert "moved to" in out
    landed = sorted(p.name for p in movie_root.rglob("*") if p.is_file())
    # Only the primary video and its subtitle land under the library root.
    assert landed == ["Inception.2010.1080p.BluRay.x264-GRP.mkv",
                      "Inception.2010.1080p.BluRay.x264-GRP.srt"]
    # Sample + extra stayed in staging.
    assert sample.exists() and extra.exists()
    # Provenance shows the role-based skip.
    rows = {f.source_relative_path: f
            for f in downloads_store.list_download_files(did)}
    assert rows["sample.mkv"].media_role == "sample"
    assert rows["sample.mkv"].verification_state == "skipped"
    assert "never moves to a library root" in (
        rows["sample.mkv"].verification_reason or "")
    assert rows["Behind.The.Scenes.mkv"].media_role == "extra"
    assert rows["Behind.The.Scenes.mkv"].verification_state == "skipped"


def test_no_gating_video_moves_nothing(tmp_path, monkeypatch):
    # N3: a payload with ONLY non-gating files (sample + sub) places nothing.
    staging = tmp_path / "staging"
    movie_root = tmp_path / "Movies"
    _write(staging / "sample.mkv", size=500)
    _write(staging / "Inception.2010.srt", size=100)
    want = _want(title="Inception", year=2010, source="tmdb", ext="27205")
    did = _make_download(want=want, staging=staging,
                         files=["sample.mkv", "Inception.2010.srt"])
    dm = _dm(monkeypatch, dest_dir=movie_root)

    out = dm._post_process(did)

    assert "no primary/episode video" in out
    assert not movie_root.exists() or not list(movie_root.rglob("*"))


# ---------------------------------------------------------------------------
# Partial pack — one file cannot be placed -> partial, not silently complete
# ---------------------------------------------------------------------------

def test_partial_pack_marks_partial_not_moved(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    tv_root = tmp_path / "TV"
    e1 = _write(staging / "Show.S01E01.1080p.WEB.x264-GRP.mkv", size=3000)
    e2 = _write(staging / "Show.S01E02.1080p.WEB.x264-GRP.mkv", size=3000)
    # A DIFFERENT, larger file already sits where E02 would land -> E02 can't move.
    blocker = _write(tv_root / "Show.S01E02.1080p.WEB.x264-GRP.mkv", size=9000)
    want = _want(media_type="tv", title="Show", season=1)
    did = _make_download(want=want, staging=staging,
                         files=[e1.name, e2.name], media_type="tv")
    dm = _dm(monkeypatch, dest_dir=tv_root)

    dm._post_process(did)

    row = downloads_store.get_download(did)
    assert row.verification_state == "partial"   # not "verified"
    # E01 moved; E02 stayed because a different larger file blocks it.
    assert blocker.stat().st_size == 9000        # the blocker was not overwritten


# ---------------------------------------------------------------------------
# Aggregate fulfillment — a first episode never completes a season
# ---------------------------------------------------------------------------

def test_first_episode_does_not_complete_season(monkeypatch):
    import shows_store
    show_id = shows_store.upsert_show(title="Kept Show", media_type="tv",
                                      source="tmdb", external_id="k1")

    class _Ep:
        def __init__(s, e):
            s.season, s.episode, s.title, s.air_date = 1, e, "", "2020-01-01"
    shows_store.replace_episodes(show_id, [_Ep(1), _Ep(2)])
    req = queue_store.add_request(
        "kept show", "cole", media_type="tv", resolved_title="Kept Show",
        external_id="k1", identity_source="tmdb", season=1,
        aliases=["Kept Show"])
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)

    # Episode 1 present -> progress, NOT fulfilled.
    shows_store.set_episode_file(show_id, 1, 1, "C:/tv/Kept Show/S01E01.mkv")
    dm._finalize_fulfillment(req.request_id)
    assert queue_store.get_request(req.request_id).status != queue_store.STATUS_FULFILLED

    # Episode 2 completes the aired set -> fulfilled.
    shows_store.set_episode_file(show_id, 1, 2, "C:/tv/Kept Show/S01E02.mkv")
    dm._finalize_fulfillment(req.request_id)
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_FULFILLED


# ---------------------------------------------------------------------------
# Transient failure never enters the permanent blocklist
# ---------------------------------------------------------------------------

def test_transient_timeout_absent_from_blocklist():
    did = downloads_store.create_download(
        title="Some.Movie.2015.1080p", magnet=f"magnet:?xt=urn:btih:{_hash('t')}",
        source="tpb", media_type="movie", request_id=42, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False)
    downloads_store.set_status(did, "error", error="tracker timeout",
                               completed=True)
    # It went to failed_grabs (transient rotation), NEVER the blocklist.
    assert downloads_store.list_blocklist() == []
    assert downloads_store.failed_grab_times("req:42")


def test_race_loser_not_blocklisted_and_regrabbable(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    did = downloads_store.create_download(
        title="Movie.2020.1080p", magnet=f"magnet:?xt=urn:btih:{_hash('rl')}",
        source="tpb", media_type="movie", request_id=None, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False)
    downloads_store.set_status(did, "downloading")
    dm._cancel_race_loser(did)
    # No blocklist entry -> the same release stays eligible for another identity.
    assert downloads_store.list_blocklist() == []
    hist = [h.action for h in downloads_store.list_history()
            if h.download_id == did]
    assert "race_loser" in hist


# ---------------------------------------------------------------------------
# Reconciliation — poisoned rows detected + reopened, false found cleared
# ---------------------------------------------------------------------------

class _Entry:
    def __init__(self, name):
        self.name = name


def test_reconcile_clears_poisoned_sequel_found_and_blocks(monkeypatch):
    # Requests 55/86 pattern: open + found_in_library=1, only a SEQUEL present.
    req = queue_store.add_request(
        "angry birds", "cole", media_type="movie",
        resolved_title="The Angry Birds Movie", external_id="153518",
        identity_source="tmdb", canonical_year=2016)
    queue_store.update_library_status(req.request_id, found=True)
    monkeypatch.setattr(
        "library_index.search_library",
        lambda *a, **k: [_Entry("The Angry Birds Movie 2 (2019)")])

    summary = maintenance.daily_library_check()

    r = queue_store.get_request(req.request_id)
    assert r.found_in_library is False              # false positive cleared
    assert r.status == queue_store.STATUS_OPEN      # stays grabbable
    assert summary["cleared_false_found"] >= 1
    # The sequel is blocked for movie 1's identity.
    entries = downloads_store.blocklist_entries_for_subject("tmdb:153518")
    assert any("Angry Birds Movie 2" in (e.parsed_title or "") for e in entries)


def test_reconcile_clears_needs_identity_false_found(monkeypatch):
    # Request 85 pattern: needs_identity but carries a stale found flag.
    req = queue_store.add_request("married at first sight", "cole",
                                  media_type="unknown",
                                  status=queue_store.STATUS_NEEDS_IDENTITY)
    queue_store.update_library_status(req.request_id, found=True)
    monkeypatch.setattr("library_index.search_library", lambda *a, **k: [])

    maintenance.daily_library_check()
    assert queue_store.get_request(req.request_id).found_in_library is False


def test_reconcile_closes_movie_already_in_library(monkeypatch):
    # The real-world bug: a movie the user already has stays 'open' forever with
    # found_in_library=1, cluttering the grab queue, because found was only ever
    # an observation and nothing closed the request.
    req = queue_store.add_request(
        "the wizard of oz", "cole", media_type="movie",
        resolved_title="The Wizard of Oz", external_id="630",
        identity_source="tmdb", canonical_year=1939)
    queue_store.update_library_status(req.request_id, found=True)
    monkeypatch.setattr(
        "library_index.search_library",
        lambda *a, **k: [_Entry("The Wizard of Oz (1939)")])

    summary = maintenance.daily_library_check()

    r = queue_store.get_request(req.request_id)
    assert r.status == queue_store.STATUS_FULFILLED       # actually closes now
    assert r.found_in_library is True
    assert r.library_verified_at is not None              # library-satisfied
    assert r.placed_at is None                            # not a Plexxarr placement
    assert summary["fulfilled_from_library"] >= 1


def test_reconcile_does_not_close_season_specific_request(monkeypatch):
    # A season-specific TV request must NOT close just because the show exists:
    # 'found' proves the show is present, not that S05 is complete.
    req = queue_store.add_request(
        "law and order svu", "cole", media_type="tv",
        resolved_title="Law & Order: Special Victims Unit", external_id="75692",
        identity_source="tvdb", season=5)
    queue_store.update_library_status(req.request_id, found=True)
    monkeypatch.setattr(
        "library_index.search_library",
        lambda *a, **k: [_Entry("Law & Order Special Victims Unit - S01E01")])

    summary = maintenance.daily_library_check()

    r = queue_store.get_request(req.request_id)
    assert r.status == queue_store.STATUS_OPEN            # stays open
    assert summary.get("fulfilled_from_library", 0) == 0


def test_reconcile_does_not_close_grabbing_request(monkeypatch):
    # An in-flight grab must never be yanked closed from under itself.
    req = queue_store.add_request(
        "dune", "cole", media_type="movie", resolved_title="Dune",
        external_id="438631", identity_source="tmdb", canonical_year=2021)
    queue_store.update_library_status(req.request_id, found=True)
    queue_store.set_status(req.request_id, queue_store.STATUS_GRABBING)
    monkeypatch.setattr(
        "library_index.search_library",
        lambda *a, **k: [_Entry("Dune (2021)")])

    maintenance.daily_library_check()
    assert queue_store.get_request(req.request_id).status == \
        queue_store.STATUS_GRABBING


def test_reconcile_does_not_close_other_type(monkeypatch):
    # 'other' has no identity to trust — never auto-closed on a name hit.
    req = queue_store.add_request(
        "some request", "cole", media_type="other",
        resolved_title="Some Request")
    queue_store.update_library_status(req.request_id, found=True)
    monkeypatch.setattr(
        "library_index.search_library",
        lambda *a, **k: [_Entry("Some Request")])
    maintenance.daily_library_check()
    # 'other' can't even be found by identity; it must stay open regardless.
    assert queue_store.get_request(req.request_id).status != \
        queue_store.STATUS_FULFILLED


def test_reconcile_reopens_mislinked_fulfilled(monkeypatch, tmp_path):
    req = queue_store.add_request(
        "inception", "cole", media_type="movie", resolved_title="Inception",
        external_id="27205", identity_source="tmdb", canonical_year=2010)
    queue_store.set_status(req.request_id, queue_store.STATUS_FULFILLED)
    did = downloads_store.create_download(
        title="Inception", magnet=f"magnet:?xt=urn:btih:{_hash('inc')}",
        source="tpb", media_type="movie", request_id=req.request_id,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=True)
    downloads_store.link_request_download(req.request_id, did, "movie")
    # A recorded placement that is now GONE from disk (broken link).
    downloads_store.add_download_file(
        did, source_relative_path="Inception.mkv",
        source_absolute_path="/staging/Inception.mkv",
        final_path=str(tmp_path / "Movies" / "Inception (2010).mkv"),
        media_role="primary_video", verification_state="verified",
        moved_at="CURRENT")
    monkeypatch.setattr("library_index.search_library", lambda *a, **k: [])

    maintenance.daily_library_check()
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN


# ---------------------------------------------------------------------------
# Quarantine scoping + adoption (Tremors 2 for Tremors 1)
# ---------------------------------------------------------------------------

def test_tremors_scoping_and_staged_adoption(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    movie_root = tmp_path / "Movies"
    f = _write(staging / "Tremors.II.Aftershocks.1996.1080p.BluRay.x264-GRP.mkv")
    # Tremors 1 request; the Tremors 2 payload contradicts it.
    t1 = queue_store.add_request(
        "tremors", "cole", media_type="movie", resolved_title="Tremors",
        external_id="T1", identity_source="tmdb", canonical_year=1990)
    want1 = _want(title="Tremors", year=1990, source="tmdb", ext="T1")
    did = _make_download(want=want1, staging=staging, files=[f.name],
                         request_id=t1.request_id)
    dm = _dm(monkeypatch, dest_dir=movie_root)
    dm._post_process(did)

    # Blocked for Tremors 1, quarantined (bytes kept), request reopened.
    assert downloads_store.blocklist_entries_for_subject("tmdb:T1")
    assert f.exists()

    # A later Tremors 2 request: the release is NOT blocked for it, and the
    # staged copy is adoptable after fresh verification.
    from torrent_select import SelectWant
    from media_identity import MediaIdentity
    t2 = queue_store.add_request(
        "tremors 2", "cole", media_type="movie",
        resolved_title="Tremors II: Aftershocks", external_id="T2",
        identity_source="tmdb", canonical_year=1996)
    want2 = SelectWant(identity=MediaIdentity(
        media_type="movie", identity_source="tmdb", external_id="T2",
        canonical_title="Tremors II Aftershocks", canonical_year=1996))
    # No T2 block.
    assert downloads_store.blocklist_entries_for_subject("tmdb:T2") == []
    found = dm.find_adoptable_quarantine(want2)
    assert found is not None
    row, _result = found
    out = dm.adopt_quarantine(row.download_id, t2.request_id)
    assert "adopted" in out and "moved to" in out
    assert list(movie_root.rglob("*.mkv"))          # now placed for T2
    # The original Tremors 1 block is untouched.
    assert downloads_store.blocklist_entries_for_subject("tmdb:T1")


def test_quarantine_listed_with_no_silent_expiry(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    movie_root = tmp_path / "Movies"
    f = _write(staging / "Wrong.Movie.2.2001.1080p.mkv")
    want = _want(title="Wrong Movie", year=2000, source="tmdb", ext="w1")
    did = _make_download(want=want, staging=staging, files=[f.name])
    dm = _dm(monkeypatch, dest_dir=movie_root)
    dm._post_process(did)

    q = downloads_store.list_quarantined_downloads()
    assert len(q) == 1 and q[0].download_id == did
    # Age + size are inspectable; nothing auto-expires on a second sweep.
    assert q[0].created_at
    assert dm._staged_size(q[0]) > 0
    assert len(downloads_store.list_quarantined_downloads()) == 1


# ---------------------------------------------------------------------------
# Provenance survives removal; archived rows hidden by default
# ---------------------------------------------------------------------------

def test_removal_keeps_provenance(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    req = queue_store.add_request("inception", "cole", media_type="movie",
                                  resolved_title="Inception", external_id="27205",
                                  identity_source="tmdb")
    did = downloads_store.create_download(
        title="Inception", magnet=f"magnet:?xt=urn:btih:{_hash('rm')}",
        source="tpb", media_type="movie", request_id=req.request_id,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    downloads_store.link_request_download(req.request_id, did, "movie")
    downloads_store.add_download_file(
        did, source_relative_path="Inception.mkv",
        source_absolute_path="/tmp/Inception.mkv", final_path=None,
        media_role="primary_video", verification_state="verified")

    dm.remove(did, delete_files=False)

    # Archived, not deleted: the row + provenance survive.
    assert downloads_store.get_download(did) is not None
    assert downloads_store.get_download(did).removed_at is not None
    assert downloads_store.list_download_files(did)             # file rows kept
    assert downloads_store.downloads_for_request(req.request_id)  # junction kept
    # Hidden from the default list, visible when explicitly asked for.
    assert all(d.download_id != did for d in downloads_store.list_downloads())
    assert any(d.download_id == did
               for d in downloads_store.list_downloads(include_removed=True))


# ---------------------------------------------------------------------------
# Wrong-grab user action — block + reopen + quarantine (default) / recycle
# ---------------------------------------------------------------------------

def test_last_viable_blocked_records_deferral(tmp_path, monkeypatch):
    from torrent_search import CollectedPool, TorrentResult
    import request_intake
    from media_lookup import MediaResult
    match = MediaResult(title="Inception", year=2010, external_id="27205",
                        external_url="https://www.themoviedb.org/movie/27205",
                        media_type="movie", overview="", source="tmdb",
                        origin_countries=())
    req = request_intake.add_matched_request("Inception", "cole",
                                             media_type="movie", match=match)
    only = TorrentResult(
        title="Inception.2010.1080p.BluRay.x264-AAA",
        magnet=f"magnet:?xt=urn:btih:{_hash('only')}", size_bytes=1400 * _MB,
        seeders=80, source="tpb", media_type="movie")
    # Pre-block the only candidate for this identity.
    downloads_store.add_blocklist_entry(
        subject_type="request_identity", subject_key="tmdb:27205",
        reason_code=downloads_store.BLOCK_REASON_USER_WRONG_PICK,
        infohash=_hash("only"))
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr("download_manager.search_collect",
                        lambda *a, **k: CollectedPool(
                            results=(only,),
                            pool_stats={"per_source": {"tpb": 1},
                                        "collected": 1, "deduped": 1,
                                        "duplicates_removed": 0}))
    monkeypatch.setattr("download_manager._request_movie_minutes",
                        lambda *a, **k: None)

    started = dm.auto_grab_open_requests()
    assert started == []                             # nothing grabbed
    deferral = downloads_store.get_grab_deferral(f"req:{req.request_id}")
    assert deferral is not None
    assert deferral["reason"] == "last viable candidate blocked"
    assert deferral["next_attempt_at"]               # persisted for Task E


def test_wrong_grab_quarantines_by_default(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    req = queue_store.add_request(
        "inception", "cole", media_type="movie", resolved_title="Inception",
        external_id="27205", identity_source="tmdb")
    did = downloads_store.create_download(
        title="Inception.2010.1080p", magnet=f"magnet:?xt=urn:btih:{_hash('wg')}",
        source="tpb", media_type="movie", request_id=req.request_id,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)

    out = dm.mark_wrong_grab(did, recycle=False)
    assert "quarantined" in out
    entries = downloads_store.blocklist_entries_for_subject("tmdb:27205")
    assert any(e.reason_code == downloads_store.BLOCK_REASON_USER_WRONG_PICK
               for e in entries)
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN
    assert downloads_store.get_download(did).removed_at is not None
