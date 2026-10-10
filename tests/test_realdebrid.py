"""Real-Debrid client tests. Fake transport, fake clock, fake downloader."""

import json
import logging
import urllib.parse

import pytest

import realdebrid as rd

TOKEN = "SUPERSECRETTOKEN9999"
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


class FakeRD:
    """Scripted Real-Debrid. `infos` is consumed one per GET info (last repeats)."""

    def __init__(self, infos, links=None):
        self.infos = list(infos)
        self.calls = []
        self.selected = None
        self.deleted = []
        self.responses = []  # optional scripted (status, body) for the next calls
        self.unrestrict_map = links or {}

    def __call__(self, method, url, headers, data, timeout):
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        path = url[len(rd.API_BASE):]
        self.calls.append((method, path))
        if self.responses:
            return self.responses.pop(0)
        form = urllib.parse.parse_qs(data.decode()) if data else {}
        if path == "/torrents/addMagnet":
            return 201, json.dumps({"id": "TID1", "uri": "x"}).encode()
        if path.startswith("/torrents/info/"):
            info = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
            return 200, json.dumps(info).encode()
        if path.startswith("/torrents/selectFiles/"):
            self.selected = form["files"][0]
            return 204, b""
        if path.startswith("/torrents/delete/"):
            self.deleted.append(path.rsplit("/", 1)[1])
            return 204, b""
        if path == "/unrestrict/link":
            link = form["link"][0]
            return 200, json.dumps(self.unrestrict_map[link]).encode()
        if path == "/user":
            return 200, json.dumps({"username": "u", "type": "premium"}).encode()
        raise AssertionError(path)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def downloader(blobs, record=None, chunk=4):
    """Fake streaming download keyed by url; honours Range-style `start`."""
    def dl(url, start):
        if record is not None:
            record.append((url, start))
        data = blobs[url]
        status = 206 if start else 200
        body = data[start:]

        def gen():
            for i in range(0, len(body), chunk):
                yield body[i:i + chunk]
        return status, gen()
    return dl


def client(fake):
    return rd.RealDebridClient(TOKEN, transport=fake, sleep=lambda s: None)


def single_file_info(status="downloaded", **extra):
    info = {"id": "TID1", "filename": "Movie.2020", "status": status, "progress": 100,
            "files": [{"id": 1, "path": "/Movie.2020.mkv", "bytes": 10, "selected": 1}],
            "links": ["L1"]}
    info.update(extra)
    return info


LINKS = {"L1": {"filename": "Movie.2020.mkv", "filesize": 10, "download": "https://dl/1"}}
BLOBS = {"https://dl/1": b"0123456789"}


def test_cached_path_writes_and_verifies(tmp_path):
    fake = FakeRD([single_file_info()], LINKS)
    prog = []
    res = rd.fetch(client(fake), MAGNET, tmp_path, download=downloader(BLOBS),
                   on_progress=lambda s, f: prog.append((s, f)))
    assert res.cached is True and res.total_bytes == 10 and res.torrent_id == "TID1"
    assert res.rel_paths == ["Movie.2020.mkv"]
    assert (tmp_path / "Movie.2020.mkv").read_bytes() == b"0123456789"
    assert not list(tmp_path.glob("*.part"))
    assert prog[-1] == ("local", 1.0)
    assert fake.deleted == ["TID1"]


def test_delete_after_false_keeps_torrent(tmp_path):
    fake = FakeRD([single_file_info()], LINKS)
    rd.fetch(client(fake), MAGNET, tmp_path, download=downloader(BLOBS),
             delete_after=False)
    assert fake.deleted == []


def test_not_cached_then_downloaded_and_multi_file_folder(tmp_path):
    info_dl = {"status": "downloading", "progress": 40}
    done = {"id": "TID1", "filename": "Show.S01", "status": "downloaded", "progress": 100,
            "files": [{"id": 1, "path": "/ep1.mkv", "bytes": 4, "selected": 1},
                      {"id": 2, "path": "/ep2.mkv", "bytes": 4, "selected": 0}],
            "links": ["A"]}
    fake = FakeRD([info_dl, done],
                  {"A": {"filesize": 4, "download": "https://dl/a", "filename": "ep1.mkv"}})
    clock = Clock()
    res = rd.fetch(client(fake), MAGNET, tmp_path,
                   download=downloader({"https://dl/a": b"abcd"}),
                   sleep=clock.sleep, monotonic=clock.now)
    assert res.cached is False
    assert res.rel_paths == ["Show.S01/ep1.mkv"]
    assert (tmp_path / "Show.S01" / "ep1.mkv").read_bytes() == b"abcd"


def waiting_info(files):
    return {"id": "TID1", "filename": "Pack", "status": "waiting_files_selection",
            "progress": 0, "files": files, "links": []}


PACK = [{"id": 1, "path": "/S06/Show S06E11.mkv", "bytes": 1},
        {"id": 2, "path": "/S06/Show S06E12.mkv", "bytes": 1},
        {"id": 3, "path": "/S06/Show S06E12.srt", "bytes": 1},
        {"id": 4, "path": "/readme.txt", "bytes": 1}]


def run_selection(files, **kw):
    fake = FakeRD([waiting_info(files), single_file_info()], LINKS)
    clock = Clock()
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as d:
        rd.fetch(client(fake), MAGNET, pathlib.Path(d), download=downloader(BLOBS),
                 sleep=clock.sleep, monotonic=clock.now, **kw)
    return fake.selected


def test_selects_filename_match():
    assert run_selection(PACK, want_file_name="show s06e12.MKV") == "2"


def test_selects_idx_fallback():
    assert run_selection(PACK, want_file_name="nope.mkv", want_file_idx=0) == "1"


def test_selects_videos_only_with_matching_subs():
    assert run_selection(PACK) == "1,2,3"


def test_choose_files_pure_edges():
    assert rd.choose_files([{"id": 1, "path": "/a.txt"}, {"id": 2, "path": "/b.nfo"}]) == [1, 2]
    # idx pointing at a non-video falls through to the video list
    files = [{"id": 1, "path": "/a.txt"}, {"id": 2, "path": "/b.mkv"}]
    assert rd.choose_files(files, want_file_idx=0) == [2]
    assert rd.choose_files([]) == []


def test_remote_stall_deletes_and_raises(tmp_path):
    fake = FakeRD([{"status": "downloading", "progress": 10}], LINKS)
    clock = Clock()
    with pytest.raises(rd.RealDebridStalled):
        rd.fetch(client(fake), MAGNET, tmp_path, stall_timeout_s=60, poll_s=5,
                 sleep=clock.sleep, monotonic=clock.now, download=downloader(BLOBS))
    assert fake.deleted == ["TID1"]


def test_progress_resets_stall_timer(tmp_path):
    infos = [{"status": "downloading", "progress": p} for p in (10, 10, 10, 20, 20, 20)]
    infos.append(single_file_info())
    fake = FakeRD(infos, LINKS)
    clock = Clock()
    res = rd.fetch(client(fake), MAGNET, tmp_path, stall_timeout_s=12, poll_s=5,
                   sleep=clock.sleep, monotonic=clock.now, download=downloader(BLOBS))
    assert res.cached is False


@pytest.mark.parametrize("status", ["magnet_error", "error", "virus", "dead"])
def test_dead_statuses(tmp_path, status):
    fake = FakeRD([{"status": status}], LINKS)
    with pytest.raises(rd.RealDebridDead):
        rd.fetch(client(fake), MAGNET, tmp_path, download=downloader(BLOBS))
    assert fake.deleted == ["TID1"]


def test_401_is_auth_error():
    fake = FakeRD([])
    fake.responses = [(401, b'{"error": "bad_token", "error_code": 8}')]
    with pytest.raises(rd.RealDebridAuthError) as ei:
        client(fake).user()
    assert ei.value.status == 401 and ei.value.error_code == 8
    fake.responses = [(403, b'{"error": "permission_denied", "error_code": 9}')]
    with pytest.raises(rd.RealDebridAuthError):
        client(fake).add_magnet(MAGNET)


def test_429_retry_then_success():
    fake = FakeRD([])
    fake.responses = [(429, b""), (429, b""),
                      (200, b'{"username": "u", "type": "premium"}')]
    assert client(fake).user()["type"] == "premium"
    assert len(fake.calls) == 3


def test_429_gives_up_after_three_retries():
    fake = FakeRD([])
    fake.responses = [(429, b"")] * 5
    with pytest.raises(rd.RealDebridError) as ei:
        client(fake).user()
    assert ei.value.status == 429 and len(fake.calls) == 4


def test_resume_with_range_when_part_exists(tmp_path):
    (tmp_path / "Movie.2020.mkv.part").write_bytes(b"01234")
    fake = FakeRD([single_file_info()], LINKS)
    rec = []
    rd.fetch(client(fake), MAGNET, tmp_path, download=downloader(BLOBS, rec))
    assert rec == [("https://dl/1", 5)]
    assert (tmp_path / "Movie.2020.mkv").read_bytes() == b"0123456789"


def test_resume_when_server_ignores_range_restarts(tmp_path):
    (tmp_path / "Movie.2020.mkv.part").write_bytes(b"XXXXX")

    def dl(url, start):
        return 200, iter([BLOBS[url]])
    fake = FakeRD([single_file_info()], LINKS)
    rd.fetch(client(fake), MAGNET, tmp_path, download=dl)
    assert (tmp_path / "Movie.2020.mkv").read_bytes() == b"0123456789"


def test_size_mismatch_raises(tmp_path):
    fake = FakeRD([single_file_info()], LINKS)
    with pytest.raises(rd.RealDebridError):
        rd.fetch(client(fake), MAGNET, tmp_path,
                 download=downloader({"https://dl/1": b"short"}))
    assert not (tmp_path / "Movie.2020.mkv").exists()


def test_cancel_mid_download_keeps_part_and_deletes_torrent(tmp_path):
    fake = FakeRD([single_file_info()], LINKS)
    seen = {"n": 0}

    def should_cancel():
        seen["n"] += 1
        return seen["n"] > 3  # poll check, then a couple of chunks

    with pytest.raises(rd.RealDebridCancelled):
        rd.fetch(client(fake), MAGNET, tmp_path, download=downloader(BLOBS),
                 should_cancel=should_cancel)
    assert fake.deleted == ["TID1"]
    assert (tmp_path / "Movie.2020.mkv.part").exists()
    assert not (tmp_path / "Movie.2020.mkv").exists()


def test_cancel_during_polling(tmp_path):
    fake = FakeRD([{"status": "downloading", "progress": 5}], LINKS)
    with pytest.raises(rd.RealDebridCancelled):
        rd.fetch(client(fake), MAGNET, tmp_path, should_cancel=lambda: True,
                 download=downloader(BLOBS))
    assert fake.deleted == ["TID1"]


def test_path_traversal_is_neutralised(tmp_path):
    info = single_file_info(files=[{"id": 1, "path": "/../../evil.mkv",
                                    "bytes": 10, "selected": 1}])
    fake = FakeRD([info], LINKS)
    res = rd.fetch(client(fake), MAGNET, tmp_path, download=downloader(BLOBS))
    assert res.rel_paths == ["evil.mkv"] and (tmp_path / "evil.mkv").exists()


def test_token_never_leaks(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c = client(FakeRD([]))
    assert TOKEN not in repr(c) and TOKEN not in str(c)

    def boom(method, url, headers, data, timeout):
        raise OSError(f"connection reset, sent Bearer {TOKEN}")

    bad = rd.RealDebridClient(TOKEN, transport=boom)
    with pytest.raises(rd.RealDebridError) as ei:
        bad.user()
    assert TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)

    fake = FakeRD([])
    fake.responses = [(401, f'{{"error": "bad {TOKEN}", "error_code": 8}}'.encode())]
    with pytest.raises(rd.RealDebridAuthError) as ei2:
        client(fake).user()
    assert TOKEN not in str(ei2.value)

    fake2 = FakeRD([single_file_info()], LINKS)
    rd.fetch(client(fake2), MAGNET, tmp_path, download=downloader(BLOBS))
    assert TOKEN not in caplog.text


def test_pack_subset_lands_under_the_torrent_name_not_the_file_name(tmp_path):
    """Seen live 2026-10-10: with one file of a pack selected, RD's
    "filename" is that file's name; "original_filename" is the torrent's.
    Staging must mirror webtorrent: <torrent name>/<path in torrent>."""
    done = {"id": "TID1", "filename": "Show S06E12.mkv",
            "original_filename": "Show Complete Series", "status": "downloaded",
            "progress": 100,
            "files": [{"id": 1, "path": "/S06/Show S06E11.mkv", "bytes": 4, "selected": 0},
                      {"id": 2, "path": "/S06/Show S06E12.mkv", "bytes": 4, "selected": 1}],
            "links": ["A"]}
    fake = FakeRD([done], {"A": {"filesize": 4, "download": "https://dl/a",
                                 "filename": "Show S06E12.mkv"}})
    clock = Clock()
    res = rd.fetch(client(fake), MAGNET, tmp_path,
                   download=downloader({"https://dl/a": b"abcd"}),
                   sleep=clock.sleep, monotonic=clock.now)
    assert res.rel_paths == ["Show Complete Series/S06/Show S06E12.mkv"]


def test_a_stream_that_ends_short_resumes_instead_of_failing(tmp_path):
    """Live 2026-10-10: a 583 MB file arrived 7,048 bytes short and the whole
    download was thrown away. It must resume with a Range request."""
    fake = FakeRD([single_file_info()], LINKS)
    calls = []

    def flaky(url, start):
        calls.append(start)
        data = BLOBS[url]
        if len(calls) == 1:
            return 200, iter([data[:6]])          # connection ends early
        return 206, iter([data[start:]])
    clock = Clock()
    res = rd.fetch(client(fake), MAGNET, tmp_path, download=flaky,
                   sleep=clock.sleep, monotonic=clock.now)
    assert calls == [0, 6]
    assert (tmp_path / res.rel_paths[0]).read_bytes() == b"0123456789"


def test_a_dropped_connection_mid_stream_resumes(tmp_path):
    fake = FakeRD([single_file_info()], LINKS)
    calls = []

    def dropping(url, start):
        calls.append(start)
        data = BLOBS[url]
        if len(calls) == 1:
            def gen():
                yield data[:4]
                raise ConnectionResetError("reset by peer")
            return 200, gen()
        return 206, iter([data[start:]])
    clock = Clock()
    res = rd.fetch(client(fake), MAGNET, tmp_path, download=dropping,
                   sleep=clock.sleep, monotonic=clock.now)
    assert calls == [0, 4]
    assert (tmp_path / res.rel_paths[0]).read_bytes() == b"0123456789"
