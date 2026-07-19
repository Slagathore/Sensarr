# =============================================================================
# Fix-sprint section A regressions: stop grabbing what we already have.
#
# Three behaviours the bootstrap named, each of which failed at HEAD before the
# fix:
#   1. numeric titles ("2073", "1917", "2012") must match and never be stripped
#      as a year or read as a sequel/year in verification;
#   2. auto-grab must refuse a request whose identity is already in the library
#      at grab time, independent of the daily found flag;
#   3. a request whose grabs keep failing must land in needs_attention once the
#      per-request attempt cap is reached, not loop forever.
# =============================================================================

import types
from pathlib import Path

import pytest

import config
import db
import downloads_store
import maintenance
import media_identity
import queue_store
import request_intake
import verification
from download_manager import DownloadManager
from media_lookup import MediaResult


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


def _entry(name):
    """A library-search result stand-in (only .name is read)."""
    return types.SimpleNamespace(name=name)


def _movie(title="Inception", year=2010, ext="27205"):
    return MediaResult(
        title=title, year=year, external_id=ext,
        external_url=f"https://www.themoviedb.org/movie/{ext}",
        media_type="movie", overview="", source="tmdb", origin_countries=())


# ---------------------------------------------------------------------------
# 1. Numeric-title matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2073", "2073"),
    ("2073 (2024)", "2073"),
    ("1917", "1917"),
    ("1917 (2019)", "1917"),
    ("2012", "2012"),
])
def test_core_title_keeps_numeric_titles(raw, expected):
    # HEAD stripped the whole title to "" for a year-like name.
    assert maintenance._core_title(raw) == expected


@pytest.mark.parametrize("title,year,release", [
    ("2073", 2024, "2073.2024.1080p.WEB.x264-GRP.mkv"),
    ("2073", 2024, "2073.1080p.WEB.x264-GRP.mkv"),
    ("1917", 2019, "1917.2019.1080p.BluRay.x264-GRP.mkv"),
    ("2012", 2009, "2012.2009.1080p.BluRay.x264-GRP.mkv"),
])
def test_numeric_title_not_read_as_year(title, year, release):
    # HEAD parsed the numeric title AS a year and quarantined the correct copy.
    want = media_identity.MediaIdentity(
        media_type="movie", canonical_title=title, canonical_year=year)
    verdict = media_identity.compare_media_identity(
        want, verification.parse_file(Path(release)))
    assert verdict.ok, verdict.reason_code


def test_genuine_year_mismatch_still_rejected():
    want = media_identity.MediaIdentity(
        media_type="movie", canonical_title="Inception", canonical_year=2010)
    verdict = media_identity.compare_media_identity(
        want, verification.parse_file(Path("Inception.1999.1080p.mkv")))
    assert not verdict.ok and verdict.reason_code == "year_mismatch"


def test_library_identity_eval_matches_numeric_title():
    want = media_identity.MediaIdentity(
        media_type="movie", canonical_title="2073", canonical_year=2024)
    matched, contradicting = maintenance._library_identity_eval(
        want, [_entry("2073 (2024)")])
    assert matched and not contradicting


# ---------------------------------------------------------------------------
# Spelled-number / digit title equivalence (Twelve Monkeys == 12 Monkeys)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word_title,digit_title", [
    ("Twelve Monkeys", "12 Monkeys"),
    ("Seven Samurai", "7 Samurai"),
    ("Ocean's Eleven", "Ocean's 11"),
    ("Three Billboards", "3 Billboards"),
    ("Twenty One Pilots", "21 Pilots"),
])
def test_spelled_and_digit_titles_are_equal(word_title, digit_title):
    assert media_identity.normalize_title(word_title) == \
        media_identity.normalize_title(digit_title)
    assert not media_identity.sequel_mismatch(word_title, digit_title)


def test_twelve_monkeys_library_repro():
    # Real-data repro: request wants "Twelve Monkeys" (tmdb 63, 1995); Plex
    # titles the owned film "12 Monkeys" with no year in the entry name.
    want = media_identity.MediaIdentity(
        media_type="movie", identity_source="tmdb", external_id="63",
        canonical_title="Twelve Monkeys", canonical_year=1995)
    matched, contradicting = maintenance._library_identity_eval(
        want, [_entry("12 Monkeys")])
    assert matched and not contradicting


def test_spelled_number_sequel_still_rejected():
    # A genuine sequel of a spelled-number title must still contradict.
    want = media_identity.MediaIdentity(
        media_type="movie", canonical_title="Twelve Monkeys", canonical_year=1995)
    matched, contradicting = maintenance._library_identity_eval(
        want, [_entry("12 Monkeys 2")])
    assert not matched and contradicting
    assert media_identity.sequel_mismatch("Twelve Monkeys", "12 Monkeys 2")
    # And two distinct numeric titles are not merged by the number canonicaliser.
    assert media_identity.sequel_signature("2073") == media_identity.sequel_signature("2074")
    v = media_identity.compare_media_identity(
        media_identity.MediaIdentity(
            media_type="movie", canonical_title="2073", canonical_year=2024),
        verification.parse_file(Path("2074.2025.1080p.WEB.x264-GRP.mkv")))
    assert not v.ok and v.reason_code == "year_mismatch"


# ---------------------------------------------------------------------------
# 2. At-grab-time library refusal (independent of the daily found flag)
# ---------------------------------------------------------------------------

def test_auto_grab_refuses_request_already_in_library(monkeypatch):
    req = request_intake.add_matched_request(
        "2073", "cole", media_type="movie",
        match=_movie(title="2073", year=2024, ext="700"))
    assert req.status == queue_store.STATUS_OPEN
    assert not req.found_in_library  # daily flag never set — the whole point

    # The library already holds it (numeric title the daily check couldn't set).
    monkeypatch.setattr("library_index.search_library",
                        lambda *a, **k: [_entry("2073 (2024)")])
    # The gate must fire before any search — grabbing would be the bug.
    monkeypatch.setattr("download_manager._request_movie_minutes",
                        lambda *a, **k: None)
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)

    started = dm.auto_grab_open_requests()

    assert started == []
    row = queue_store.get_request(req.request_id)
    assert row.found_in_library is True     # gate marked it found and skipped
    assert not downloads_store.request_ids_with_downloads()  # nothing grabbed


# ---------------------------------------------------------------------------
# 3. Per-request attempt cap -> needs_attention
# ---------------------------------------------------------------------------

def _fail_a_grab(req_id, seed):
    did = downloads_store.create_download(
        title=f"copy {seed}",
        magnet=f"magnet:?xt=urn:btih:{'abc0123456789def0123456789abcdef0123456'[:39]}{seed}&dn=x",
        source="tpb", media_type="movie", request_id=req_id,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    downloads_store.set_status(did, "error", error="permission denied",
                               completed=True)
    return did


def test_attempt_cap_escalates_to_needs_attention(monkeypatch):
    monkeypatch.setattr(config, "GRAB_ATTEMPT_CAP", 3)
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())

    # Below the cap the request keeps re-opening (grabbable for a DIFFERENT
    # release), never silently stuck.
    _fail_a_grab(req.request_id, 1)
    _fail_a_grab(req.request_id, 2)
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN
    assert downloads_store.request_grab_attempts(req.request_id) == 2

    # The cap-th terminal failure escalates instead of looping forever.
    _fail_a_grab(req.request_id, 3)
    assert (queue_store.get_request(req.request_id).status
            == queue_store.STATUS_NEEDS_ATTENTION)


# ---------------------------------------------------------------------------
# Gap 1: a cancelled in-flight grab must resolve, never strand at 'grabbing'
# ---------------------------------------------------------------------------

def _grab_a_copy(dm, req_id, seed):
    from torrent_search import TorrentResult
    res = TorrentResult(
        title=f"Inception.2010.copy{seed}-GRP",
        magnet=f"magnet:?xt=urn:btih:{'0123456789abcdef0123456789abcdef0123456'[:39]}{seed}&dn=x",
        size_bytes=1400 * 1024 ** 2, seeders=50, source="tpb",
        media_type="movie")
    return dm.grab(res, request_id=req_id, request_title="Inception 2010")


def test_cancel_in_flight_grab_reopens_request(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())

    did = _grab_a_copy(dm, req.request_id, 1)
    # grab() marks the request in-flight.
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_GRABBING

    dm.cancel(did)
    # A cancel must not leave it stuck at 'grabbing' (auto-grab never scans it).
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN


def test_cancel_at_cap_escalates(monkeypatch):
    monkeypatch.setattr(config, "GRAB_ATTEMPT_CAP", 2)
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())

    d1 = _grab_a_copy(dm, req.request_id, 1)
    dm.cancel(d1)  # attempt 1 -> reopens
    assert queue_store.get_request(req.request_id).status == queue_store.STATUS_OPEN
    d2 = _grab_a_copy(dm, req.request_id, 2)
    dm.cancel(d2)  # attempt 2 == cap -> needs_attention
    assert (queue_store.get_request(req.request_id).status
            == queue_store.STATUS_NEEDS_ATTENTION)


# ---------------------------------------------------------------------------
# Gap 2: intake dedupes identical identities (queue_store.add_request untouched)
# ---------------------------------------------------------------------------

def _tv(title="Some Show", ext="s-9"):
    return MediaResult(
        title=title, year=2020, external_id=ext,
        external_url=f"https://www.themoviedb.org/tv/{ext}",
        media_type="tv", overview="", source="tmdb", origin_countries=())


def test_intake_dedupes_same_movie():
    m = _movie(title="Dune", year=2021, ext="438631")
    r1 = request_intake.add_matched_request("dune", "a", media_type="movie", match=m)
    r2 = request_intake.add_matched_request("Dune!!", "b", media_type="movie", match=m)
    assert r2.request_id == r1.request_id
    assert len(queue_store.list_requests(status="all", limit=50)) == 1


def test_intake_keeps_distinct_seasons():
    show = _tv()
    r1 = request_intake.add_matched_request(
        "show", "a", media_type="tv", match=show, season=1)
    r2 = request_intake.add_matched_request(
        "show", "b", media_type="tv", match=show, season=2)
    assert r1.request_id != r2.request_id
    assert len(queue_store.list_requests(status="all", limit=50)) == 2


def test_intake_no_reopen_when_fulfilled_and_in_library(monkeypatch):
    m = _movie(title="2073", year=2024, ext="700")
    r1 = request_intake.add_matched_request("2073", "a", media_type="movie", match=m)
    queue_store.set_status(r1.request_id, queue_store.STATUS_FULFILLED)
    # Still on disk -> a re-request must not create a new open row.
    monkeypatch.setattr("library_index.search_library",
                        lambda *a, **k: [_entry("2073 (2024)")])
    r2 = request_intake.add_matched_request("2073", "b", media_type="movie", match=m)
    assert r2.request_id == r1.request_id
    rows = queue_store.list_requests(status="all", limit=50)
    assert len(rows) == 1 and rows[0].status == queue_store.STATUS_FULFILLED


def test_intake_reopens_when_fulfilled_but_gone(monkeypatch):
    m = _movie(title="Heat", year=1995, ext="949")
    r1 = request_intake.add_matched_request("heat", "a", media_type="movie", match=m)
    queue_store.set_status(r1.request_id, queue_store.STATUS_FULFILLED)
    # No longer on disk -> a fresh request IS allowed.
    monkeypatch.setattr("library_index.search_library", lambda *a, **k: [])
    r2 = request_intake.add_matched_request("heat", "b", media_type="movie", match=m)
    assert r2.request_id != r1.request_id
