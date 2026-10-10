# =============================================================================
# 2026-10-10 fixes:
#  - JoJo S06 (2026-10-04): three manual grabs were routed with one raw torrent
#    name as the "show", which created a junk show folder and named every file
#    "[ToonsHub] JoJos ... S06E03 1080p ... - S06E0x".
#  - Episode files follow the naming already used in their season folder.
#  - A pack and a single episode of the same torrent: whichever finishes
#    second dedupes instead of erroring.
#  - Torrentio options per media type; the attempt cap ignores app faults.
# =============================================================================

import json

import pytest

import config
import db
import download_manager
import downloads_store
import torrent_routing
import torrent_search
from download_manager import DownloadManager

RAW = ("[ToonsHub] JoJos Bizarre Adventure S06E03 1080p NF WEB-DL DUAL AAC2.0 "
       "H.264 (JoJo no Kimyou na Bouken Steel Ball Run - 2nd - 3rd STAGE, "
       "Dual-Audio, Multi-Subs)")


@pytest.fixture(autouse=True)
def _fresh():
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("downloads", "download_history", "download_files"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


# --- names --------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    (RAW, True),
    ("Show.S01E02.720p.HDTV.x264-GRP", True),
    ("[SubsPlease] Show - 05", True),
    ("JoJos Bizarre Adventure", False),
    ("JoJo`s Bizarre Adventure (2012)", False),
    ("Man vs. Wild", False),
    ("Dexter: Resurrection", False),
])
def test_release_names_are_told_apart_from_show_names(title, expected):
    assert torrent_routing.looks_like_release_name(title) is expected


def test_a_raw_release_name_never_founds_a_show_folder(tmp_path, monkeypatch):
    anime_root = tmp_path / "Anime"
    (anime_root / "JoJos Bizarre Adventure" / "Season 06").mkdir(parents=True)
    monkeypatch.setattr(config, "media_paths_for_types",
                        lambda mt: [str(anime_root)])
    plan = torrent_routing.plan_route(
        "[ToonsHub] JoJos Bizarre Adventure S06E05 1080p NF WEB-DL DUAL AAC2.0 H.264",
        "anime", request_title=RAW)
    assert plan.confident
    assert plan.dest_dir == str(anime_root / "JoJos Bizarre Adventure" / "Season 06")
    assert plan.new_filename == "JoJos Bizarre Adventure - S06E05"
    assert not any("ToonsHub" in p.name for p in anime_root.iterdir())


def test_unmatched_raw_name_stays_in_staging_instead(tmp_path, monkeypatch):
    anime_root = tmp_path / "Anime"
    anime_root.mkdir()
    monkeypatch.setattr(config, "media_paths_for_types",
                        lambda mt: [str(anime_root)])
    plan = torrent_routing.plan_route(RAW, "anime", request_title=RAW)
    assert not plan.confident
    assert list(anime_root.iterdir()) == []


def test_episode_names_follow_the_season_folder(tmp_path):
    season = tmp_path / "JoJos Bizarre Adventure" / "Season 06"
    season.mkdir(parents=True)
    for n in (1, 2, 3):
        (season / f"JoJos Bizarre Adventure - S06E0{n}.mkv").write_bytes(b"x")
    base = torrent_routing.episode_name_base(
        "JoJo`s Bizarre Adventure (2012)", str(season.parent), str(season))
    assert base == "JoJos Bizarre Adventure"


def test_episode_name_falls_back_to_folder_then_title(tmp_path):
    folder = tmp_path / "Lanterns"
    folder.mkdir()
    assert torrent_routing.episode_name_base("Lanterns (2026)", str(folder),
                                             str(folder / "Season 01")) == "Lanterns"
    junk = tmp_path / "[bonkai77] Show [1080p]"
    junk.mkdir()
    assert torrent_routing.episode_name_base("Show", str(junk), None) == "Show"
    # AniDB's backtick becomes a real apostrophe.
    assert torrent_routing.episode_name_base("JoJo`s Bizarre Adventure (2012)") == \
        "JoJo's Bizarre Adventure (2012)"


def test_a_manual_grab_hands_routing_no_fake_request_title():
    row = type("Row", (), {
        "route_title": None, "request_id": None, "title": RAW,
        "want_json": json.dumps({"request_id": None, "search_alias": RAW,
                                 "canonical_title": None})})()
    assert download_manager._request_title_from_row(row) is None
    req_row = type("Row", (), {
        "route_title": None, "request_id": 5, "title": RAW,
        "want_json": json.dumps({"request_id": 5, "search_alias": "Inception 2010",
                                 "canonical_title": "Inception"})})()
    assert download_manager._request_title_from_row(req_row) == "Inception 2010"


# --- pack + single episode of the same torrent ------------------------------------

def _row(files, **kw):
    did = downloads_store.create_download(
        title="Pack", magnet="magnet:?xt=urn:btih:" + "ab" * 20, source="torrentio",
        media_type="tv", request_id=None, staging_dir="x", planned_dest=None,
        planned_name=None, route_reason=None, auto_rename=True, auto_move=True, **kw)
    downloads_store.set_files(did, files)
    return downloads_store.get_download(did)


def test_second_finisher_finds_the_first_already_placed_it(tmp_path):
    placed = tmp_path / "Show - S01E05.mkv"
    placed.write_bytes(b"x")
    pack = _row(["Pack/S01/E05.mkv", "Pack/S01/E06.mkv"])
    single = _row(["Pack\\S01\\E05.mkv"])
    downloads_store.add_download_file(
        pack.download_id, source_relative_path="Pack/S01/E05.mkv",
        source_absolute_path=None, media_role="episode", final_path=str(placed),
        verification_state="verified")
    got = DownloadManager._placed_by_another_download(single)
    assert [f.download_id for f in got] == [pack.download_id]


def test_not_a_duplicate_when_only_part_of_it_was_placed(tmp_path):
    placed = tmp_path / "Show - S01E05.mkv"
    placed.write_bytes(b"x")
    single = _row(["Pack/S01/E05.mkv"])
    pack = _row(["Pack/S01/E05.mkv", "Pack/S01/E06.mkv"])
    downloads_store.add_download_file(
        single.download_id, source_relative_path="Pack/S01/E05.mkv",
        source_absolute_path=None, media_role="episode", final_path=str(placed),
        verification_state="verified")
    # The pack still needs E06 from staging: it is not a duplicate.
    assert DownloadManager._placed_by_another_download(pack) == []


def test_a_placed_copy_that_vanished_does_not_count(tmp_path):
    pack = _row(["Pack/E05.mkv"])
    single = _row(["Pack/E05.mkv"])
    downloads_store.add_download_file(
        pack.download_id, source_relative_path="Pack/E05.mkv",
        source_absolute_path=None, media_role="episode",
        final_path=str(tmp_path / "gone.mkv"), verification_state="verified")
    assert DownloadManager._placed_by_another_download(single) == []


# --- Torrentio options --------------------------------------------------------------

def test_torrentio_options_per_media_type(monkeypatch):
    monkeypatch.setattr(config, "TORRENTIO_OPTIONS", "")
    tv = torrent_search.torrentio_options("tv")
    anime = torrent_search.torrentio_options("anime")
    assert "nyaasi" not in tv["providers"] and "rutor" not in tv["providers"]
    assert "ilcorsaronero" not in tv["providers"] and "thepiratebay" in tv["providers"]
    assert "nyaasi" in anime["providers"] and "1337x" in anime["providers"]
    assert set(tv["qualityfilter"].split(",")) == {"threed", "scr", "cam"}
    monkeypatch.setattr(config, "TORRENTIO_OPTIONS", "providers=yts|sort=seeders")
    assert torrent_search.torrentio_options("tv") == {"providers": "yts",
                                                      "sort": "seeders"}


# --- attempt cap ------------------------------------------------------------------------

def test_attempt_cap_still_counts_app_side_failures():
    """The cap is the loop guard: a setup fault that fails every grab must
    still escalate instead of reopening the request forever."""
    import queue_store
    req = queue_store.add_request("Some Movie", "cole", media_type="movie")
    for error in ("permission denied", "stalled: no data for 900s"):
        did = downloads_store.create_download(
            title="x", magnet="magnet:?xt=urn:btih:" + "cd" * 20, source="tpb",
            media_type="movie", request_id=req.request_id, staging_dir="x",
            planned_dest=None, planned_name=None, route_reason=None,
            auto_rename=False, auto_move=False)
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET status='error', error=? WHERE id=?",
                         (error, did))
            conn.commit()
    assert downloads_store.request_grab_attempts(req.request_id) == 2


# --- verifier round 4 follow-ups -------------------------------------------------

def test_bracketed_real_titles_are_not_release_names():
    assert not torrent_routing.looks_like_release_name("[Oshi no Ko]")
    assert torrent_routing.looks_like_release_name("[SubsPlease] Oshi no Ko - 05")


def test_existing_junk_names_in_the_folder_are_not_copied(tmp_path):
    season = tmp_path / "JoJos Bizarre Adventure" / "Season 06"
    season.mkdir(parents=True)
    for n in (1, 2, 3):
        (season / f"[ToonsHub] JoJos Bizarre Adventure S06E03 1080p NF WEB-DL - S06E0{n}.mkv"
         ).write_bytes(b"x")
    assert torrent_routing.episode_name_base(
        "JoJo`s Bizarre Adventure (2012)", str(season.parent), str(season)) == \
        "JoJos Bizarre Adventure"


def test_another_shows_files_in_a_shared_folder_are_not_copied(tmp_path):
    shared = tmp_path / "Shared Season"
    shared.mkdir()
    for n in (1, 2, 3):
        (shared / f"Show B - S01E0{n}.mkv").write_bytes(b"x")
    assert torrent_routing.episode_name_base("Show A", None, str(shared)) == "Show A"


def test_a_junk_folder_name_is_cleaned_before_naming(tmp_path):
    folder = tmp_path / "World Trigger [BD]"
    folder.mkdir()
    assert torrent_routing.episode_name_base(
        folder.name, str(folder), str(folder / "Season 03")) == "World Trigger"


def test_same_path_from_a_different_torrent_is_not_a_duplicate(tmp_path):
    placed = tmp_path / "Show A - S01E01.mkv"
    placed.write_bytes(b"x")
    a = _row(["S01E01.mkv"])
    did = downloads_store.create_download(
        title="Other", magnet="magnet:?xt=urn:btih:" + "ef" * 20, source="tpb",
        media_type="tv", request_id=None, staging_dir="x", planned_dest=None,
        planned_name=None, route_reason=None, auto_rename=True, auto_move=True)
    downloads_store.set_files(did, ["S01E01.mkv"])
    downloads_store.add_download_file(
        a.download_id, source_relative_path="S01E01.mkv", source_absolute_path=None,
        media_role="episode", final_path=str(placed), verification_state="verified")
    assert DownloadManager._placed_by_another_download(
        downloads_store.get_download(did)) == []


def test_a_legacy_row_with_no_request_gets_no_title():
    row = type("Row", (), {"route_title": None, "request_id": None,
                           "title": "Some Show Season 2 Complete", "want_json": None})()
    assert download_manager._request_title_from_row(row) is None


def test_unregister_keeps_a_newer_runner_of_the_same_download():
    downloads_store.clear_runner_registry()
    downloads_store.register_runner(7, 111)
    downloads_store.register_runner(7, 222)          # restarted: new process
    downloads_store.unregister_runner(7, 111)        # old thread cleaning up
    assert downloads_store.registered_runners() == [(7, 222)]
