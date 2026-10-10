# =============================================================================
# Anime: an English audio track (dual audio / English dub) is the top language
# preference, ahead of English subtitles (Cole, 2026-10-09). Release names spell
# it a dozen ways; the selector has to read all of them and none of the
# look-alikes (French "MULTi", "Hindi Dub", "Multi-Subs").
# =============================================================================

import hashlib

import pytest

import rtn_compat
import torrent_select as ts
from media_identity import MediaIdentity

MB = 1024 * 1024


def _cand(title, *, size_mb=700, seeders=50, cached=None, hints=()):
    ih = hashlib.sha1(title.encode()).hexdigest()
    return ts.Candidate(title=title, infohash=ih, size_bytes=size_mb * MB,
                        seeders=seeders, source="nyaa", cached=cached,
                        hints=tuple(hints))


def _want(media_type="anime", title="Mashle", **kw):
    return ts.SelectWant(
        identity=MediaIdentity(media_type=media_type, canonical_title=title,
                               season=1, episode=5),
        **kw)


@pytest.mark.parametrize("title", [
    "[Yameii] Mashle - S01E05 [English Dub] [CR WEB-DL 720p]",
    "[Judas] Mashle - S01E05 [1080p][HEVC x265 10bit][Dual-Audio][Multi-Subs]",
    "Mashle S01E05 1080p BluRay Dual Audio x265-ASW",
    "Mashle S01E05 [BD 1080p HEVC] [JPN+ENG] [Multi-Sub]",
    "Mashle S01E05 1080p CR WEB-DL AAC2.0 H 264 DUAL-VARYG",
    "Mashle S01E05 1080p WEB H264 -DUB",
    "Mashle S01E05 1080p English Audio",
    "Mashle S01E05 1080p WEB-DL Multi-Audio",
    "Mashle S01E05 Eng Dubbed 720p",
    "Mashle S01E05 Jap/Eng 720p",
    "Mashle S01E05 Japanese + English BDRip",
])
def test_english_audio_spellings_are_recognised(title):
    parsed, _ = rtn_compat.parse_release(title)
    assert ts.has_english_audio_marker(title, parsed)


@pytest.mark.parametrize("title", [
    "[Erai-raws] Mashle - 05 [1080p][Multiple Subtitle]",
    "[SubsPlease] Mashle - 05 (1080p) [ABCD1234].mkv",
    "Mashle - 05 [1080p] [Eng Sub]",
    "Mashle S01E05 MULTi 1080p WEB x264-AMB3R",
    "Mashle 05 [Hindi Dub] 720p",
    "Mashle 05 German Dub 480p",
    "Mashle S01E05 Latino Castellano 1080p",
    "Mashle S01E05 VOSTFR 1080p",
    # Verifier finding 2026-10-09: subtitle-only "dual" tags.
    "[SubsPlease] Mashle - 05 (1080p) [Dual Subs]",
    "Mashle S01E05 1080p WEB Dual-Sub",
    "Mashle S01E05 1080p Dual Sub ENG CHI",
    "Mashle S01E05 1080p [JPN+ENG Subs]",
])
def test_subs_and_foreign_dubs_are_not_english_audio(title):
    parsed, _ = rtn_compat.parse_release(title)
    assert not ts.has_english_audio_marker(title, parsed)


def test_torrentio_audio_tags_count_but_french_multi_does_not():
    gb, fr, jp = "\U0001F1EC\U0001F1E7", "\U0001F1EB\U0001F1F7", "\U0001F1EF\U0001F1F5"
    assert ts.has_english_audio_marker("Mashle S01E05", None, ("Dual Audio",))
    assert ts.has_english_audio_marker("Mashle S01E05", None, ("Multi Audio", gb, jp))
    assert not ts.has_english_audio_marker("Mashle S01E05", None, ("Multi Audio", fr, jp))


def test_dual_audio_beats_a_better_seeded_subs_only_release():
    subs = _cand("[SubsPlease] Mashle - S01E05 (1080p) [ABCD1234].mkv",
                 seeders=900)
    dual = _cand("[Judas] Mashle - S01E05 [1080p][Dual-Audio][Multi-Subs]",
                 seeders=12)
    decision = ts.select_torrent([subs, dual], _want())
    assert decision.chosen_title == dual.title
    comps = {s.title: s.components for s in decision.scores}
    assert comps[dual.title]["language_pref"] == 75.0
    assert comps[subs.title]["language_pref"] == 0.0


def test_dual_audio_720p_beats_english_subbed_1080p():
    subbed = _cand("Mashle S01E05 1080p WEB-DL [Eng Sub]", seeders=300)
    dubbed = _cand("Mashle S01E05 720p WEB-DL [English Dub]", seeders=40,
                   size_mb=400)
    decision = ts.select_torrent([subbed, dubbed], _want())
    assert decision.chosen_title == dubbed.title


def test_dual_audio_is_a_bonus_for_anime_only():
    # For TV/movies English must be the primary track, so dual audio is the
    # "English is not primary" penalty there, never the anime bonus.
    parsed, _ = rtn_compat.parse_release("Show S01E01 1080p Dual Audio")
    assert ts.language_score("Show S01E01 1080p Dual Audio", parsed, "tv") == -50.0


def test_cached_release_skips_the_seeder_floor_and_scores_a_bonus():
    dead = _cand("[Judas] Mashle - S01E05 [1080p][Dual-Audio]", seeders=0,
                 cached=True)
    decision = ts.select_torrent([dead], _want(min_seeders=4))
    assert decision.chosen_title == dead.title
    assert decision.scores[0].components["debrid_cached"] == 30.0

    uncached = _cand("[Judas] Mashle - S01E05 [1080p][Dual-Audio]", seeders=0)
    decision = ts.select_torrent([uncached], _want(min_seeders=4))
    assert decision.chosen_title is None
    assert decision.verdicts[0].reason_code == "insufficient_seeders"
