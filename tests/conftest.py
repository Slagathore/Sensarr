import atexit
import logging
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

# IMPORTANT: this runs at conftest IMPORT time, before pytest imports any test
# module. config.py resolves APP_DB_PATH the moment it is first imported, so
# the env var must be set here at module level — a session fixture is too late
# and the tests would write into the real application database.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="prb-tests-")


def _remove_test_dir() -> None:
    # Close the log file first or Windows keeps the folder; 98 of these had
    # piled up in %TEMP% by 2026-10.
    logging.shutdown()
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


atexit.register(_remove_test_dir)
os.environ["APP_DB_PATH"] = str(Path(_TEST_DB_DIR) / "test_app.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")
# Task H: pin every app_paths contract dir to the temp tree too, so tests
# never write caches/locks into the repo (Windows legacy layout) or the
# real ~/.config//~/.cache (Linux XDG layout). Setting SENSARR_CONFIG_DIR
# also stops config.py from loading a developer's real .env — tests see the
# same clean-environment defaults CI sees.
for _key, _sub in (("SENSARR_CONFIG_DIR", "config"),
                   ("SENSARR_DATA_DIR", "data"),
                   ("SENSARR_CACHE_DIR", "cache"),
                   ("SENSARR_RUNTIME_DIR", "runtime")):
    os.environ[_key] = str(Path(_TEST_DB_DIR) / _sub)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to make a real outbound network connection.

    A subclass of RuntimeError on purpose: several lookup helpers (e.g.
    media_lookup.get_tmdb_movie_runtime) already catch RuntimeError around
    their network call and degrade to None/[] — that same handling now
    degrades a blocked-network attempt exactly like a real connection
    failure, just instantly instead of after a 10-15s urlopen timeout.
    """


_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def _guard_address(address) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("utf-8", "ignore")
    if host not in _ALLOWED_HOSTS:
        raise NetworkBlockedError(
            f"Outbound network connect blocked in tests: destination={address!r}. "
            "Mock the real seam instead (search_collect/search_torrents, "
            "media_lookup._get_json, the ollama client, requests/urllib calls, "
            "…) rather than letting the test hit the network."
        )


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Block every non-localhost socket connect for every test.

    Phase 3 wired several automatic decision paths straight onto
    torrent_search.search_collect and media_lookup's TMDB/TVDB/Jikan HTTP
    calls. A test that forgets to mock one of those used to hit the real
    network and burn a full urlopen timeout (10-15s) per call — this turns
    that into an immediate, clearly-named failure (or, where the code already
    catches RuntimeError around the lookup, an instant graceful fallback)
    instead of a slow or flaky run. localhost/127.0.0.1 stays open (Ollama,
    qBittorrent, Plex are all expected to run locally).
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        _guard_address(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _guard_address(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _no_download_manager_background_work(monkeypatch):
    """Skip DownloadManager startup recovery and monitor daemons in tests.

    DownloadManager.__init__ -> _recover_previous_session() calls
    psutil.process_iter(['name', 'cmdline']) over every running process to
    find leaked Node torrent-runner children from a previous app session. On
    a normal dev machine (hundreds of processes) that alone costs ~10s of
    wall clock — real OS work, not network, so the socket guard above can't
    catch it. No test exercises the real orphan-sweep behavior (it's a
    startup-only safety net), so it's a no-op here by default.

    A manager also starts a 60-second queue-monitor daemon. Tests construct
    many managers against one temporary DB, so old daemons can wake during a
    later test and restart its deliberately queued row. Monitor decisions are
    invoked directly by their unit tests; no background thread is needed.
    """
    try:
        import download_manager
    except Exception:
        return
    monkeypatch.setattr(
        download_manager.DownloadManager, "_recover_previous_session",
        lambda self: None, raising=False)
    monkeypatch.setattr(
        download_manager.DownloadManager, "_QUEUE_MONITOR_AUTOSTART",
        False, raising=False)


@pytest.fixture(autouse=True)
def _no_airing_network(monkeypatch):
    """Stop show sync from hitting the network for airing/episode fallbacks.

    sync_show() resolves a TMDB id + next-episode-to-air and can fall back to
    TMDB episode lists or AniList airing — all over the network. Tests that
    care override the specific call they exercise.
    """
    try:
        import show_tracker
    except Exception:
        return
    for name in ("resolve_tmdb_tv_id", "get_tmdb_next_air", "find_tmdb_tv_by_tvdb"):
        monkeypatch.setattr(show_tracker, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(show_tracker, "get_tmdb_tv_episodes", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(show_tracker, "get_anime_airing", lambda *a, **k: (None, ""), raising=False)
