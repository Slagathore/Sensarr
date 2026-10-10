# =============================================================================
# tests/test_search_quality.py
# =============================================================================
# The search/selection quality rules Cole asked for after a week of picking
# torrents by hand:
#
#   1. a parser that cannot parse must say so (parse_error), never blame the
#      release ("unparseable") — the 1.6.0 EXE shipped a parser with no data
#      files and silently rejected every candidate in existence
#   2. punctuation must not decide a search: C.H.U.D. / What's New Scooby-Doo?
#   3. an indexer's trending-list answer to a query it could not match must be
#      thrown away, not gated one result at a time
#   4. no 4K unless 4K is all there is
#   5. movies: a foreign-language tag is a heavy non-preference, cancelled by
#      an English marker. Anime: an English sub/dub marker is a strong plus
#   6. a movie that is not out yet is not searched for
#   7. punctuation must not decide a library presence check either
# =============================================================================

import datetime as dt

import pytest

import library_index
import rtn_compat
import torrent_search as tsr
import torrent_select as ts
from media_identity import MediaIdentity

_MB = 1024 * 1024
_GB = 1024 * _MB


def _candidate(title, *, seeders=50, size=2 * _GB, ih=None):
    import hashlib
    ih = ih or hashlib.sha1(title.encode()).hexdigest()
    return ts.Candidate(title=title, infohash=ih, size_bytes=size, seeders=seeders)


def _want(media_type, title, year=None, **kw):
    return ts.SelectWant(
        identity=MediaIdentity(media_type=media_type, canonical_title=title,
                               canonical_year=year),
        **kw)


def _components(title, media_type, want_title, year=None):
    decision = ts.select_torrent([_candidate(title)],
                                 _want(media_type, want_title, year))
    assert decision.scores, decision.verdicts
    return decision.scores[0].components


# ---------------------------------------------------------------------------
# 1. parser integrity
# ---------------------------------------------------------------------------

def test_parser_is_healthy_in_this_environment():
    status = rtn_compat.parser_status()
    assert status["healthy"], status


def test_missing_keyword_data_is_survived_not_silently_fatal(monkeypatch):
    """PTT reads adult keywords off disk on EVERY parse. A build without those
    files made every release unparseable; the shim must recover instead."""
    import PTT.adult as ptt_adult

    def boom(filename="combined-keywords.txt"):
        raise FileNotFoundError("bundled keyword data missing")

    monkeypatch.setattr(ptt_adult, "load_adult_keywords", boom)
    monkeypatch.setattr(rtn_compat, "_STATUS", None)
    monkeypatch.setattr(rtn_compat, "_DETAIL", "")

    assert rtn_compat.ensure_parser(force=True) == rtn_compat.STATUS_DEGRADED
    parsed, reason = rtn_compat.parse_release("Soul (2020) [720p] [BluRay]")
    assert reason == ""
    assert parsed.parsed_title == "Soul"


def test_parser_failure_is_reported_as_parse_error_not_unparseable(monkeypatch):
    monkeypatch.setattr(rtn_compat, "parse_release",
                        lambda title: (None, "parse_error"))
    decision = ts.select_torrent(
        [_candidate("Gullivers.Travels.2010.1080p.BluRay.x265")],
        _want("movie", "Gullivers Travels", 2010))
    verdict = decision.verdicts[0]
    assert verdict.reason_code == "parse_error"
    assert "parser" in verdict.detail


@pytest.mark.parametrize("title", [
    "Gullivers.Travels.2010.1080p.BluRay.x265",
    "Angel.Has.Fallen.2019.720p.WEBRip.800MB.x264-GalaxyRG",
    "Kitchen Nightmares US S10E01 1080p WEB h264-EDITH",
    "Percy Jackson And The Olympians: The Lightning Thief (2010) 1080",
    "The Complete 1980's Strawberry Shortcake TV Series",
    "Soul (2020) [720p] [BluRay]",
])
def test_real_picks_parse(title):
    """Every one of these was rejected as 'unparseable' by the shipped 1.6.0."""
    parsed, reason = rtn_compat.parse_release(title)
    assert reason == "", f"{title!r} -> {reason}"
    assert parsed.parsed_title


# ---------------------------------------------------------------------------
# 2. query spellings
# ---------------------------------------------------------------------------

def test_dotted_acronym_collapses():
    assert tsr.collapse_acronyms("C.H.U.D. (1984)") == "CHUD (1984)"
    assert tsr.collapse_acronyms("S.W.A.T. S01") == "SWAT S01"
    # A single initial is not an acronym.
    assert tsr.collapse_acronyms("John Q. Public") == "John Q. Public"


def test_grammar_is_stripped_but_words_are_not():
    assert tsr.strip_grammar("What's New Scooby-Doo?") == "Whats New Scooby Doo"
    assert tsr.strip_grammar("Gulliver's Travels (2010)") == "Gullivers Travels 2010"


def test_native_script_survives_grammar_stripping():
    assert tsr.strip_grammar("逐玉 S01") == "逐玉 S01"


def test_variants_ladder_order():
    variants = tsr.query_variants("What's New Scooby-Doo? S01")
    assert variants[0] == "What's New Scooby-Doo? S01"
    assert "Whats New Scooby Doo S01" in variants
    # the distinctive word keeps the season marker
    assert "Scooby S01" in variants


def test_distinctive_word_skips_stopwords_and_markers():
    assert tsr.distinctive_word("What's New Scooby-Doo? S01") == "Scooby"
    assert tsr.distinctive_word("The Box 2009") == ""   # nothing long enough


def test_translation_variant_for_native_script(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "translate_title_to_english",
                        lambda title: "Pursuit of Jade")
    variants = tsr.query_variants("逐玉 S01")
    assert variants[0] == "逐玉 S01"
    assert "Pursuit of Jade S01" in variants


def test_translation_failure_costs_nothing(monkeypatch):
    import llm_service
    monkeypatch.setattr(llm_service, "translate_title_to_english",
                        lambda title: None)
    assert tsr.query_variants("逐玉") == ("逐玉",)


# ---------------------------------------------------------------------------
# 3. relevance — the trending-list defence
# ---------------------------------------------------------------------------

def test_trending_list_answer_is_not_treated_as_results():
    junk = "Spider-Man: Brand New Day 2026.1080p.HQ Pre.Multi.AAC 2.0.x264"
    assert not tsr.is_relevant(junk, "C.H.U.D.")
    assert tsr.is_relevant("CHUD 1984 1080p BluRay x264-OFT", "C.H.U.D.")
    assert tsr.is_relevant("C.H.U.D. II Bud The Chud (1989) 720p", "CHUD")


def test_search_climbs_the_ladder_when_the_first_spelling_is_junk(monkeypatch):
    """The faithful spelling gets the trending list; the collapsed one works."""
    trending = [
        tsr.TorrentResult(title=f"Blockbuster {i} 2026 1080p WEBRip",
                          magnet=tsr._magnet_from_hash(f"{i:040d}", "x"),
                          size_bytes=_GB, seeders=900, source="tpb",
                          media_type="movie")
        for i in range(100)
    ]
    real = [
        tsr.TorrentResult(title="CHUD 1984 1080p BluRay x264-OFT",
                          magnet=tsr._magnet_from_hash("a" * 40, "chud"),
                          size_bytes=_GB, seeders=5, source="tpb",
                          media_type="movie"),
        tsr.TorrentResult(title="C.H.U.D. (1984) 720p BluRay-LAMA",
                          magnet=tsr._magnet_from_hash("b" * 40, "chud2"),
                          size_bytes=_GB, seeders=2, source="tpb",
                          media_type="movie"),
    ]

    def fake_tpb(query, media_type, *, limit=30, collect=False):
        return real if query == "CHUD" else trending

    monkeypatch.setattr(tsr, "search_tpb", fake_tpb)
    monkeypatch.setattr(tsr, "search_yts", lambda q, *, limit=20: [])

    pool = tsr.search_collect("C.H.U.D.", "movie")
    titles = [r.title for r in pool.results]
    assert titles and all("Blockbuster" not in t for t in titles)
    assert pool.pool_stats["query_used"] == "CHUD"


def test_relevant_but_thin_pool_keeps_climbing(monkeypatch):
    """One dead 0-seeder hit is not 'download worthy' — try the next spelling."""
    thin = [tsr.TorrentResult(
        title="Whats.New.Scooby.Doo.S01.SWEDISH.DVDRip.XviD-aka",
        magnet=tsr._magnet_from_hash("c" * 40, "x"), size_bytes=_GB,
        seeders=0, source="tpb", media_type="tv")]
    wide = [
        tsr.TorrentResult(
            title=f"Whats New Scooby Doo S01 1080p WEB-DL {i}",
            magnet=tsr._magnet_from_hash(f"{i:040x}", "y"), size_bytes=_GB,
            seeders=20 + i, source="tpb", media_type="tv")
        for i in range(4)
    ]

    def fake_tpb(query, media_type, *, limit=30, collect=False):
        if query == "Scooby S01":
            return wide
        if query == "Whats New Scooby Doo S01":
            return thin
        return []

    monkeypatch.setattr(tsr, "search_tpb", fake_tpb)
    pool = tsr.search_collect("What's New Scooby-Doo? S01", "tv")
    assert len(pool.results) >= 4
    assert "Scooby S01" in pool.pool_stats["query_variants_tried"]


def test_tpb_category_targets_match_the_media_type():
    assert tsr.tpb_categories("movie") == ("201", "207", "202")
    assert tsr.tpb_categories("tv") == ("205", "208")
    assert tsr.tpb_categories("other") == ("200",)


def test_tpb_asks_for_its_categories(monkeypatch):
    calls: list[str] = []

    def fake_request(query, cat, media_type):
        calls.append(cat)
        return [tsr.TorrentResult(
            title="anything 2024 1080p",
            magnet=tsr._magnet_from_hash("e" * 40, "x"), size_bytes=_GB,
            seeders=1, source="tpb", media_type="movie")]

    monkeypatch.setattr(tsr, "_tpb_request", fake_request)
    tsr.search_tpb("anything", "movie")
    assert calls == ["201,207,202"]


# ---------------------------------------------------------------------------
# 4 + 5. resolution and language preferences
# ---------------------------------------------------------------------------

def test_4k_loses_to_1080p_but_still_wins_alone():
    uhd = _candidate("Some.Movie.2024.2160p.UHD.BluRay.x265-GRP")
    hd = _candidate("Some.Movie.2024.1080p.BluRay.x264-GRP")
    want = _want("movie", "Some Movie", 2024)

    both = ts.select_torrent([uhd, hd], want)
    assert both.chosen_title == hd.title

    alone = ts.select_torrent([uhd], want)
    assert alone.chosen_title == uhd.title


def test_foreign_language_movie_is_a_heavy_non_preference():
    assert _components("Some.Movie.2024.FRENCH.1080p.WEBRip", "movie",
                       "Some Movie", 2024)["language_pref"] < 0


def test_english_alongside_other_languages_is_still_penalised():
    # Superseded 2026-10-10 (Cole: English as the PRIMARY language is
    # paramount): an ENG tag no longer cancels the penalty, it only softens it
    # from "no English" to "English is not the main track".
    assert _components("Some.Movie.2024.MULTi.ENG.FRE.1080p", "movie",
                       "Some Movie", 2024)["language_pref"] == -80.0
    assert _components("Some.Movie.2024.FRENCH.1080p", "movie",
                       "Some Movie", 2024)["language_pref"] == -120.0
    assert _components("Some.Movie.2024.1080p.WEB-DL", "movie",
                       "Some Movie", 2024)["language_pref"] == 0


def test_english_release_beats_the_dubbed_one():
    dub = _candidate("Valiant.2005.1080p.BDRip.Dublado")
    eng = _candidate("Valiant.2005.1080p.BDRip.x264")
    decision = ts.select_torrent([dub, eng], _want("movie", "Valiant", 2005))
    assert decision.chosen_title == eng.title


def test_anime_english_marker_is_a_strong_preference():
    subbed = _components("[SubsPlease] Dandadan - 01 (1080p) [Eng Subs]",
                         "anime", "Dandadan")
    raw = _components("[Raws] Dandadan - 01 (1080p)", "anime", "Dandadan")
    assert subbed["language_pref"] > 0
    assert raw["language_pref"] == 0


def test_dual_audio_counts_as_english_for_anime():
    assert ts.has_english_marker(
        "[EMBER] Mushoku Tensei (2024) [1080p Dual Audio HEVC 10 bits DDP]")


def test_foreign_language_tv_is_a_heavy_non_preference():
    assert _components("Show.S01E02.SWEDISH.DVDRip.XviD", "tv",
                       "Show")["language_pref"] < 0


def test_tv_multi_language_with_english_is_still_penalised():
    assert _components("Show.S01E02.MULTi.ENG.FRE.1080p.WEB", "tv",
                       "Show")["language_pref"] == -80.0


def test_english_tv_episode_beats_the_spanish_one():
    # The 2026-09-12 grab: a Spanish-tagged Futurama episode was taken over
    # plain English releases because TV skipped the language score.
    spanish = _candidate("Futurama s06e05 HDTV spanish", seeders=80)
    english = _candidate("Futurama.S06E05.HDTV.XviD-FQM", seeders=40)
    decision = ts.select_torrent([spanish, english],
                                 _want("tv", "Futurama"))
    assert decision.chosen_title == english.title


# ---------------------------------------------------------------------------
# 6. unreleased movies are not searched for
# ---------------------------------------------------------------------------

def _request(media_type="movie", release_date=None):
    import queue_store
    return queue_store.QueueRequest(
        request_id=1, requester="cole", content="a film", status="open",
        created_at="", completed_at=None, media_type=media_type,
        resolved_title="A Film", external_id="1", external_url="",
        found_in_library=False, library_checked_at=None,
        release_date=release_date)


def test_movie_months_out_is_held():
    import download_manager as dm
    hours = dm.prerelease_hold_hours(
        _request(release_date="2026-12-25"), today=dt.date(2026, 9, 3),
        lead_days=30)
    assert hours / 24 == pytest.approx(83)


def test_movie_inside_the_lead_window_is_searched():
    import download_manager as dm
    assert dm.prerelease_hold_hours(
        _request(release_date="2026-09-20"), today=dt.date(2026, 9, 3),
        lead_days=30) == 0.0


@pytest.mark.parametrize("media_type,release_date", [
    ("movie", None),                 # unknown date never holds
    ("movie", "2024-01-01"),         # already out
    ("tv", "2027-01-01"),            # shows are paced per episode instead
])
def test_no_hold_cases(media_type, release_date):
    import download_manager as dm
    assert dm.prerelease_hold_hours(
        _request(media_type, release_date), today=dt.date(2026, 9, 3),
        lead_days=30) == 0.0


def test_release_date_reaches_the_request_confirmation():
    import request_flow
    from media_lookup import MediaResult
    match = MediaResult(
        title="A Film", year=2026, external_id="1", external_url="",
        media_type="movie", overview="", source="tmdb",
        release_date="2026-12-25")
    note = request_flow._release_note(match)
    assert "Dec 25, 2026" in note


# ---------------------------------------------------------------------------
# 7. punctuation-blind library presence
# ---------------------------------------------------------------------------

def test_compact_name_ignores_separators():
    assert (library_index.compact_name("Spider-Man- Brand New Day 2026.mkv")
            == library_index.compact_name("spiderman brand new day 2026mkv"))


def test_presence_check_survives_a_missing_hyphen(tmp_path, monkeypatch):
    import db
    import sqlite3

    db_file = tmp_path / "index.db"
    monkeypatch.setattr(db, "db_path", lambda: db_file)
    monkeypatch.setattr(library_index, "_db_path", lambda: db_file)
    library_index.initialize_library_index_db()
    name = "Spider-Man- Brand New Day 2026.1080p.mkv"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "INSERT INTO library_files (path, name, root_path, search_name,"
            " size_bytes, modified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (f"D:/movies/{name}", name, "D:/movies", name.casefold(),
             _GB, 0.0))
        conn.commit()

    # The exact spelling still works...
    assert library_index.search_library("spider-man- brand new day",
                                        local_only=True)
    # ...and so does the one a human would type.
    found = library_index.search_library("spiderman brand new day",
                                         local_only=True)
    assert [entry.name for entry in found] == [name]


def test_category_search_falls_back_to_the_whole_video_tree(monkeypatch):
    """Uploaders miscategorise; narrowing must never lose a findable release."""
    calls: list[str] = []
    hit = tsr.TorrentResult(
        title="Odd.Upload.2024.1080p", magnet=tsr._magnet_from_hash("d" * 40, "x"),
        size_bytes=_GB, seeders=3, source="tpb", media_type="movie")

    def fake_request(query, cat, media_type):
        calls.append(cat)
        return [hit] if cat == "200" else []

    monkeypatch.setattr(tsr, "_tpb_request", fake_request)
    results = tsr.search_tpb("odd upload", "movie")
    assert calls == ["201,207,202", "200"]
    assert results == [hit]


# ---------------------------------------------------------------------------
# 6b. the hold, through the real auto-grab pass
# ---------------------------------------------------------------------------

def test_unreleased_movie_is_held_by_its_own_key_and_out_films_still_grab(
        monkeypatch, tmp_path):
    """Two open movie requests, one unreleased. The unreleased one is held
    under ITS OWN deferral key and the released one still grabs — the loop
    must never defer the request it is not looking at."""
    import db as _db
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(_db, "db_path", lambda: tmp_path / "t.db")

    import downloads_store
    import queue_store
    import request_intake
    import shows_store
    from download_manager import DownloadManager
    from media_lookup import MediaResult
    from torrent_search import CollectedPool, TorrentResult

    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    shows_store.initialize_shows_db()

    def _match(title, ext, release_date):
        return MediaResult(
            title=title, year=2026, external_id=ext,
            external_url=f"https://www.themoviedb.org/movie/{ext}",
            media_type="movie", overview="", source="tmdb",
            release_date=release_date)

    far = request_intake.add_matched_request(
        "Far Off Film", "cole", media_type="movie",
        match=_match("Far Off Film", "1", "2099-12-25"))
    out = request_intake.add_matched_request(
        "Old Film", "cole", media_type="movie",
        match=_match("Old Film", "2", "2001-01-01"))

    winner = TorrentResult(
        title="Old Film 2026 1080p BluRay x264-GRP",
        magnet="magnet:?xt=urn:btih:" + "f" * 40 + "&dn=x",
        size_bytes=1400 * _MB, seeders=40, source="tpb", media_type="movie")

    manager = DownloadManager()
    monkeypatch.setattr("download_manager.search_collect",
                        lambda *a, **k: CollectedPool(
                            results=(winner,),
                            pool_stats={"per_source": {"tpb": 1}}))
    monkeypatch.setattr("download_manager._request_movie_minutes",
                        lambda *a, **k: None)
    monkeypatch.setattr("download_manager._maybe_start_next", lambda: None,
                        raising=False)
    monkeypatch.setattr(manager, "_run_download", lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(manager, "_maybe_start_next", lambda: None,
                        raising=False)

    manager.auto_grab_open_requests()

    held = downloads_store.get_grab_deferral(f"req:{far.request_id}")
    assert held is not None and "not released yet" in (held["reason"] or "")
    # The film that IS out must not have been deferred by its neighbour.
    assert downloads_store.get_grab_deferral(f"req:{out.request_id}") is None


# ---------------------------------------------------------------------------
# 2026-10-10: English primary for non-anime; seeders count for more
# ---------------------------------------------------------------------------

def test_plain_english_copy_beats_the_indian_multi_language_one():
    """The Love Hypothesis case: a 60-seed '[Tel + Tam + Hin + Mal + Kan +
    Eng]' WEB-DL used to beat a 752-seed English copy."""
    multi = _candidate("The Love Hypothesis (2026) 1080p TRUE WEB-DL - AVC - "
                       "[Tel + Tam + Hin + Mal + Kan + Eng] - 5.1", seeders=60)
    english = _candidate("The Love Hypothesis 2026 1080p WEB-DL HEVC x265 5.1 BONE",
                         seeders=752)
    decision = ts.select_torrent([multi, english],
                                 _want("movie", "The Love Hypothesis", 2026))
    assert decision.chosen_title == english.title


def test_torrentio_language_flags_count():
    gb, it = "\U0001F1EC\U0001F1E7", "\U0001F1EE\U0001F1F9"
    parsed, _ = rtn_compat.parse_release("Show S01E01 1080p WEB")
    assert ts.language_score("Show S01E01 1080p WEB", parsed, "tv", hints=(it,)) == -120.0
    assert ts.language_score("Show S01E01 1080p WEB", parsed, "tv", hints=(gb, it)) == -80.0
    assert ts.language_score("Show S01E01 1080p WEB", parsed, "tv", hints=(gb,)) == 0.0


def test_a_foreign_original_is_not_penalised_for_its_own_language():
    """Bloodhounds (KR): a Korean-tagged release is the original, not a dub."""
    parsed, _ = rtn_compat.parse_release("Bloodhounds S01E01 KOREAN 1080p WEB")
    assert ts.language_score("Bloodhounds S01E01 KOREAN 1080p WEB", parsed, "tv",
                             origin_countries=("KR",)) == 0.0
    assert ts.language_score("Bloodhounds S01E01 KOREAN 1080p WEB", parsed, "tv",
                             origin_countries=("US",)) == -120.0


def test_seeders_now_weigh_about_as_much_as_size():
    few = _components("Some.Movie.2024.1080p.WEB-DL", "movie", "Some Movie", 2024)
    assert few["seed_health"] > 8  # the old cap
    import torrent_select
    assert torrent_select._SEED_HEALTH_MAX == 35.0


def test_english_subtitles_do_not_make_a_foreign_release_english():
    """Verifier 2026-10-10: ENG.SUBS / Multi-Subs only lifted a Spanish- or
    Italian-audio film to -50; subtitles are not an English track."""
    for title in ("Movie.2024.SPANISH.1080p.ENG.SUBS",
                  "Movie.2024.iTA.1080p.Multi-Subs",
                  "Movie.2024.FRENCH.1080p.English.Subbed"):
        assert _components(title, "movie", "Movie", 2024)["language_pref"] == -120.0, title
    assert _components("Movie.2024.iTA-ENG.1080p", "movie", "Movie",
                       2024)["language_pref"] == -80.0
