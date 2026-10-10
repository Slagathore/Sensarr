# =============================================================================
# Torrentio as a search source, end to end inside the app: ID lookup, the
# merged pool, pinned pack files, and the apibay calls it saves.
# =============================================================================

import json

import pytest

import config
import download_manager
import downloads_store
import shows_store
import torrent_search
import torrentio
from download_manager import DownloadManager, _build_want_snapshot
from torrent_search import TorrentioTarget, TorrentResult

GB = 1024 ** 3
MB = 1024 ** 2


def _stream(ih, *, torrent, file_path=None, size=1 * GB, seeders=50,
            idx=None, cached=None, audio=(), flags=()):
    name = file_path.replace("\\", "/").split("/")[-1] if file_path else None
    return torrentio.TorrentioStream(
        info_hash=ih * 40 if len(ih) == 1 else ih, file_idx=idx,
        torrent_name=torrent, file_name=name, file_path=file_path,
        size_bytes=size, seeders=seeders, provider="ThePirateBay",
        audio_tags=tuple(audio), language_flags=tuple(flags), quality="1080p",
        cached=cached)


@pytest.fixture(autouse=True)
def _torrentio_on(monkeypatch):
    monkeypatch.setattr(config, "TORRENTIO_ENABLED", True)
    monkeypatch.setattr(config, "REALDEBRID_API_TOKEN", "")
    monkeypatch.setattr(config, "MIN_SEEDERS", 4)
    with torrent_search._PROVIDER_LOCK:
        torrent_search._PROVIDER_FAILURE_UNTIL.clear()
    yield
    with torrent_search._PROVIDER_LOCK:
        torrent_search._PROVIDER_FAILURE_UNTIL.clear()


def _fake_torrentio(monkeypatch, streams, calls=None):
    def fake(video_id, kind, **kw):
        if calls is not None:
            calls.append((video_id, kind, kw.get("season"), kw.get("episode"),
                          kw.get("debrid_key")))
        return list(streams)
    monkeypatch.setattr(torrentio, "search", fake)


def _no_text(monkeypatch, record):
    def tpb(*a, **k):
        record.append(("tpb", a))
        return []
    def nyaa(*a, **k):
        record.append(("nyaa", a))
        return []
    monkeypatch.setattr(torrent_search, "search_tpb", tpb)
    monkeypatch.setattr(torrent_search, "search_yts", lambda *a, **k: [])
    monkeypatch.setattr(torrent_search, "search_nyaa", nyaa)


FUTURAMA = TorrentioTarget("tt0149460", "series", 6, 12)


def test_pack_file_becomes_a_single_episode_result(monkeypatch):
    _fake_torrentio(monkeypatch, [
        _stream("a", torrent="Futurama Season 1-7 S01-S07 DVDRip-1080p MiXE",
                file_path="Futurama.S06E12.1080p.BluRay.DTS.x264-CtrlHD.mkv",
                idx=87, seeders=263, size=int(1.23 * GB)),
        _stream("b", torrent="Futurama S06E12 720p HDTV x264", seeders=40),
        _stream("c", torrent="Futurama S06E12 1080p WEB", seeders=12),
    ])
    text = []
    _no_text(monkeypatch, text)
    pool = torrent_search.search_collect("Futurama S06E12", "tv",
                                         torrentio_target=FUTURAMA)
    by_hash = {torrent_search._infohash(r.magnet): r for r in pool.results}
    pack = by_hash["a" * 40]
    assert pack.title == "Futurama.S06E12.1080p.BluRay.DTS.x264-CtrlHD.mkv"
    assert pack.file_idx == 87 and pack.file_name.endswith("CtrlHD.mkv")
    assert pack.size_bytes == int(1.23 * GB)       # the FILE, not the pack
    assert pack.source == "torrentio" and "btih:" + "a" * 40 in pack.magnet
    single = by_hash["b" * 40]
    assert single.file_idx is None and single.file_name is None
    # A solid, seeded Torrentio pool means apibay is not asked at all.
    assert text == []
    assert pool.pool_stats["text_search_skipped"] is True
    assert pool.pool_stats["per_source"]["torrentio"] == 3


def test_thin_torrentio_pool_still_runs_the_text_providers(monkeypatch):
    _fake_torrentio(monkeypatch, [_stream("a", torrent="Futurama S06E12 HDTV",
                                          seeders=2)])
    text = []
    _no_text(monkeypatch, text)
    pool = torrent_search.search_collect("Futurama S06E12", "tv",
                                         torrentio_target=FUTURAMA)
    assert any(src == "tpb" for src, _ in text)
    assert pool.pool_stats["text_search_skipped"] is False


def test_anime_always_asks_nyaa_too(monkeypatch):
    _fake_torrentio(monkeypatch, [
        _stream(ch, torrent=f"[Grp{ch}] Mashle - S01E05 [1080p]", seeders=99)
        for ch in "abc"])
    text = []
    _no_text(monkeypatch, text)
    torrent_search.search_collect("Mashle S01E05", "anime",
                                  torrentio_target=TorrentioTarget("tt1", "series", 1, 5))
    assert any(src == "nyaa" for src, _ in text)


def test_duplicate_keeps_the_torrentio_copy_with_the_best_seeders(monkeypatch):
    ih = "d" * 40
    _fake_torrentio(monkeypatch, [_stream(ih, torrent="Show S01E01 1080p",
                                          file_path="Show.S01E01.mkv", idx=3,
                                          seeders=5)])
    monkeypatch.setattr(torrent_search, "search_tpb", lambda *a, **k: [
        TorrentResult(title="Show S01E01 1080p", magnet=f"magnet:?xt=urn:btih:{ih}",
                      size_bytes=GB, seeders=400, source="tpb", media_type="tv")])
    pool = torrent_search.search_collect(
        "Show S01E01", "tv", torrentio_target=TorrentioTarget("tt2", "series", 1, 1))
    [only] = list(pool.results)
    assert only.source == "torrentio" and only.file_idx == 3
    assert only.seeders == 400


def test_torrentio_error_trips_its_circuit_only(monkeypatch):
    def boom(*a, **k):
        raise torrentio.TorrentioError("HTTP 503", 503)
    monkeypatch.setattr(torrentio, "search", boom)
    text = []
    _no_text(monkeypatch, text)
    pool = torrent_search.search_collect("Futurama S06E12", "tv",
                                         torrentio_target=FUTURAMA)
    assert list(pool.results) == []
    assert any(src == "tpb" for src, _ in text)   # text search still ran
    status = torrent_search.provider_circuit_status(("torrentio", "tpb"))
    assert status["torrentio"]["available"] is False
    assert status["tpb"]["available"] is True


def test_cache_check_sends_the_rd_token_only_when_asked(monkeypatch):
    calls = []
    _fake_torrentio(monkeypatch, [], calls)
    _no_text(monkeypatch, [])
    monkeypatch.setattr(config, "REALDEBRID_API_TOKEN", "tok123")
    monkeypatch.setattr(config, "REALDEBRID_CACHE_CHECK", True)
    torrent_search.search_torrentio(FUTURAMA, "tv")
    monkeypatch.setattr(config, "REALDEBRID_CACHE_CHECK", False)
    torrentio.clear_cache()
    torrent_search.search_torrentio(FUTURAMA, "tv")
    assert [c[4] for c in calls] == ["tok123", None]


def test_disabled_or_no_target_never_calls_torrentio(monkeypatch):
    calls = []
    _fake_torrentio(monkeypatch, [], calls)
    assert torrent_search.search_torrentio(None, "tv") == []
    monkeypatch.setattr(config, "TORRENTIO_ENABLED", False)
    assert torrent_search.search_torrentio(FUTURAMA, "tv") == []
    assert calls == []


def test_title_choice_for_pack_files():
    t = torrent_search._torrentio_title
    assert t(_stream("a", torrent="Pack", file_path="S06/Futurama S06E12.mkv")) == "Futurama S06E12.mkv"
    # A file that does not name the show falls back to the torrent name.
    assert t(_stream("a", torrent="Futurama S06 1080p", file_path="S06/12.mkv")) == "Futurama S06 1080p"
    assert t(_stream("a", torrent="Pack", file_path="S06E12.mkv")) == "Pack"
    # Movie collections: the file names the film and year.
    assert t(_stream("a", torrent="[PACK] Pixar 4K Collection",
                     file_path="A Bugs Life (1998) 2160p.mkv")) == "A Bugs Life (1998) 2160p.mkv"


def test_want_snapshot_and_runner_options_carry_the_pinned_file():
    result = TorrentResult(title="Futurama.S06E12.mkv", magnet="magnet:?xt=urn:btih:" + "a" * 40,
                           size_bytes=GB, seeders=9, source="torrentio",
                           media_type="tv", file_idx=87,
                           file_name="Futurama.S06E12.mkv")
    want = _build_want_snapshot(result, None, request_title=None, show_id=1,
                                season=6, episode=12, minutes=22.0)
    assert want["file_idx"] == 87 and want["file_name"] == "Futurama.S06E12.mkv"
    row = type("Row", (), {"want_json": json.dumps(want), "season": 6,
                           "episode": 12})()
    opts = DownloadManager._runner_selection_options(row)
    assert opts["fileIdx"] == 87 and opts["fileName"] == "Futurama.S06E12.mkv"
    plain = DownloadManager._runner_selection_options(
        type("Row", (), {"want_json": None, "season": 1, "episode": 2})())
    assert plain["fileIdx"] is None and plain["fileName"] is None


def test_episode_target_prefers_the_anime_mapping(monkeypatch):
    import anime_db
    import media_lookup
    monkeypatch.setattr(anime_db, "mapping_for_anidb",
                        lambda ext: {"imdb_id": "tt9999999"})
    monkeypatch.setattr(media_lookup, "get_imdb_id",
                        lambda kind, tmdb: pytest.fail("TMDB not needed"))
    show = shows_store.TrackedShow.__new__(shows_store.TrackedShow)
    object.__setattr__(show, "show_id", 1)
    object.__setattr__(show, "source", "anidb")
    object.__setattr__(show, "external_id", "17000")
    object.__setattr__(show, "tmdb_id", "123")
    target = DownloadManager._torrentio_target_for_episode(show, 1, 5)
    assert target == TorrentioTarget("tt9999999", "series", 1, 5)


def test_episode_target_from_tmdb_and_none_without_ids(monkeypatch):
    import media_lookup
    monkeypatch.setattr(media_lookup, "get_imdb_id",
                        lambda kind, tmdb: "tt0149460" if (kind, tmdb) == ("tv", "615") else None)
    show = shows_store.TrackedShow.__new__(shows_store.TrackedShow)
    for k, v in {"show_id": 2, "source": "tvdb", "external_id": "73871",
                 "tmdb_id": "615"}.items():
        object.__setattr__(show, k, v)
    assert DownloadManager._torrentio_target_for_episode(show, 6, 12) == FUTURAMA
    object.__setattr__(show, "tmdb_id", None)
    assert DownloadManager._torrentio_target_for_episode(show, 6, 12) is None


def test_movie_request_targets(monkeypatch):
    import media_lookup
    monkeypatch.setattr(media_lookup, "get_imdb_id",
                        lambda kind, tmdb: "tt0120623" if (kind, tmdb) == ("movie", "9487") else None)
    req = type("Req", (), {"identity_source": "tmdb", "external_id": "9487",
                           "request_id": 1})()
    assert DownloadManager._torrentio_target_for_movie_request(req) == \
        TorrentioTarget("tt0120623", "movie")
    omdb = type("Req", (), {"identity_source": "omdb", "external_id": "tt0111161",
                            "request_id": 2})()
    assert DownloadManager._torrentio_target_for_movie_request(omdb).imdb_id == "tt0111161"
    other = type("Req", (), {"identity_source": "jikan", "external_id": "5",
                             "request_id": 3})()
    assert DownloadManager._torrentio_target_for_movie_request(other) is None


def test_cache_tags_are_only_requested_when_real_debrid_is_the_engine(monkeypatch):
    """Verifier finding: with qBittorrent as the engine a 'cached' tag would
    let a 0-seed release past the floor and then never finish."""
    monkeypatch.setattr(config, "REALDEBRID_API_TOKEN", "tok123")
    monkeypatch.setattr(config, "REALDEBRID_CACHE_CHECK", True)
    monkeypatch.setattr(config, "QBITTORRENT_ENABLED", False)
    assert torrent_search.debrid_cache_key() == "tok123"
    monkeypatch.setattr(config, "QBITTORRENT_ENABLED", True)
    assert torrent_search.debrid_cache_key() is None


def test_one_cached_result_is_not_a_pool():
    one = [TorrentResult(title="Movie 2024 HDCAM", magnet="magnet:?xt=urn:btih:" + "a" * 40,
                         size_bytes=GB, seeders=0, source="torrentio",
                         media_type="movie", cached=True)]
    assert not torrent_search._torrentio_is_enough(one, "movie")
    three = one + [
        TorrentResult(title=f"Movie 2024 1080p {i}", magnet="magnet:?xt=urn:btih:" + ch * 40,
                      size_bytes=GB, seeders=0, source="torrentio",
                      media_type="movie", cached=None)
        for i, ch in enumerate("bc")]
    assert torrent_search._torrentio_is_enough(three, "movie")


def test_torrent_level_cam_or_language_tag_wins_over_the_inner_file_name():
    t = torrent_search._torrentio_title
    cam = _stream("a", torrent="Movie.2024.HDCAM-XYZ", file_path="Movie.2024.mkv")
    assert t(cam) == "Movie.2024.HDCAM-XYZ"
    french = _stream("a", torrent="Show S01 FRENCH 1080p WEB",
                     file_path="Show.S01E03.1080p.mkv")
    assert t(french) == "Show S01 FRENCH 1080p WEB"


# --- joining an in-flight download of the same torrent (verifier #1 / N3) -----

def _live(magnet, *, want=None, season=None, episode=None):
    did = downloads_store.create_download(
        title="Pack", magnet=magnet, source="torrentio", media_type="tv",
        request_id=None, staging_dir=str(config.TORRENT_DOWNLOAD_DIR),
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False, season=season, episode=episode,
        want_json=json.dumps(want) if want else None)
    downloads_store.set_status(did, "downloading")
    return did


@pytest.fixture
def grab_dm(monkeypatch):
    import db
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM downloads")
        conn.commit()
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    return dm


PACK = "magnet:?xt=urn:btih:" + "ab" * 20


def _pinned_pick(name, idx):
    return TorrentResult(title=name, magnet=PACK, size_bytes=GB, seeders=50,
                         source="torrentio", media_type="tv", file_idx=idx,
                         file_name=name)


def test_a_pick_of_another_pack_file_starts_its_own_download(grab_dm):
    e05 = _live(PACK, want={"file_idx": 4, "file_name": "Show.S01E05.mkv"},
                season=1, episode=5)
    got = grab_dm.grab(_pinned_pick("Show.S01E06.mkv", 5),
                       episode_context=(7, 1, 6))
    assert got != e05


def test_the_same_pinned_file_joins_the_live_download(grab_dm):
    e06 = _live(PACK, want={"file_idx": 5, "file_name": "Show.S01E06.mkv"},
                season=1, episode=6)
    assert grab_dm.grab(_pinned_pick("Show.S01E06.mkv", 5),
                        episode_context=(7, 1, 6)) == e06


def test_a_whole_season_download_already_brings_the_pinned_file(grab_dm):
    pack = _live(PACK, season=1, episode=None)        # a season-pack grab
    assert grab_dm.grab(_pinned_pick("Show.S01E06.mkv", 5),
                        episode_context=(7, 1, 6)) == pack


def test_a_whole_pack_download_of_another_season_does_not(grab_dm):
    s2 = _live(PACK, season=2, episode=None)
    assert grab_dm.grab(_pinned_pick("Show.S01E06.mkv", 5),
                        episode_context=(7, 1, 6)) != s2


def test_the_right_live_copy_is_found_among_several(grab_dm):
    _live(PACK, want={"file_idx": 4, "file_name": "Show.S01E05.mkv"}, season=1, episode=5)
    e06 = _live(PACK, want={"file_idx": 5, "file_name": "Show.S01E06.mkv"},
                season=1, episode=6)
    assert grab_dm.grab(_pinned_pick("Show.S01E06.mkv", 5),
                        episode_context=(7, 1, 6)) == e06
