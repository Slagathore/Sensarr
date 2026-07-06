"""Folder-name cleaning, junk-tolerant identification scoring, and airing capture."""
from datetime import date, timedelta

import pytest

import show_tracker
import shows_store
from media_lookup import EpisodeInfo, MediaResult, best_title_similarity
from show_tracker import clean_show_folder_name


def test_clean_strips_release_junk_and_brackets():
    assert clean_show_folder_name(
        "[Hi10]_Shingeki_no_Bahamut_Genesis_[BD_1080p]"
    ) == ("Shingeki no Bahamut Genesis", None)
    assert clean_show_folder_name(
        "[bonkai77] Cool Show - Virgin Soul [WEB-DL] [1080p] [x265]"
    )[0] == "Cool Show - Virgin Soul"


def test_clean_strips_leading_date_bracket_hentai():
    assert clean_show_folder_name("[2000.08.12 - 2004.01.30] Yakin byoutou") == (
        "Yakin byoutou", None)


def test_clean_extracts_year_and_rejects_junk_only():
    assert clean_show_folder_name("American Housewife (2016)") == ("American Housewife", 2016)
    assert clean_show_folder_name("Andor (2022)") == ("Andor", 2022)
    assert clean_show_folder_name("[Unsorted]")[0] == ""


def test_clean_stops_at_season_marker():
    assert clean_show_folder_name("Great Show S2")[0] == "Great Show"
    assert clean_show_folder_name("Great Show Season 3 Complete")[0] == "Great Show"


def test_alt_title_scoring_matches_romaji_or_english():
    # A romaji folder name vs an English primary title still scores high via alt.
    q = "Shingeki no Bahamut Genesis"
    with_alt = MediaResult(title="Rage of Bahamut: Genesis", year=2014, external_id="1",
                           external_url="", media_type="anime", overview="", source="jikan",
                           alt_titles=("Shingeki no Bahamut: Genesis",))
    without_alt = MediaResult(title="Rage of Bahamut: Genesis", year=2014, external_id="1",
                              external_url="", media_type="anime", overview="", source="jikan")
    # The romaji alt title is a near-exact match; scoring against it wins big.
    assert best_title_similarity(q, with_alt) >= 0.95
    assert best_title_similarity(q, with_alt) > best_title_similarity(q, without_alt)


def test_identify_picks_best_across_both_tv_sources(monkeypatch):
    # TVDB returns a wrong-but-nonempty result; TMDB has the right one. The old
    # `TVDB or TMDB` short-circuit would have been stuck with the wrong TVDB hit.
    wrong = MediaResult(title="Completely Different", year=1990, external_id="w",
                        external_url="", media_type="tv", overview="", source="tvdb")
    right = MediaResult(title="Andor", year=2022, external_id="393189",
                        external_url="", media_type="tv", overview="", source="tmdb")
    monkeypatch.setattr(show_tracker, "search_tvdb_shows", lambda *a, **k: [wrong])
    monkeypatch.setattr(show_tracker, "search_tmdb_shows", lambda *a, **k: [right])
    match = show_tracker._identify_folder("Andor (2022)", "tv")
    assert match is not None and match.external_id == "393189"


@pytest.fixture()
def airing_show():
    show_id = shows_store.upsert_show(
        title="Airing Anime", media_type="anime", source="jikan",
        external_id="21", year=1999,
    )
    return show_id


def test_airing_captured_from_tmdb_next_air(airing_show, monkeypatch):
    soon = (date.today() + timedelta(days=5)).isoformat()
    monkeypatch.setattr(show_tracker, "resolve_tmdb_tv_id", lambda *a, **k: "37854")
    monkeypatch.setattr(show_tracker, "get_tmdb_next_air",
                        lambda tid: EpisodeInfo(23, 1169, "Next One", soon))
    # No episode fetcher for jikan-airing path needed; stub episode fetch empty.
    monkeypatch.setitem(show_tracker.EPISODE_FETCHERS, "jikan",
                        (lambda _id, **k: [], lambda _id: "Currently Airing"))

    show_tracker.sync_show(airing_show)
    show = shows_store.get_show(airing_show)
    assert show is not None
    assert show.tmdb_id == "37854"
    assert show.next_air_date == soon
    assert (show.next_season, show.next_episode) == (23, 1169)

    # The stored next-air drives the Upcoming panel even with no episode rows.
    upcoming = shows_store.upcoming_episodes(days=14)
    hit = [(s.title, e.episode) for s, e in upcoming if s.show_id == airing_show]
    assert ("Airing Anime", 1169) in hit


def test_no_next_air_clears_field(airing_show, monkeypatch):
    monkeypatch.setattr(show_tracker, "resolve_tmdb_tv_id", lambda *a, **k: "37854")
    monkeypatch.setattr(show_tracker, "get_tmdb_next_air", lambda tid: None)
    monkeypatch.setitem(show_tracker.EPISODE_FETCHERS, "jikan",
                        (lambda _id, **k: [], lambda _id: "Finished Airing"))
    show_tracker.sync_show(airing_show)
    show = shows_store.get_show(airing_show)
    assert show is not None and show.next_air_date is None
