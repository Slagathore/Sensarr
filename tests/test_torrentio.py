"""Torrentio client tests. No network: every call goes through a fake transport."""

import json
from pathlib import Path

import pytest

import torrentio

H = "1c8802a81f91dc74b58e33ba6af757b3f01cf164"
KEY = "SECRETKEY123456"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(torrentio, "MIN_INTERVAL_S", 0.0)
    torrentio.clear_cache()
    yield
    torrentio.clear_cache()


def _obj(title, name="Torrentio\n1080p", **extra):
    d = {"name": name, "title": title, "infoHash": H, "fileIdx": 87}
    d.update(extra)
    return d


def test_pack_file_line():
    s = torrentio.parse_stream(_obj(
        "Futurama Season 1-7 DVDRip\nS06/Futurama S06E12.mkv\n"
        "\U0001F464 263 \U0001F4BE 1.23 GB ⚙️ ThePirateBay"))
    assert s.info_hash == H and s.file_idx == 87
    assert s.torrent_name == "Futurama Season 1-7 DVDRip"
    assert s.file_path == "S06/Futurama S06E12.mkv"
    assert s.file_name == "Futurama S06E12.mkv"
    assert s.seeders == 263 and s.provider == "ThePirateBay"
    assert s.size_bytes == int(1.23 * 1024 ** 3)
    assert s.quality == "1080p" and s.cached is None


def test_single_file_no_file_line_uses_hint():
    s = torrentio.parse_stream(_obj(
        "Some.Movie.2020.1080p\n\U0001F464 5 \U0001F4BE 700 MB ⚙️ YTS",
        behaviorHints={"filename": "Some.Movie.2020.1080p.mkv"}))
    assert s.file_path is None
    assert s.file_name == "Some.Movie.2020.1080p.mkv"
    s2 = torrentio.parse_stream(_obj(
        "Some.Movie\n\U0001F464 5 \U0001F4BE 700 MB ⚙️ YTS"))
    assert s2.file_name is None and s2.torrent_name == "Some.Movie"


def test_joined_provider_and_multi_audio():
    s = torrentio.parse_stream(_obj(
        "Name\nf.mkv\n\U0001F464 9 \U0001F4BE 1 GB ⚙️ 1337x\nMulti Audio"))
    assert s.provider == "1337x"
    assert s.audio_tags == ("Multi Audio",)
    assert s.language_flags == ()


def test_dual_audio_with_flags():
    s = torrentio.parse_stream(_obj(
        "Name\nf.mkv\n\U0001F464 9 \U0001F4BE 1 GB ⚙️ 1337x\n"
        "Dual Audio / \U0001F1EC\U0001F1E7 / \U0001F1F9\U0001F1F7"))
    assert s.audio_tags == ("Dual Audio",)
    assert s.language_flags == ("\U0001F1EC\U0001F1E7", "\U0001F1F9\U0001F1F7")


def test_flags_only_line():
    s = torrentio.parse_stream(_obj(
        "Name\n\U0001F464 9 \U0001F4BE 1 GB ⚙️ X\n\U0001F1EE\U0001F1F9"))
    assert s.language_flags == ("\U0001F1EE\U0001F1F9",) and s.audio_tags == ()


def test_html_entities_unescaped():
    s = torrentio.parse_stream(_obj(
        "A Bug&#039;s Life\n\U0001F464 1 \U0001F4BE 1 GB ⚙️ X"))
    assert s.torrent_name == "A Bug's Life"


def test_rd_mode_cached_and_not():
    url = f"https://torrentio.strem.fun/resolve/realdebrid/{KEY}/{H.upper()}/null/4/f.mkv"
    obj = {"name": "[RD+] Torrentio\n4k HDR", "url": url,
           "title": "N\nf.mkv\n\U0001F464 2 \U0001F4BE 2 GB ⚙️ X"}
    s = torrentio.parse_stream(obj)
    assert s.info_hash == H and s.file_idx == 4
    assert s.cached is True and s.quality == "4k HDR"
    assert KEY not in repr(s)
    obj["name"] = "[RD download] Torrentio\n720p"
    assert torrentio.parse_stream(obj).cached is False


def test_no_hash_returns_none():
    assert torrentio.parse_stream({"name": "x", "title": "y", "url": "http://a/b"}) is None
    assert torrentio.parse_stream({"infoHash": "zz"}) is None


def test_parse_size():
    assert torrentio.parse_size("1.23 GB") == int(1.23 * 1024 ** 3)
    assert torrentio.parse_size("697.41 MB") == int(697.41 * 1024 ** 2)
    assert torrentio.parse_size("10 KB") == 10240
    assert torrentio.parse_size("2 TB") == 2 * 1024 ** 4
    assert torrentio.parse_size("5 B") == 5
    assert torrentio.parse_size("junk") == 0


def test_build_url_and_redaction():
    u = torrentio.build_url("https://t.example/", "series", "tt0149460:6:12",
                            options={"sort": "qualitysize", "qualityfilter": "scr,cam"},
                            debrid_key=KEY)
    assert u == ("https://t.example/sort=qualitysize|qualityfilter=scr,cam|"
                 f"realdebrid={KEY}/stream/series/tt0149460:6:12.json")
    r = torrentio.redact(u)
    assert KEY not in r and "realdebrid=***" in r
    assert torrentio.build_url("https://t.example", "movie", "kitsu:123") == \
        "https://t.example/stream/movie/kitsu:123.json"
    assert "/resolve/realdebrid/***/" in torrentio.redact(
        f"https://x/resolve/realdebrid/{KEY}/{H}/null/1/a.mkv")


def test_search_series_urls(monkeypatch):
    seen = []

    def tr(url, headers, timeout):
        seen.append(url)
        return 200, b'{"streams": []}'

    torrentio.search("tt0149460", "series", season=6, episode=12, transport=tr)
    torrentio.search("kitsu:7442", "series", season=1, episode=3, transport=tr)
    torrentio.search("tt0120623", "movie", transport=tr)
    assert seen[0].endswith("/stream/series/tt0149460:6:12.json")
    assert seen[1].endswith("/stream/series/kitsu:7442:3.json")
    assert seen[2].endswith("/stream/movie/tt0120623.json")
    with pytest.raises(ValueError):
        torrentio.search("tt1", "series", transport=tr)


def test_404_is_empty():
    assert torrentio.search("tt9", "movie", transport=lambda u, h, t: (404, b"")) == []


def test_5xx_raises_with_status_and_no_key():
    with pytest.raises(torrentio.TorrentioError) as ei:
        torrentio.search("tt9", "movie", debrid_key=KEY,
                         transport=lambda u, h, t: (503, b""))
    assert ei.value.status == 503 and KEY not in str(ei.value)


def test_bad_json_and_transport_error():
    with pytest.raises(torrentio.TorrentioError):
        torrentio.search("tt9", "movie", transport=lambda u, h, t: (200, b"<html>"))
    torrentio.clear_cache()

    def boom(url, headers, timeout):
        raise OSError(f"timeout talking to {url}")

    with pytest.raises(torrentio.TorrentioError) as ei:
        torrentio.search("tt9", "movie", debrid_key=KEY, transport=boom)
    assert KEY not in str(ei.value)


def test_cache_hit_avoids_second_call():
    calls = []

    def tr(url, headers, timeout):
        calls.append(url)
        return 200, json.dumps({"streams": [_obj("N\n\U0001F464 1 \U0001F4BE 1 GB ⚙️ X")]}).encode()

    a = torrentio.search("tt1", "movie", transport=tr)
    b = torrentio.search("tt1", "movie", transport=tr)
    assert len(calls) == 1 and a == b and len(a) == 1


def test_magnet_for():
    s = torrentio.parse_stream(_obj("My Show S01\n\U0001F464 1 \U0001F4BE 1 GB ⚙️ X"))
    m = torrentio.magnet_for(s, ["udp://tracker.example:80/announce"])
    assert f"btih:{H}" in m and "dn=My%20Show%20S01" in m
    assert "tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce" in m


def test_live_sample_fixture_parses():
    path = Path(__file__).parent / "data" / "torrentio_series_sample.json"
    if not path.exists():
        pytest.skip("fixture not present")
    data = json.loads(path.read_text(encoding="utf-8"))
    streams = [torrentio.parse_stream(o) for o in data["streams"]]
    assert streams and all(s and len(s.info_hash) == 40 for s in streams)
    assert all(s.size_bytes > 0 and s.provider for s in streams)
    assert any(s.file_path for s in streams)
