# =============================================================================
# TMDB cross-reference for TVDB shows. "Monster (2022)" (TVDB 389492, the
# Netflix anthology) was linked by title search to Monster High: Webisodes, so
# its runtime read 4 minutes and every real episode was "oversize"; "East of
# Eden (2026)" matched nothing at all. Both from the bracketed year in the title.
# =============================================================================
import urllib.parse

import config
import media_lookup
import shows_store
import show_tracker


def _search_stub(results, seen):
    def fake(url, *a, **k):
        seen.append(url)
        return {"results": results}
    return fake


def test_bracketed_year_becomes_the_year_filter(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "k")
    seen = []
    monkeypatch.setattr(media_lookup, "_get_json", _search_stub([
        {"id": 113988, "name": "DAHMER - Monster: The Jeffrey Dahmer Story",
         "first_air_date": "2022-09-21"},
    ], seen))
    media_lookup.resolve_tmdb_tv_id("Monster (2022)")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
    assert q["query"] == ["Monster"]
    assert q["first_air_date_year"] == ["2022"]


def test_title_without_brackets_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "k")
    seen = []
    monkeypatch.setattr(media_lookup, "_get_json", _search_stub([], seen))
    media_lookup.resolve_tmdb_tv_id("2073", 2024)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
    assert q["query"] == ["2073"]          # a title that IS a year survives
    assert q["first_air_date_year"] == ["2024"]


def test_find_by_tvdb_needs_exactly_one_hit(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "k")
    answers = {
        "404093": {"tv_results": [{"id": 127529, "name": "Bloodhounds"}]},
        "389492": {"tv_results": []},
        "1": {"tv_results": [{"id": 5}, {"id": 6}]},
    }
    monkeypatch.setattr(media_lookup, "_get_json",
                        lambda url, *a, **k: answers[url.split("/find/")[1].split("?")[0]])
    assert media_lookup.find_tmdb_tv_by_tvdb("404093") == "127529"
    assert media_lookup.find_tmdb_tv_by_tvdb("389492") is None
    assert media_lookup.find_tmdb_tv_by_tvdb("1") is None
    assert media_lookup.find_tmdb_tv_by_tvdb(None) is None


def test_tvdb_show_uses_the_exact_link_before_a_title_search(monkeypatch):
    show_id = shows_store.upsert_show(title="Bloodhounds", media_type="tv",
                                      source="tvdb", external_id="404093", year=2023)
    monkeypatch.setattr(show_tracker, "find_tmdb_tv_by_tvdb", lambda tvdb: "127529")

    def _no_search(*a, **k):
        raise AssertionError("title search should not run when TMDB knows the TVDB id")
    monkeypatch.setattr(show_tracker, "resolve_tmdb_tv_id", _no_search)
    assert show_tracker._ensure_tmdb_id(shows_store.get_show(show_id)) == "127529"
    assert shows_store.get_show(show_id).tmdb_id == "127529"


def test_tvdb_show_falls_back_to_title_search(monkeypatch):
    show_id = shows_store.upsert_show(title="Monster (2022)", media_type="tv",
                                      source="tvdb", external_id="389492", year=2022)
    monkeypatch.setattr(show_tracker, "find_tmdb_tv_by_tvdb", lambda tvdb: None)
    monkeypatch.setattr(show_tracker, "resolve_tmdb_tv_id", lambda *a, **k: "113988")
    assert show_tracker._ensure_tmdb_id(shows_store.get_show(show_id)) == "113988"
