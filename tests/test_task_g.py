# =============================================================================
# Task G (Phase 6) — per-show "match my existing sizes" override.
#
# The deterministic derivation (size_match.pick_from_sizes) is pinned with
# EXACT expected bucket/target values on synthetic size sets (uniform, bimodal,
# outlier-heavy, tied buckets), proven permutation-invariant, and the wiring is
# proven end to end: an override show accepts a candidate the global max
# vetoes, and the recorded selection run's pick_meta says
# size_mode=match_library.
# =============================================================================

import hashlib
import itertools
import json
import math
import random

import pytest

import config
import db
import download_manager
import downloads_store
import queue_store
import shows_store
import size_match
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
def _fresh():
    _clean()
    yield
    _clean()


# ---------------------------------------------------------------------------
# The algorithm — exact values (fixed-origin log-space buckets)
# ---------------------------------------------------------------------------

def test_bucket_is_fixed_origin_log_space():
    # bucket = floor(log(size)/log(1.10)) — pinned against the definition, so
    # a refactor that shifts the origin (v1's underdetermined bins) fails here.
    for size in (50 * MB, 300 * MB, 502 * MB, 1500 * MB, 12000 * MB):
        assert size_match.bucket_for(size) == math.floor(
            math.log(size) / math.log(1.10))
    assert size_match.bucket_for(502 * MB) == 210
    assert size_match.bucket_for(300 * MB) == 205
    assert size_match.bucket_for(1500 * MB) == 222


def test_uniform_sample_exact_target():
    sizes = [500 * MB, 505 * MB, 510 * MB, 495 * MB, 502 * MB]
    pick = size_match.pick_from_sizes(sizes, 24.0)
    assert pick.ok
    assert pick.size_mode == "match_library"
    assert pick.sample_count == 5
    assert pick.winning_bucket == 210          # all five land in one bucket
    assert pick.bucket_population == 5
    assert pick.median_bytes == 502 * MB       # median of the winning bucket
    assert pick.mb_per_min == pytest.approx(502 / 24.0)
    assert pick.fallback_reason is None


def test_bimodal_sample_majority_bucket_wins():
    small = [300 * MB, 305 * MB, 310 * MB, 295 * MB, 302 * MB]   # bucket 205 x5
    big = [1500 * MB, 1520 * MB, 1480 * MB]                      # bucket 222 x3
    pick = size_match.pick_from_sizes(small + big, 24.0)
    assert pick.ok
    assert pick.winning_bucket == 205
    assert pick.bucket_population == 5
    assert pick.median_bytes == 302 * MB
    assert pick.mb_per_min == pytest.approx(302 / 24.0)


def test_outlier_heavy_sample_ignores_outliers():
    # Four consistent files + a tiny sample-sized junk + a monster remux.
    sizes = [520 * MB, 525 * MB, 530 * MB, 522 * MB, 50 * MB, 12000 * MB]
    pick = size_match.pick_from_sizes(sizes, 24.0)
    assert pick.ok
    assert pick.winning_bucket == 211          # the 520-530 MB cluster
    assert pick.bucket_population == 4
    # Even count in the bucket: median = mean of the middle two (522, 525).
    assert pick.median_bytes == int((522 * MB + 525 * MB) / 2)


def test_tied_buckets_take_the_one_nearest_the_overall_median():
    # Two buckets of two. Overall median = (302+800)/2 = 551 MB; bucket
    # medians 301 vs 802.5 — 301 is nearer (250 vs 251.5), so 205 wins.
    pick = size_match.pick_from_sizes(
        [300 * MB, 302 * MB, 800 * MB, 805 * MB], 24.0)
    assert pick.winning_bucket == 205
    assert pick.median_bytes == int((300 * MB + 302 * MB) / 2)
    # Shift the small cluster so the LARGE bucket median is nearer:
    # overall median = (310+800)/2 = 555; |305-555|=250 vs |802.5-555|=247.5.
    pick2 = size_match.pick_from_sizes(
        [300 * MB, 310 * MB, 800 * MB, 805 * MB], 24.0)
    assert pick2.winning_bucket == 215
    assert pick2.median_bytes == int((800 * MB + 805 * MB) / 2)


def test_permutation_of_input_order_changes_nothing():
    base = [520 * MB, 525 * MB, 530 * MB, 522 * MB, 50 * MB, 12000 * MB]
    expected = size_match.pick_from_sizes(base, 24.0)
    for perm in itertools.islice(itertools.permutations(base), 0, 720, 37):
        assert size_match.pick_from_sizes(list(perm), 24.0) == expected
    rng = random.Random(42)
    for _ in range(20):
        shuffled = list(base)
        rng.shuffle(shuffled)
        assert size_match.pick_from_sizes(shuffled, 24.0) == expected


def test_fewer_than_three_files_falls_back_flagged():
    pick = size_match.pick_from_sizes([500 * MB, 505 * MB], 24.0)
    assert not pick.ok
    assert pick.mb_per_min is None
    assert pick.sample_count == 2
    assert "need 3" in pick.fallback_reason
    meta = pick.meta()
    assert meta["size_mode"] == "match_library"
    assert meta["fallback_reason"] == pick.fallback_reason
    # Both size rules are NAMED in the explanation, and they are distinct.
    assert "1.2x" in meta["size_rules"]["oversize_deferral"]
    assert "1.8x" in meta["size_rules"]["hard_max"]


def test_zero_and_negative_sizes_are_excluded():
    pick = size_match.pick_from_sizes(
        [0, -5, 500 * MB, 505 * MB, 510 * MB], 24.0)
    assert pick.sample_count == 3
    assert pick.ok


# ---------------------------------------------------------------------------
# Eligibility sampling (stale paths, subtitles, samples, zero sizes)
# ---------------------------------------------------------------------------

class _Ep:
    def __init__(self, path, has_file=True):
        self.has_file = has_file
        self.file_path = path


def _make_file(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


def test_eligible_episode_sizes_filters(tmp_path):
    import torrent_routing
    import verification
    good1 = _make_file(tmp_path / "Show - S01E01.mkv", 500 * MB)
    good2 = _make_file(tmp_path / "Show - S01E02.mkv", 505 * MB)
    zero = _make_file(tmp_path / "Show - S01E03.mkv", 0)
    sub = _make_file(tmp_path / "Show - S01E04.srt", 90 * 1024)
    sample = _make_file(tmp_path / "Show - S01E05.sample.mkv", 30 * MB)
    eps = [
        _Ep(str(good1)), _Ep(str(good2)), _Ep(str(zero)), _Ep(str(sub)),
        _Ep(str(sample)),
        _Ep(str(tmp_path / "gone.mkv")),          # stale file_path
        _Ep(None),                                # no path recorded
        _Ep(str(good1), has_file=False),          # not marked on-disk
    ]

    def junk(name):
        return bool(verification._SAMPLE_RE.search(name)
                    or verification._EXTRA_NAME_RE.search(name))

    sizes = size_match.eligible_episode_sizes(
        eps, video_exts=torrent_routing.VIDEO_EXTENSIONS, is_junk_name=junk)
    assert sorted(sizes) == [500 * MB, 505 * MB]


# ---------------------------------------------------------------------------
# Wiring — an override show accepts what the global max vetoes
# ---------------------------------------------------------------------------

def _seed_override_show(tmp_path, *, size_mode):
    show_id = shows_store.upsert_show(
        title="House of the Dragon", media_type="tv",
        source="tvdb", external_id="371572")
    if size_mode != "global":
        shows_store.set_show_size_mode(show_id, size_mode)
    shows_store.set_show_runtime(show_id, 24.0)
    # Three existing 500-MB-class episodes on disk + one aired missing episode.
    for i, size in enumerate((500 * MB, 505 * MB, 510 * MB), start=1):
        f = _make_file(tmp_path / f"HotD - S01E0{i}.mkv", size)
        shows_store.set_episode_file(show_id, 1, i, str(f))
    shows_store.replace_episodes(show_id, [
        type("E", (), {"season": 1, "episode": i, "title": f"E{i}",
                       "air_date": "2024-01-01"})() for i in range(1, 5)
    ])
    for i, size in enumerate((500 * MB, 505 * MB, 510 * MB), start=1):
        shows_store.set_episode_file(
            show_id, 1, i, str(tmp_path / f"HotD - S01E0{i}.mkv"))
    return shows_store.get_show(show_id)


def _dm(monkeypatch):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    return dm


def _big_candidate():
    ih = hashlib.sha1(b"hotd-big").hexdigest()
    return TorrentResult(
        title="House.of.the.Dragon.S01E04.1080p.WEB.h264-GRP",
        magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=505 * MB,
        seeders=50, source="tpb", media_type="tv")


@pytest.fixture
def _tight_global_max(monkeypatch):
    # Global TV cap: 5 MB/min x 24 min = 120 MB — vetoes a 505 MB episode.
    monkeypatch.setattr(config, "SIZE_MAX_MB_PER_MIN_TV", 5.0)
    monkeypatch.setattr(config, "SIZE_PREF_MB_PER_MIN_TV", 3.0)


def test_global_mode_vetoes_oversized_candidate(tmp_path, monkeypatch,
                                                _tight_global_max):
    show = _seed_override_show(tmp_path, size_mode="global")
    dm = _dm(monkeypatch)
    ep = [e for e in shows_store.missing_episodes(show.show_id)][0]

    class _Pool:
        results = (_big_candidate(),)
        pool_stats = {"per_source": {"tpb": 1}}
    monkeypatch.setattr(download_manager, "search_collect",
                        lambda *a, **k: _Pool())
    grabbed = []
    monkeypatch.setattr(dm, "grab", lambda *a, **k: grabbed.append(k) or 999)

    started = dm._grab_one_episode(show, ep)

    assert started == [] and not grabbed   # the global 120 MB cap rejects it
    run = downloads_store.get_selection_run_for_download(999)
    assert run is None


def test_match_library_accepts_what_global_max_vetoes(tmp_path, monkeypatch,
                                                      _tight_global_max):
    show = _seed_override_show(tmp_path, size_mode="match_library")
    assert show.size_mode == "match_library"
    dm = _dm(monkeypatch)
    ep = [e for e in shows_store.missing_episodes(show.show_id)][0]

    class _Pool:
        results = (_big_candidate(),)
        pool_stats = {"per_source": {"tpb": 1}}
    monkeypatch.setattr(download_manager, "search_collect",
                        lambda *a, **k: _Pool())
    grabbed = []
    monkeypatch.setattr(
        dm, "grab", lambda *a, **k: grabbed.append((a, k)) or 999)

    started = dm._grab_one_episode(show, ep)

    # Derived pref = 505 MB / 24 min ≈ 21 MB/min; hard max 1.8x ≈ 916 MB —
    # the 505 MB candidate passes despite the 120 MB global cap.
    assert started == [999] and grabbed

    # pick_meta on the persisted run says size_mode=match_library with the
    # full derivation (sample count, winning bucket, median, MB/min).
    run = downloads_store.get_selection_run_for_download(999)
    assert run is not None
    stats = json.loads(run.pool_stats_json)
    sm = stats["size_match"]
    assert sm["size_mode"] == "match_library"
    assert sm["sample_count"] == 3
    assert sm["winning_bucket"] == 210
    assert sm["median_bytes"] == 505 * MB
    assert sm["derived_mb_per_min"] == pytest.approx(505 / 24.0, abs=0.01)
    assert sm["fallback_reason"] is None
    # Both size rules are named on the decision.
    assert "1.2x" in sm["size_rules"]["oversize_deferral"]
    assert "1.8x" in sm["size_rules"]["hard_max"]


def test_match_library_with_thin_sample_falls_back_to_global(
        tmp_path, monkeypatch, _tight_global_max):
    # Only 2 files on disk -> fallback: the global cap still vetoes, and the
    # recorded pick_meta carries the flagged fallback reason.
    show_id = shows_store.upsert_show(
        title="Thin Show", media_type="tv", source="tvdb", external_id="42")
    shows_store.set_show_size_mode(show_id, "match_library")
    shows_store.set_show_runtime(show_id, 24.0)
    shows_store.replace_episodes(show_id, [
        type("E", (), {"season": 1, "episode": i, "title": f"E{i}",
                       "air_date": "2024-01-01"})() for i in range(1, 4)
    ])
    for i, size in enumerate((500 * MB, 505 * MB), start=1):
        f = _make_file(tmp_path / f"Thin - S01E0{i}.mkv", size)
        shows_store.set_episode_file(show_id, 1, i, str(f))
    show = shows_store.get_show(show_id)
    dm = _dm(monkeypatch)
    ep = shows_store.missing_episodes(show_id)[0]

    ih = hashlib.sha1(b"thin-big").hexdigest()
    cand = TorrentResult(
        title="Thin.Show.S01E03.1080p.WEB.h264-GRP",
        magnet=f"magnet:?xt=urn:btih:{ih}", size_bytes=505 * MB,
        seeders=9, source="tpb", media_type="tv")

    class _Pool:
        results = (cand,)
        pool_stats = {"per_source": {"tpb": 1}}
    monkeypatch.setattr(download_manager, "search_collect",
                        lambda *a, **k: _Pool())
    monkeypatch.setattr(dm, "grab", lambda *a, **k: 999)

    started = dm._grab_one_episode(show, ep)
    assert started == []                      # global cap still in force

    with db.connect() as conn:
        row = conn.execute(
            "SELECT pool_stats_json FROM selection_runs ORDER BY id DESC"
            " LIMIT 1").fetchone()
    sm = json.loads(row[0])["size_match"]
    assert sm["fallback_reason"] and "need 3" in sm["fallback_reason"]
    assert sm["derived_mb_per_min"] is None


def test_size_pick_cached_per_pass(tmp_path, monkeypatch):
    show = _seed_override_show(tmp_path, size_mode="match_library")
    dm = _dm(monkeypatch)
    calls = []
    real = size_match.pick_from_sizes

    def counting(sizes, minutes):
        calls.append(1)
        return real(sizes, minutes)
    monkeypatch.setattr(size_match, "pick_from_sizes", counting)

    first = dm._size_pick_for_show(show)
    second = dm._size_pick_for_show(show)
    assert first is second and len(calls) == 1
    dm._size_pick_cache.clear()               # what a new pass does
    third = dm._size_pick_for_show(show)
    assert len(calls) == 2 and third == first


def test_set_show_size_mode_rejects_unknown_mode():
    show_id = shows_store.upsert_show(
        title="X", media_type="tv", source="tvdb", external_id="9")
    with pytest.raises(ValueError):
        shows_store.set_show_size_mode(show_id, "bogus")
    shows_store.set_show_size_mode(show_id, "match_library")
    assert shows_store.get_show(show_id).size_mode == "match_library"
    shows_store.set_show_size_mode(show_id, "global")
    assert shows_store.get_show(show_id).size_mode == "global"
