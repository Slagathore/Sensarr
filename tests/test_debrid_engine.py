# =============================================================================
# The Real-Debrid engine inside DownloadManager: routing, completion contract,
# dead-swarm failure, service-failure fallback, cancel ownership, slot count.
# =============================================================================

import json

import pytest

import config
import db
import download_manager
import downloads_store
import realdebrid
from download_manager import DownloadManager, _DebridHandle, _debrid_episode_chooser

IH = "e" * 40


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("downloads", "download_history", "failed_grabs",
                      "request_downloads", "download_files"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    monkeypatch.setattr(config, "QBITTORRENT_ENABLED", False)
    monkeypatch.setattr(config, "REALDEBRID_API_TOKEN", "rd-token-xyz")
    monkeypatch.setattr(config, "REALDEBRID_FALLBACK_TO_P2P", True)
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path))
    yield


def _row(tmp_path, *, want=None, season=None, episode=None, failure_key=None):
    did = downloads_store.create_download(
        title="Futurama.S06E12.1080p", magnet=f"magnet:?xt=urn:btih:{IH}",
        source="torrentio", media_type="tv", request_id=None,
        staging_dir=str(tmp_path), planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False,
        season=season, episode=episode, failure_key=failure_key,
        want_json=json.dumps(want) if want else None)
    downloads_store.set_status(did, "downloading")
    return did


def _dm(monkeypatch, node_calls, post_calls):
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(dm, "_run_download_node",
                        lambda *a, **k: node_calls.append(a))
    monkeypatch.setattr(dm, "_finish_post_process",
                        lambda did, **k: post_calls.append(did) or "moved")
    return dm


def test_cached_release_completes_through_real_debrid(monkeypatch, tmp_path):
    node, post, seen = [], [], {}
    dm = _dm(monkeypatch, node, post)
    did = _row(tmp_path, want={"file_idx": 87, "file_name": "Futurama.S06E12.mkv"},
               season=6, episode=12)

    def fake_fetch(client, magnet, dest, **kw):
        seen.update(kw, magnet=magnet, dest=dest, client=repr(client))
        kw["on_progress"]("local", 0.5)
        (dest / "Futurama.S06E12.mkv").write_bytes(b"x")
        return realdebrid.FetchResult(["Futurama.S06E12.mkv"], "T1", True, 1)
    monkeypatch.setattr(realdebrid, "fetch", fake_fetch)

    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)

    row = downloads_store.get_download(did)
    assert row.status == "downloaded" and row.progress == 1.0
    assert json.loads(row.files_json) == ["Futurama.S06E12.mkv"]
    assert post == [did] and node == []
    # The pinned file reaches RD; no episode chooser is needed then.
    assert seen["want_file_idx"] == 87
    assert seen["want_file_name"] == "Futurama.S06E12.mkv"
    assert seen["chooser"] is None
    assert "rd-token-xyz" not in seen["client"]
    history = [h.after_value or "" for h in downloads_store.history_for_download(did)]
    assert any("via Real-Debrid, cached" in h for h in history)
    assert did not in dm._processes


def test_dead_swarm_fails_without_a_second_15_minutes_on_p2p(monkeypatch, tmp_path):
    node, post = [], []
    dm = _dm(monkeypatch, node, post)
    did = _row(tmp_path, failure_key="ep:7:6:12")

    def stalled(*a, **k):
        raise realdebrid.RealDebridStalled("No progress for 900s (downloading)")
    monkeypatch.setattr(realdebrid, "fetch", stalled)
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)

    row = downloads_store.get_download(did)
    assert row.status == "error" and row.error.startswith("stalled")
    assert downloads_store.display_status(row.status, row.progress,
                                          error=row.error) == "stalled"
    assert node == []
    # The failure is remembered so the next pass picks a different copy.
    assert IH in downloads_store.failed_grab_info("ep:7:6:12")


def test_service_failure_falls_back_to_the_torrent_client(monkeypatch, tmp_path):
    node, post = [], []
    dm = _dm(monkeypatch, node, post)
    did = _row(tmp_path)

    def refused(*a, **k):
        raise realdebrid.RealDebridAuthError("Real-Debrid GET /user: bad_token", 401)
    monkeypatch.setattr(realdebrid, "fetch", refused)
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)

    assert len(node) == 1
    actions = [h.action for h in downloads_store.history_for_download(did)]
    assert "debrid_fallback" in actions
    assert downloads_store.get_download(did).status == "downloading"


def test_service_failure_without_fallback_errors_the_row(monkeypatch, tmp_path):
    node, post = [], []
    dm = _dm(monkeypatch, node, post)
    monkeypatch.setattr(config, "REALDEBRID_FALLBACK_TO_P2P", False)
    did = _row(tmp_path)
    monkeypatch.setattr(realdebrid, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        realdebrid.RealDebridError("Real-Debrid POST /torrents/addMagnet: HTTP 503", 503)))
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)
    row = downloads_store.get_download(did)
    assert row.status == "error" and "Real-Debrid failed" in row.error
    assert node == []


def test_a_cancel_mid_fetch_leaves_the_row_to_whoever_cancelled(monkeypatch, tmp_path):
    node, post = [], []
    dm = _dm(monkeypatch, node, post)
    did = _row(tmp_path)

    def cancelled_by_user(client, magnet, dest, **kw):
        # The user hit Cancel: DownloadManager.cancel kills the handle and
        # writes 'cancelled'; the fetch notices and stops.
        assert dm.cancel(did) is True
        assert kw["should_cancel"]() is True
        kw["on_progress"]("remote", 0.3)       # must NOT flip the row back
        raise realdebrid.RealDebridCancelled("Cancelled")
    monkeypatch.setattr(realdebrid, "fetch", cancelled_by_user)
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)

    assert downloads_store.get_download(did).status == "cancelled"
    assert node == [] and post == []


def test_no_token_means_the_torrent_client(monkeypatch, tmp_path):
    node, post = [], []
    dm = _dm(monkeypatch, node, post)
    monkeypatch.setattr(config, "REALDEBRID_API_TOKEN", "")
    monkeypatch.setattr(realdebrid, "fetch",
                        lambda *a, **k: pytest.fail("RD must not run"))
    did = _row(tmp_path)
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)
    assert len(node) == 1


def test_debrid_handle_counts_as_a_running_slot(monkeypatch):
    dm = DownloadManager()
    handle = _DebridHandle()
    dm._processes[42] = handle
    assert dm._active_node_count() == 1
    handle.kill()
    assert handle.cancelled() and handle.poll() is None
    handle.returncode = 0
    assert dm._active_node_count() == 0


def test_episode_chooser_picks_the_wanted_file_from_a_pack():
    choose = _debrid_episode_chooser(6, 12)
    files = [{"id": 1, "path": "/Futurama S06/Futurama.S06E11.mkv"},
             {"id": 2, "path": "/Futurama S06/Futurama.S06E12.mkv"},
             {"id": 3, "path": "/Futurama S06/Futurama 6x12 extras.mkv"},
             {"id": 4, "path": "/Futurama S07/Futurama.S07E12.mkv"}]
    assert choose(files) == [2, 3]
    assert _debrid_episode_chooser(None, None) is None
    assert choose([{"id": 1, "path": "/movie.mkv"}]) == []


def test_cached_low_seed_release_does_not_fall_back_to_p2p(monkeypatch, tmp_path):
    """Verifier finding: a release picked past the seeder floor only because
    RD had it cached would just stall on the torrent client."""
    node, post = [], []
    dm = _dm(monkeypatch, node, post)
    monkeypatch.setattr(config, "MIN_SEEDERS", 4)
    did = _row(tmp_path, want={"debrid_cached": True, "seeders": 0})
    monkeypatch.setattr(realdebrid, "fetch", lambda *a, **k: (_ for _ in ()).throw(
        realdebrid.RealDebridError("Real-Debrid POST /unrestrict/link: HTTP 503", 503)))
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)
    row = downloads_store.get_download(did)
    assert node == [] and row.status == "error"
    assert "Real-Debrid-only" in row.error
    # The service failed, not the release: no failure is held against it.
    assert downloads_store.is_release_fault(row.error) is False


def test_progress_writes_are_throttled(monkeypatch, tmp_path):
    node, post, writes = [], [], []
    dm = _dm(monkeypatch, node, post)
    did = _row(tmp_path)
    real = downloads_store.set_progress
    monkeypatch.setattr(downloads_store, "set_progress",
                        lambda d, f: writes.append(f) or real(d, f))

    def chatty(client, magnet, dest, **kw):
        for i in range(1000):                 # 1000 chunks, 0.1% apart
            kw["on_progress"]("local", i / 1000)
        (dest / "a.mkv").write_bytes(b"x")
        return realdebrid.FetchResult(["a.mkv"], "T", True, 1)
    monkeypatch.setattr(realdebrid, "fetch", chatty)
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)
    assert len(writes) < 250                 # about one per 0.5%
    assert downloads_store.get_download(did).status == "downloaded"


def test_a_late_progress_tick_never_resurrects_a_settled_row(tmp_path):
    did = _row(tmp_path)
    downloads_store.set_status(did, "cancelled", completed=True)
    downloads_store.set_progress(did, 0.4)
    row = downloads_store.get_download(did)
    assert row.status == "cancelled" and row.progress == 0.4
    downloads_store.set_status(did, "queued")
    downloads_store.set_progress(did, 0.5)
    assert downloads_store.get_download(did).status == "downloading"


def test_real_debrid_publishes_its_file_list_before_writing(monkeypatch, tmp_path):
    """So cleanup elsewhere never mistakes RD's in-progress files for orphans."""
    node, post, order = [], [], []
    dm = _dm(monkeypatch, node, post)
    did = _row(tmp_path)
    real_set_files = downloads_store.set_files
    monkeypatch.setattr(downloads_store, "set_files",
                        lambda d, rels: order.append(("files", list(rels))) or real_set_files(d, rels))

    def fetch(client, magnet, dest, **kw):
        kw["on_files"](["Pack/E01.mkv"])
        order.append(("write",))
        (dest / "Pack").mkdir()
        (dest / "Pack" / "E01.mkv").write_bytes(b"x")
        return realdebrid.FetchResult(["Pack/E01.mkv"], "T", True, 1)
    monkeypatch.setattr(realdebrid, "fetch", fetch)
    dm._run_download(did, f"magnet:?xt=urn:btih:{IH}", str(tmp_path), None)
    assert order[0] == ("files", ["Pack/E01.mkv"]) and order[1] == ("write",)


def test_a_runner_we_killed_writes_neither_progress_nor_an_error(monkeypatch, tmp_path):
    """Verifier N7: after a rotation requeued the row, a late progress tick
    flipped it to 'downloading' and the thread then wrote 'runner exit code'
    as an error against the release."""
    monkeypatch.setattr(config, "REALDEBRID_API_TOKEN", "")
    dm = DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    monkeypatch.setattr(download_manager, "ensure_runner_ready", lambda: (True, "ok"))
    did = _row(tmp_path)

    class _FakeProc:
        returncode = 1
        def __init__(self):
            self.stdout = iter(['{"event": "progress", "progress": 0.3}\n'])
        def poll(self):
            return 1
        def wait(self, timeout=None):
            return 1
        def kill(self):
            pass

    def popen(*a, **k):
        # The monitor rotates the download while its last line is in flight.
        dm._stopped_by_owner.add(did)
        downloads_store.set_status(did, "queued")
        return _FakeProc()
    monkeypatch.setattr(download_manager.subprocess, "Popen", popen)
    dm._run_download_node(did, "magnet:?xt=urn:btih:" + IH, str(tmp_path), None)
    row = downloads_store.get_download(did)
    assert row.status == "queued"
    assert IH not in downloads_store.failed_grab_info("x")
    assert not any(h.action == "error" for h in downloads_store.history_for_download(did))
