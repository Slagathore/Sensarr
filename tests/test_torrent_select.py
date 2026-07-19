"""Task B — the deterministic selection engine (torrent_select.select_torrent)
and the widened pool collector (torrent_search.search_collect).

Covers the section-4 test paragraph in full:
  * the three real-failure fixtures (Angry Birds 2, MAFS AU, cancelled-then-block),
  * the golden corpus (parse+gate+score ordering),
  * determinism under shuffle,
  * the pool-widening regression (correct low-seed release beats wrong high-seed),
  * absence-vs-contradiction (unmarked country / missing year pass and score down),
  * an RTN parser failure on ONE candidate rejects it visibly without crashing.
"""
import json
import random
from pathlib import Path

import pytest

import torrent_search
import torrent_select as ts
from media_identity import MediaIdentity
from torrent_select import (
    BlocklistEntry,
    Candidate,
    SelectWant,
    make_blocklist,
    select_torrent,
)

_CORPUS = json.loads(
    (Path(__file__).parent / "data" / "torrent_corpus.json").read_text(encoding="utf-8")
)

_GB = 1024 ** 3
_MB = 1024 ** 2


def _cand(title, seeders=50, size=1400 * _MB, infohash=None, source="tpb"):
    if infohash is None:
        # Deterministic-but-unique 40-hex hash from the title.
        import hashlib
        infohash = hashlib.sha1(title.encode()).hexdigest()
    return Candidate(title=title, infohash=infohash, size_bytes=size,
                     seeders=seeders, source=source)


def _movie_want(title, year, countries=(), **kw):
    return SelectWant(
        identity=MediaIdentity(
            media_type="movie", identity_source="tmdb", external_id="1",
            canonical_title=title, canonical_year=year,
            origin_countries=tuple(countries)),
        size_pref_mb_min=kw.pop("pref", 10.0), fallback_minutes=120.0, **kw)


def _tv_want(title, season, countries=(), aliases=(), **kw):
    return SelectWant(
        identity=MediaIdentity(
            media_type="tv", identity_source="tvdb", external_id="1",
            canonical_title=title, origin_countries=tuple(countries),
            aliases=tuple(aliases), season=season),
        size_pref_mb_min=kw.pop("pref", 22.0), fallback_minutes=24.0, **kw)


# ---------------------------------------------------------------------------
# Real-failure fixture 1: Angry Birds 2 rejected, valid original accepted.
# ---------------------------------------------------------------------------

def test_angry_birds_sequel_rejected_original_accepted():
    want = _movie_want("The Angry Birds Movie", 2016, countries=["US"])
    sequel = _cand("The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GROUP",
                   seeders=900, size=2 * _GB)
    original = _cand("The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE",
                     seeders=30, size=1400 * _MB)

    decision = select_torrent([sequel, original], want)

    assert decision.chosen_title == original.title
    by_title = {v.title: v for v in decision.verdicts}
    # The sequel dies on the title-identity gate even though it out-seeds the
    # original 30x and RTN.title_match() alone would accept it.
    assert by_title[sequel.title].passed is False
    assert by_title[sequel.title].reason_code == "sequel_mismatch"
    assert by_title[original.title].passed is True


def test_angry_birds_numeric_only_variant_rejected():
    # A bare "Angry Birds 2"-style name (numeric sequel, no year evidence) still
    # dies on the numeric-title guard, not just the year gate.
    want = _movie_want("The Angry Birds Movie", None)
    cand = _cand("The.Angry.Birds.Movie.2.1080p.WEB.x264")
    decision = select_torrent([cand], want)
    assert decision.chosen is False
    assert decision.verdicts[0].reason_code in (
        "sequel_mismatch", "numeric_title_mismatch")


# ---------------------------------------------------------------------------
# Real-failure fixture 2: MAFS AU rejected as a COUNTRY contradiction, not merely
# on title, against a US want carrying year+country+aliases.
# ---------------------------------------------------------------------------

def test_mafs_au_rejected_as_country_contradiction_not_title():
    want = _tv_want("Married at First Sight", season=18, countries=["US"],
                    aliases=["Married at First Sight US"])
    au = _cand("Married.At.First.Sight.AU.S13.1080p.WEB-DL-GROUP",
               seeders=800, size=3 * _GB)
    decision = select_torrent([au], want)

    assert decision.chosen is False
    verdict = decision.verdicts[0]
    # The show TITLE matches (both parse to "Married at First Sight"); the reject
    # MUST come from the country-edition gate, proving it is not a title reject.
    assert verdict.reason_code == "country_edition_contradiction"


def test_mafs_us_accepted_and_scores_country_bonus():
    want = _tv_want("Married at First Sight", season=18, countries=["US"],
                    aliases=["Married at First Sight US"])
    us = _cand("Married.at.First.Sight.US.S18E05.720p.WEB.h264-GROUP",
               seeders=40, size=1200 * _MB)
    decision = select_torrent([us], want)
    assert decision.chosen_title == us.title
    assert decision.scores[0].components["country_alias"] == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Real-failure fixture 3: cancelled-then-research excludes a blocklisted infohash
# via the gate INPUT (the module stays pure — the block is passed in).
# ---------------------------------------------------------------------------

def test_cancelled_then_research_excludes_blocklisted_infohash():
    want = _movie_want("Tremors", 1990)
    bad_hash = "1" * 40
    blocked = _cand("Tremors.1990.1080p.BluRay.x265-RARBG", seeders=999,
                    size=1600 * _MB, infohash=bad_hash)
    other = _cand("Tremors.1990.1080p.WEB-DL.x264-GRP", seeders=20,
                  size=1500 * _MB)

    blocklist = make_blocklist([
        BlocklistEntry(reason_code="user_wrong_pick", infohash=bad_hash)])
    decision = select_torrent([blocked, other], want, blocklist=blocklist)

    assert decision.chosen_title == other.title
    by_title = {v.title: v for v in decision.verdicts}
    assert by_title[blocked.title].reason_code == "blocklisted"


def test_race_loser_and_transient_reasons_never_block():
    want = _movie_want("Tremors", 1990)
    h = "2" * 40
    cand = _cand("Tremors.1990.1080p.BluRay.x265-RARBG", infohash=h)
    # A race_loser / tracker_timeout entry for the very same infohash must NOT
    # block it — those reasons are transient, not identity failures.
    for reason in ("race_loser", "tracker_timeout", "download_stalled",
                   "client_error", "user_cancel_no_block"):
        bl = make_blocklist([BlocklistEntry(reason_code=reason, infohash=h)])
        decision = select_torrent([cand], want, blocklist=bl)
        assert decision.chosen_title == cand.title, reason


def test_blocklist_matches_on_parsed_title_size_group():
    want = _movie_want("Tremors", 1990)
    cand = _cand("Tremors.1990.1080p.BluRay.x265-RARBG", size=1600 * _MB)
    # No infohash match — match on (title, size within 2%, group) instead.
    bl = make_blocklist([BlocklistEntry(
        reason_code="identity_mismatch", parsed_title="Tremors",
        size_bytes=int(1600 * _MB * 1.01), release_group="RARBG")])
    decision = select_torrent([cand], want, blocklist=bl)
    assert decision.chosen is False
    assert decision.verdicts[0].reason_code == "blocklisted"


# ---------------------------------------------------------------------------
# Golden corpus: parse + gate + score ordering.
# ---------------------------------------------------------------------------

def test_golden_corpus_gate_and_score_ordering():
    # Want "Tremors" (1990). From the committed corpus: the original passes, the
    # sequel "Tremors II" is a sequel_mismatch, the two CAM entries are trash.
    want = _movie_want("Tremors", 1990)
    corpus = {c["raw"]: c for c in _CORPUS["cases"]}
    cands = [
        _cand("Tremors.1990.1080p.BluRay.x265-RARBG", seeders=25, size=1600 * _MB),
        _cand("Tremors.II.Aftershocks.1996.1080p.BluRay.x264", seeders=500,
              size=1600 * _MB),
        # A title-valid CAM so it reaches the CAM gate (7) instead of the title
        # gate (3); RTN flags it trash exactly like the corpus CAM entries.
        _cand("Tremors.1990.CAM.x264-YIFY", seeders=999, size=1200 * _MB),
    ]
    decision = select_torrent(cands, want)
    assert corpus["Tremors.1990.1080p.BluRay.x265-RARBG"]["trash"] is False
    assert decision.chosen_title == "Tremors.1990.1080p.BluRay.x265-RARBG"
    reasons = {v.title: v.reason_code for v in decision.verdicts}
    # "Tremors II Aftershocks" is rejected on the title-identity gate family
    # (title_match fails first here; the sequel guard would catch the roman "II"
    # otherwise). test_angry_birds pins sequel_mismatch precisely.
    assert reasons["Tremors.II.Aftershocks.1996.1080p.BluRay.x264"] in (
        "title_mismatch", "sequel_mismatch")
    assert reasons["Tremors.1990.CAM.x264-YIFY"] == "cam_or_trash"


def test_cam_gate_agrees_with_corpus_and_allow_cam_bypasses():
    want = _movie_want("Deadpool and Wolverine", 2024)
    cam = _cand("Deadpool.and.Wolverine.2024.HDTS.c1nem4", seeders=999,
                size=1500 * _MB)
    # Default: CAM/TS trash is rejected.
    assert select_torrent([cam], want).verdicts[0].reason_code == "cam_or_trash"
    # allow_cam bypasses the gate and the release scores (RTN rank is very
    # negative but it does not crash).
    want_allow = _movie_want("Deadpool and Wolverine", 2024, allow_cam=True)
    d = select_torrent([cam], want_allow)
    assert d.chosen_title == cam.title


def test_optional_cam_check_hook_participates():
    # The wiring layer can pass video_quality.is_cam_release; a non-RTN CAM name
    # then still rejects.
    want = _movie_want("Some Movie", 2020)
    # A name RTN may not flag but CAM_RE does.
    cand = _cand("Some.Movie.2020.HDCAM.x264", size=1400 * _MB)

    def cam_check(title):
        return "cam" in title.lower()

    d = select_torrent([cand], want, cam_check=cam_check)
    assert d.verdicts[0].reason_code == "cam_or_trash"


# ---------------------------------------------------------------------------
# Task D: the minimum-seeder gate. A release nobody seeds never finishes, so it
# is rejected before it is ever grabbed — but only when the want asks for it
# (min_seeders defaults to 0, so the pure module stays policy-free and the
# zero-seeder race can still gamble on unseeded candidates).
# ---------------------------------------------------------------------------

def test_min_seeders_gate_rejects_unseeded_release():
    dead = _cand("Tremors.1990.1080p.BluRay.x265-RARBG", seeders=0,
                 size=1600 * _MB)
    decision = select_torrent([dead], _movie_want("Tremors", 1990, min_seeders=1))
    assert not decision.chosen
    assert decision.verdicts[0].reason_code == "insufficient_seeders"


def test_min_seeders_gate_disabled_by_default_passes_unseeded():
    dead = _cand("Tremors.1990.1080p.BluRay.x265-RARBG", seeders=0,
                 size=1600 * _MB)
    # Default want has min_seeders=0 → the gate is off and the 0-seed release
    # survives (the zero-seeder race path relies on exactly this).
    decision = select_torrent([dead], _movie_want("Tremors", 1990))
    assert decision.chosen
    assert decision.verdicts[0].reason_code == "ok"


def test_min_seeders_gate_prefers_seeded_over_dead():
    dead = _cand("Tremors.1990.1080p.WEB.x264-DEAD", seeders=0, size=1400 * _MB)
    alive = _cand("Tremors.1990.1080p.BluRay.x264-LIVE", seeders=12,
                  size=1400 * _MB)
    decision = select_torrent([dead, alive],
                              _movie_want("Tremors", 1990, min_seeders=1))
    assert decision.chosen_title == alive.title
    reasons = {v.title: v.reason_code for v in decision.verdicts}
    assert reasons[dead.title] == "insufficient_seeders"
    assert reasons[alive.title] == "ok"


# ---------------------------------------------------------------------------
# Determinism: shuffled candidate order -> identical decision.
# ---------------------------------------------------------------------------

def test_determinism_under_shuffle():
    want = _tv_want("Severance", season=2, pref=22.0)
    base = [
        _cand("Severance.S02E01.1080p.WEB.h264-ETHEL", seeders=100, size=1200 * _MB),
        _cand("Severance.S02.COMPLETE.1080p.WEB.x264-PACK", seeders=80,
              size=6 * _GB),
        _cand("Severance.S02E01.720p.WEB.h264-LOW", seeders=100, size=600 * _MB),
        _cand("Severance.S03E01.1080p.WEB.h264-WRONG", seeders=999, size=1200 * _MB),
    ]
    reference = select_torrent(list(base), want)
    for seed in range(8):
        shuffled = list(base)
        random.Random(seed).shuffle(shuffled)
        d = select_torrent(shuffled, want)
        assert d.chosen_infohash == reference.chosen_infohash
        assert d.chosen_title == reference.chosen_title
        # Full scored order is stable too.
        assert [s.infohash for s in d.scores] == \
            [s.infohash for s in reference.scores]


def test_season_pack_beats_single_episode_on_whole_season_want():
    want = _tv_want("Severance", season=2, pref=22.0, runtime_minutes=None)
    pack = _cand("Severance.S02.COMPLETE.1080p.WEB.x264-PACK", seeders=80,
                 size=6 * _GB)
    ep = _cand("Severance.S02E01.1080p.WEB.h264-ETHEL", seeders=100,
               size=1200 * _MB)
    d = select_torrent([pack, ep], want)
    # Both pass the gate; the pack gets +15 and wins.
    assert d.chosen_title == pack.title
    top = d.scores[0]
    assert top.components["season_pack"] == pytest.approx(15.0)


def test_wrong_season_rejected_but_episode_for_season_passes():
    want = _tv_want("Severance", season=2)
    wrong = _cand("Severance.S03E01.1080p.WEB.h264-WRONG")
    right = _cand("Severance.S02E01.1080p.WEB.h264-ETHEL")
    d = select_torrent([wrong, right], want)
    reasons = {v.title: v.reason_code for v in d.verdicts}
    assert reasons[wrong.title] == "season_contradiction"
    assert reasons[right.title] == "ok"


# ---------------------------------------------------------------------------
# Absence vs contradiction: unmarked country / missing year PASS and score down;
# contradictions REJECT.
# ---------------------------------------------------------------------------

def test_absence_of_country_passes_and_scores_below_positive_match():
    want = _tv_want("Married at First Sight", season=18, countries=["US"])
    unmarked = _cand("Married.at.First.Sight.S18E05.1080p.WEB.h264-NOCOUNTRY",
                     seeders=50, size=1400 * _MB)
    marked = _cand("Married.at.First.Sight.US.S18E05.1080p.WEB.h264-US",
                   seeders=50, size=1400 * _MB)
    d = select_torrent([unmarked, marked], want)
    # Both pass the gates (absence never rejects); the US-marked one scores the
    # +20 country bonus and wins.
    assert {v.reason_code for v in d.verdicts} == {"ok"}
    assert d.chosen_title == marked.title
    scored = {s.title: s for s in d.scores}
    assert scored[marked.title].components["country_alias"] == pytest.approx(20.0)
    assert scored[unmarked.title].components["country_alias"] == pytest.approx(0.0)


def test_missing_year_passes_and_scores_down_contradiction_rejects():
    want = _movie_want("Tremors", 1990)
    no_year = _cand("Tremors.1080p.BluRay.x265-NOYEAR", seeders=50, size=1500 * _MB)
    wrong_year = _cand("Tremors.2018.1080p.WEB.x264-WRONGYEAR", seeders=999,
                       size=1500 * _MB)
    d = select_torrent([no_year, wrong_year], want)
    reasons = {v.title: v.reason_code for v in d.verdicts}
    # Missing parsed year is allowed through; a contradicting year is rejected.
    assert reasons[no_year.title] == "ok"
    assert reasons[wrong_year.title] == "year_mismatch"
    assert d.chosen_title == no_year.title


# ---------------------------------------------------------------------------
# RTN parser failure on ONE candidate rejects it visibly without crashing.
# ---------------------------------------------------------------------------

def test_unparseable_candidate_rejected_visibly_without_crash():
    want = _movie_want("Tremors", 1990)
    good = _cand("Tremors.1990.1080p.BluRay.x265-RARBG", size=1500 * _MB)
    junk = _cand("...", size=1500 * _MB, infohash="3" * 40)          # empty parse
    empty = _cand("   ", size=1500 * _MB, infohash="4" * 40)         # whitespace
    d = select_torrent([junk, good, empty], want)
    # The pass survives; the good one is chosen; the junk ones are visible.
    assert d.chosen_title == good.title
    reasons = {v.title: v.reason_code for v in d.verdicts}
    assert reasons["..."] == "unparseable"
    assert reasons["   "] == "unparseable"


def test_zero_size_and_oversize_rejected():
    want = _movie_want("Tremors", 1990, pref=10.0)
    want = SelectWant(identity=want.identity, size_pref_mb_min=10.0,
                      size_max_rate=20.0, fallback_minutes=120.0)  # cap 2.4GB/2h
    zero = _cand("Tremors.1990.1080p.BluRay.x265-ZERO", size=0, infohash="5" * 40)
    huge = _cand("Tremors.1990.2160p.BluRay.x265-HUGE", size=10 * _GB)
    ok = _cand("Tremors.1990.1080p.BluRay.x265-OK", size=1500 * _MB)
    d = select_torrent([zero, huge, ok], want)
    reasons = {v.title: v.reason_code for v in d.verdicts}
    assert reasons[zero.title] == "zero_size"
    assert reasons[huge.title] == "oversize"
    assert d.chosen_title == ok.title


def test_recent_failure_penalty_applied_but_not_a_gate():
    want = _movie_want("Tremors", 1990)
    failed = _cand("Tremors.1990.1080p.BluRay.x265-FAILED", seeders=50,
                   size=1500 * _MB, infohash="a" * 40)
    fresh = _cand("Tremors.1990.1080p.WEB-DL.x264-FRESH", seeders=50,
                  size=1500 * _MB, infohash="b" * 40)
    d = select_torrent([failed, fresh], want, recent_failures={"a" * 40})
    # The failed release still passes the gate (penalty, not veto) but scores
    # -25 and loses.
    reasons = {v.title: v.reason_code for v in d.verdicts}
    assert reasons[failed.title] == "ok"
    scored = {s.title: s for s in d.scores}
    assert scored[failed.title].components["recent_failure"] == pytest.approx(-25.0)
    assert d.chosen_title == fresh.title


def test_episode_marker_on_movie_want_rejected():
    want = _movie_want("Some Show", 2020)
    cand = _cand("Some.Show.S01E03.1080p.WEB.h264-GRP", size=1400 * _MB)
    d = select_torrent([cand], want)
    assert d.verdicts[0].reason_code == "episode_marker_on_movie"


def test_manual_pick_still_runs_gates_as_preflight():
    want = SelectWant(
        identity=MediaIdentity(media_type="movie", identity_source="tmdb",
                               external_id="1",
                               canonical_title="The Angry Birds Movie",
                               canonical_year=2016),
        size_pref_mb_min=10.0, fallback_minutes=120.0,
        mode=ts.MODE_MANUAL_USER_PICK)
    sequel = _cand("The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GROUP")
    d = select_torrent([sequel], want)
    assert d.mode == ts.MODE_MANUAL_USER_PICK
    # An identity mismatch is SURFACED even on a manual pick (the caller decides
    # whether to record a typed override); it is not silently accepted.
    assert d.chosen is False
    assert d.verdicts[0].reason_code == "sequel_mismatch"


def test_no_candidates_and_all_rejected_reasons():
    want = _movie_want("Tremors", 1990)
    assert select_torrent([], want).reason == "no candidates"
    only_cam = _cand("Longlegs.2024.CAM.x264-YIFY", size=1200 * _MB)
    d = select_torrent([only_cam], want)
    assert d.chosen is False and d.reason == "all candidates rejected"


# ---------------------------------------------------------------------------
# Pool regression: a correct low-seed release at combined-seeder-rank 25 is
# chosen over a wrong high-seed one — through search_collect + select_torrent
# with MOCKED sources (no network).
# ---------------------------------------------------------------------------

def _fake_tpb_pool():
    """24 wrong high-seed decoys (a sequel) + the correct low-seed release at
    combined-seeder rank 25 (last, fewest seeders)."""
    from torrent_search import TorrentResult, _magnet_from_hash
    import hashlib
    rows = []
    for i in range(24):
        name = f"Tremors.II.Aftershocks.1996.1080p.BluRay.x264-DECOY{i:02d}"
        h = hashlib.sha1(name.encode()).hexdigest()
        rows.append(TorrentResult(
            title=name, magnet=_magnet_from_hash(h, name),
            size_bytes=1600 * _MB, seeders=900 - i, source="tpb", media_type="tv"))
    correct = "Tremors.1990.1080p.BluRay.x265-RARBG"
    ch = hashlib.sha1(correct.encode()).hexdigest()
    rows.append(TorrentResult(
        title=correct, magnet=_magnet_from_hash(ch, correct),
        size_bytes=1500 * _MB, seeders=5, source="tpb", media_type="tv"))
    return rows


def test_pool_regression_low_seed_correct_beats_high_seed_wrong(monkeypatch):
    monkeypatch.setattr(torrent_search, "search_tpb",
                        lambda q, mt, *, limit=30, collect=False: _fake_tpb_pool())
    pool = torrent_search.search_collect("Tremors 1990", "tv", per_source=50)
    # No global seeder truncation — the correct rank-25 release survived.
    assert len(pool.results) == 25
    assert pool.pool_stats["per_source"]["tpb"] == 25

    want = _movie_want("Tremors", 1990)
    cands = [ts.to_candidate(r) for r in pool.results]
    decision = select_torrent(cands, want, pool_stats=pool.pool_stats)
    # Every decoy is a sequel_mismatch; the correct low-seed original wins.
    assert decision.chosen_title == "Tremors.1990.1080p.BluRay.x265-RARBG"
    assert decision.pool_stats["per_source"]["tpb"] == 25


def test_search_collect_dedupes_by_infohash(monkeypatch):
    from torrent_search import TorrentResult, _magnet_from_hash
    dup = "0123456789abcdef0123456789abcdef01234567"
    a = TorrentResult(title="Show.S01.1080p-A", magnet=_magnet_from_hash(dup, "a"),
                      size_bytes=_GB, seeders=10, source="tpb", media_type="tv")
    b = TorrentResult(title="Show.S01.1080p-B", magnet=_magnet_from_hash(dup, "b"),
                      size_bytes=_GB, seeders=90, source="tpb", media_type="tv")
    monkeypatch.setattr(torrent_search, "search_tpb",
                        lambda q, mt, *, limit=30, collect=False: [a, b])
    pool = torrent_search.search_collect("show", "tv")
    assert len(pool.results) == 1
    # The higher-seed copy of the identical payload is the survivor.
    assert pool.results[0].seeders == 90
    assert pool.pool_stats["duplicates_removed"] == 1


def test_search_collect_no_global_truncation_preserves_pool():
    # search_collect must not seeder-sort-then-cut like search_torrents does.
    from torrent_search import TorrentResult, _magnet_from_hash
    import hashlib
    rows = []
    for i in range(45):
        name = f"Show.S01E{i:02d}.1080p"
        h = hashlib.sha1(name.encode()).hexdigest()
        rows.append(TorrentResult(title=name, magnet=_magnet_from_hash(h, name),
                                  size_bytes=_GB, seeders=i, source="tpb",
                                  media_type="tv"))
    import torrent_search as tsr
    orig = tsr.search_tpb
    try:
        tsr.search_tpb = lambda q, mt, *, limit=30, collect=False: rows[:limit]
        pool = tsr.search_collect("show", "tv", per_source=50)
        assert len(pool.results) == 45  # nothing dropped by seeder rank
    finally:
        tsr.search_tpb = orig
