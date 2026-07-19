# =============================================================================
# Task H — Linux support: path contract, platform adapter, legacy migration,
# POSIX-safe routing/probing, updater capability.
#
# The Windows-layout tests are the regression fence for "WINDOWS BEHAVIOR
# MUST NOT CHANGE": compute_paths('win32', {}) must equal the exact
# pre-Task-H APP_DIR-derived layout, byte for byte.
# =============================================================================

import errno
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import app_paths
import legacy_migration
import platform_adapter
import torrent_routing
import updater
import video_quality

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# app_paths — Windows layout byte-identical to the pre-change layout
# ---------------------------------------------------------------------------

class TestWin32Layout:
    def test_all_dirs_equal_install_dir(self):
        p = app_paths.compute_paths("win32", {})
        install = app_paths._install_dir()
        assert p.install_dir == install
        assert p.config_dir == install          # .env beside the exe
        assert p.data_dir == install            # plex_reset_button.db beside the exe
        assert p.cache_dir == install           # caches beside the exe
        assert p.runtime_dir == install         # sensarr.pid beside the exe
        assert p.download_dir == install / "torrent_staging"

    def test_legacy_derivations_reproduced_exactly(self):
        """The exact paths modules used to build with `APP_DIR / ...`."""
        p = app_paths.compute_paths("win32", {})
        legacy = app_paths._install_dir()
        assert p.config_dir / ".env" == legacy / ".env"
        assert p.data_dir / "plex_reset_button.db" == legacy / "plex_reset_button.db"
        assert p.runtime_dir / "sensarr.pid" == legacy / "sensarr.pid"
        assert p.cache_dir / "trackers_cache.txt" == legacy / "trackers_cache.txt"
        assert p.cache_dir / "maintenance_cache.json" == legacy / "maintenance_cache.json"
        assert p.data_dir / "anime_meta.sqlite" == legacy / "anime_meta.sqlite"

    def test_xdg_env_is_ignored_on_win32(self):
        env = {"XDG_CONFIG_HOME": "/xdg/config", "XDG_DATA_HOME": "/xdg/data",
               "XDG_CACHE_HOME": "/xdg/cache", "XDG_RUNTIME_DIR": "/xdg/run"}
        p = app_paths.compute_paths("win32", env)
        assert p.config_dir == app_paths._install_dir()

    def test_win32_plan_is_always_empty(self, tmp_path):
        """Windows users are never relocated: the migration plan is empty by
        definition on win32."""
        assert legacy_migration.plan_migration(platform="win32") == []


class TestLinuxLayout:
    def test_xdg_env_respected(self):
        env = {"XDG_CONFIG_HOME": "/xdg/config", "XDG_DATA_HOME": "/xdg/data",
               "XDG_CACHE_HOME": "/xdg/cache", "XDG_RUNTIME_DIR": "/xdg/run",
               "HOME": "/home/user"}
        p = app_paths.compute_paths("linux", env)
        assert p.config_dir == Path("/xdg/config/sensarr")
        assert p.data_dir == Path("/xdg/data/sensarr")
        assert p.cache_dir == Path("/xdg/cache/sensarr")
        assert p.runtime_dir == Path("/xdg/run/sensarr")
        assert p.download_dir == Path("/xdg/data/sensarr/torrent_staging")

    def test_documented_home_fallbacks(self):
        env = {"HOME": "/home/cole"}
        p = app_paths.compute_paths("linux", env)
        assert p.config_dir == Path("/home/cole/.config/sensarr")
        assert p.data_dir == Path("/home/cole/.local/share/sensarr")
        assert p.cache_dir == Path("/home/cole/.cache/sensarr")
        # No XDG_RUNTIME_DIR (headless/CI): documented cache-tree fallback.
        assert p.runtime_dir == Path("/home/cole/.cache/sensarr/runtime")

    def test_sensarr_dir_overrides_win_everywhere(self):
        env = {"HOME": "/home/cole",
               "SENSARR_CONFIG_DIR": "/pin/c", "SENSARR_DATA_DIR": "/pin/d",
               "SENSARR_CACHE_DIR": "/pin/k", "SENSARR_RUNTIME_DIR": "/pin/r"}
        for platform in ("linux", "win32"):
            p = app_paths.compute_paths(platform, env)
            assert p.config_dir == Path("/pin/c")
            assert p.data_dir == Path("/pin/d")
            assert p.cache_dir == Path("/pin/k")
            assert p.runtime_dir == Path("/pin/r")

    def test_torrent_download_dir_override_respected(self):
        env = {"HOME": "/home/cole", "TORRENT_DOWNLOAD_DIR": "/mnt/big/staging"}
        p = app_paths.compute_paths("linux", env)
        assert p.download_dir == Path("/mnt/big/staging")

    def test_fresh_dirs_created_user_only(self, tmp_path):
        p = app_paths.AppPaths(
            bundle_dir=tmp_path, install_dir=tmp_path,
            config_dir=tmp_path / "c", data_dir=tmp_path / "d",
            cache_dir=tmp_path / "k", runtime_dir=tmp_path / "r",
            download_dir=tmp_path / "dl")
        app_paths.ensure_dirs(p)
        for d in (p.config_dir, p.data_dir, p.cache_dir, p.runtime_dir):
            assert d.is_dir()
            if sys.platform != "win32":  # mode bits are advisory on Windows
                assert (d.stat().st_mode & 0o777) == 0o700
        # Download dir stays on-demand, exactly as before.
        assert not p.download_dir.exists()


def test_env_overrides_reach_config():
    """APP_DB_PATH (set by conftest) must still decide config.APP_DB_PATH —
    every explicit path override keeps working through the refactor."""
    import config
    assert config.APP_DB_PATH == os.environ["APP_DB_PATH"]
    import db
    assert db.db_path() == Path(os.environ["APP_DB_PATH"])


def test_no_module_computes_app_dir_paths():
    """The grep gate from the spec: after Task H, no module derives its own
    writable path with `APP_DIR / ...` — app_paths owns every path decision.
    (config.APP_DIR itself remains as a read-only install-dir export.)"""
    pattern = re.compile(r"APP_DIR\s*(?:\)\s*)?/")
    offenders = []
    for py in REPO.glob("*.py"):
        if py.name == "app_paths.py":
            continue
        for lineno, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "modules still compute APP_DIR-relative paths:\n" + "\n".join(offenders))


# ---------------------------------------------------------------------------
# platform_adapter — capability flags per (monkeypatched) sys.platform
# ---------------------------------------------------------------------------

class TestAdapterCapabilities:
    def test_winget_is_windows_only(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert platform_adapter.supports_winget() is True
        monkeypatch.setattr(sys, "platform", "linux")
        assert platform_adapter.supports_winget() is False

    def test_updater_capability_matrix(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        caps = platform_adapter.updater_capability()
        assert caps.kind == "windows-self-update" and caps.can_self_update

        monkeypatch.setattr(sys, "frozen", False, raising=False)
        caps = platform_adapter.updater_capability()
        assert not caps.can_self_update and "git pull" in caps.hint

        monkeypatch.setattr(sys, "platform", "linux")
        caps = platform_adapter.updater_capability()
        assert caps.kind == "linux-source" and not caps.can_self_update
        assert "git pull" in caps.hint

        monkeypatch.setattr(sys, "frozen", True, raising=False)
        caps = platform_adapter.updater_capability()
        assert caps.kind == "linux-packaged" and not caps.can_self_update
        assert "artifact" in caps.hint  # honest "install the new build" text

    def test_linux_guidance_never_runs_a_package_manager(self, monkeypatch):
        """No subprocess AT ALL during guidance. The recording patches sit on
        the shared subprocess module, so this also catches transitive spawns
        like pystray's `uname -p` import side effect (round 2 B1) — which is
        why guidance must probe with find_spec, never import."""
        monkeypatch.setattr(sys, "platform", "linux")
        calls = []
        monkeypatch.setattr(platform_adapter.subprocess, "run",
                            lambda *a, **k: calls.append(a))
        monkeypatch.setattr(platform_adapter.subprocess, "Popen",
                            lambda *a, **k: calls.append(a))
        monkeypatch.setattr(platform_adapter.subprocess, "check_output",
                            lambda *a, **k: calls.append(a))
        text = platform_adapter.dependency_install_guidance()
        assert calls == []
        assert "sudo apt install" in text          # shown, not executed
        assert "never asks for root" in text
        assert "python3-tk" in text and "ffmpeg" in text and "xdg-utils" in text

    def test_tray_probe_never_imports_pystray(self, monkeypatch):
        """The diagnostics path must be side-effect-free: importing pystray
        spawns `uname -p` on Linux, so tray_probe may only find_spec it."""
        import builtins
        real_import = builtins.__import__

        def guard(name, *args, **kwargs):
            if name == "pystray" or name.startswith("pystray."):
                raise AssertionError("tray_probe imported pystray")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guard)
        ok, reason = platform_adapter.tray_probe()   # must not raise
        assert isinstance(ok, bool) and isinstance(reason, str)

    def test_tray_probe_reports_missing_packages(self, monkeypatch):
        import importlib.util
        monkeypatch.setattr(importlib.util, "find_spec", lambda _n: None)
        ok, reason = platform_adapter.tray_probe()
        assert ok is False and "pystray" in reason

    def test_open_path_linux_without_xdg_open(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(platform_adapter.shutil, "which", lambda _n: None)
        assert platform_adapter.open_path(tmp_path) is False

    def test_open_path_linux_uses_xdg_open(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(platform_adapter.shutil, "which",
                            lambda _n: "/usr/bin/xdg-open")
        spawned = {}

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd

        monkeypatch.setattr(platform_adapter.subprocess, "Popen", fake_popen)
        assert platform_adapter.open_path(tmp_path) is True
        assert spawned["cmd"] == ["/usr/bin/xdg-open", str(tmp_path)]

    def test_duplicate_dialog_falls_back_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setitem(sys.modules, "tkinter", None)  # force ImportError
        platform_adapter.show_duplicate_instance_message("already running")
        assert "already running" in capsys.readouterr().err


class TestProcessControl:
    def test_terminate_process_tree_kills_parent_and_child(self):
        """Spawned fixture only — never anyone's real Plex."""
        import psutil
        parent = subprocess.Popen([
            sys.executable, "-c",
            "import subprocess, sys, time;"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            "time.sleep(60)",
        ])
        try:
            proc = psutil.Process(parent.pid)
            deadline = 50
            while not proc.children() and deadline:
                import time
                time.sleep(0.1)
                deadline -= 1
            children = proc.children(recursive=True)
            assert children, "fixture child never appeared"
            ok, detail = platform_adapter.terminate_process_tree(
                parent.pid, timeout=10.0)
            assert ok, detail
            _gone, alive = psutil.wait_procs([proc] + children, timeout=10)
            assert not alive
        finally:
            if parent.poll() is None:
                parent.kill()

    def test_terminate_missing_pid_is_already_exited(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        ok, detail = platform_adapter.terminate_process_tree(proc.pid)
        assert ok and detail == "already exited"

    def test_plex_stop_routes_through_adapter_on_linux(self, monkeypatch):
        import plex_control
        monkeypatch.setattr(sys, "platform", "linux")
        called = {}

        def fake_terminate(pid, **_kwargs):
            called["pid"] = pid
            return True, "terminated"

        monkeypatch.setattr(platform_adapter, "terminate_process_tree",
                            fake_terminate)

        from typing import Any, cast

        class FakeProc:
            pid = 12345
        ok, _detail = plex_control._stop_process_tree(cast(Any, FakeProc()))
        assert ok and called["pid"] == 12345

    def test_local_control_always_available_on_windows(self, monkeypatch):
        import plex_control
        monkeypatch.setattr(sys, "platform", "win32")
        ok, _reason = plex_control.local_control_available()
        assert ok is True

    def test_local_control_remote_only_linux_is_disabled_with_reason(
            self, monkeypatch):
        import config
        import plex_control
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(config, "PLEX_MEDIA_SERVER_PATH", "")
        monkeypatch.setattr(plex_control, "_is_plex_gone", lambda: True)
        ok, reason = plex_control.local_control_available()
        assert ok is False
        assert "PLEX_MEDIA_SERVER_PATH" in reason  # explains how to enable

    def test_local_control_enabled_by_configured_executable(
            self, monkeypatch, tmp_path):
        import config
        import plex_control
        exe = tmp_path / "Plex Media Server"
        exe.write_text("#!/bin/sh\n")
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(config, "PLEX_MEDIA_SERVER_PATH", str(exe))
        ok, _reason = plex_control.local_control_available()
        assert ok is True


# ---------------------------------------------------------------------------
# Legacy migration — dry run, hash-verified copy, idempotent resume
# ---------------------------------------------------------------------------

def _fake_paths(tmp_path: Path) -> app_paths.AppPaths:
    return app_paths.AppPaths(
        bundle_dir=tmp_path / "install", install_dir=tmp_path / "install",
        config_dir=tmp_path / "config", data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache", runtime_dir=tmp_path / "runtime",
        download_dir=tmp_path / "data" / "downloads")


def _seed_legacy(install: Path) -> None:
    install.mkdir(parents=True, exist_ok=True)
    (install / ".env").write_text("PLEX_TOKEN=abc\n", encoding="utf-8")
    conn = sqlite3.connect(install / "plex_reset_button.db")
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO requests (title) VALUES ('Severance')")
    conn.commit()
    conn.close()
    (install / "trackers_cache.txt").write_text("udp://x\n", encoding="utf-8")
    (install / "sensarr.pid").write_text("123", encoding="ascii")


class TestLegacyMigration:
    def test_dry_run_plan_names_exact_src_and_dest(self, tmp_path):
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        items = legacy_migration.plan_migration(paths, platform="linux")
        by_name = {Path(i.src).name: i for i in items}
        assert set(by_name) == {".env", "plex_reset_button.db",
                                "trackers_cache.txt"}  # pid lock never moves
        assert by_name[".env"].dest == str(paths.config_dir / ".env")
        assert by_name["plex_reset_button.db"].dest == str(
            paths.data_dir / "plex_reset_button.db")
        assert by_name["trackers_cache.txt"].dest == str(
            paths.cache_dir / "trackers_cache.txt")
        text = legacy_migration.format_plan(items)
        assert str(paths.install_dir / ".env") in text
        assert str(paths.config_dir / ".env") in text
        # The plan changed nothing on disk.
        assert (paths.install_dir / ".env").is_file()
        assert not paths.config_dir.exists()

    def test_plan_includes_pre_rename_xdg_dirs(self, tmp_path):
        # A pre-rename Linux install left its data under ~/.config/plexxarr
        # and ~/.local/share/plexxarr — the plan moves it to the new names.
        paths = _fake_paths(tmp_path)
        paths.install_dir.mkdir(parents=True, exist_ok=True)
        env = {"XDG_CONFIG_HOME": str(tmp_path / "xdgc"),
               "XDG_DATA_HOME": str(tmp_path / "xdgd"),
               "XDG_CACHE_HOME": str(tmp_path / "xdgk"),
               "HOME": str(tmp_path)}
        old_config = tmp_path / "xdgc" / "plexxarr"
        old_data = tmp_path / "xdgd" / "plexxarr"
        old_config.mkdir(parents=True)
        old_data.mkdir(parents=True)
        (old_config / ".env").write_text("PLEX_TOKEN=abc\n", encoding="utf-8")
        (old_data / "plex_reset_button.db").write_bytes(b"placeholder")
        items = legacy_migration.plan_migration(paths, platform="linux",
                                                environ=env)
        dests = {Path(i.src).name: i.dest for i in items}
        assert dests[".env"] == str(paths.config_dir / ".env")
        assert dests["plex_reset_button.db"] == str(
            paths.data_dir / "plex_reset_button.db")
        # Old pid locks never migrate, same as the install-dir rule.
        (old_data / "plexxarr.pid").write_text("123", encoding="ascii")
        items = legacy_migration.plan_migration(paths, platform="linux",
                                                environ=env)
        assert "plexxarr.pid" not in {Path(i.src).name for i in items}

    def test_execute_copies_verifies_and_archives(self, tmp_path):
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        items = legacy_migration.plan_migration(paths, platform="linux")
        summary = legacy_migration.execute_migration(items, paths)
        assert summary == {"copied": 3, "skipped": 0, "failed": 0, "total": 3}
        # Copied DB reopens and still holds the data.
        conn = sqlite3.connect(paths.data_dir / "plex_reset_button.db")
        rows = conn.execute("SELECT title FROM requests").fetchall()
        conn.close()
        assert rows == [("Severance",)]
        # Originals archived (never deleted), pid lock untouched.
        assert (paths.install_dir / ".env.migrated").is_file()
        assert not (paths.install_dir / ".env").exists()
        assert (paths.install_dir / "sensarr.pid").is_file()
        # Journal recorded every op as done.
        journal = (paths.data_dir /
                   legacy_migration.JOURNAL_FILE).read_text(encoding="utf-8")
        assert journal.count('"done"') == 3

    def test_rerun_is_idempotent(self, tmp_path):
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        items = legacy_migration.plan_migration(paths, platform="linux")
        legacy_migration.execute_migration(items, paths)
        # Re-running the same items copies nothing and clobbers nothing.
        again = legacy_migration.execute_migration(items, paths)
        assert again["copied"] == 0 and again["failed"] == 0
        assert again["skipped"] == 3
        # And a fresh plan is now empty — the app stops offering it.
        assert legacy_migration.plan_migration(paths, platform="linux") == []

    def test_different_file_at_destination_fails_not_clobbers(self, tmp_path):
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        paths.config_dir.mkdir(parents=True)
        (paths.config_dir / ".env").write_text("PLEX_TOKEN=other\n",
                                               encoding="utf-8")
        items = [i for i in legacy_migration.plan_migration(paths, platform="linux")
                 if Path(i.src).name == ".env"]
        summary = legacy_migration.execute_migration(items, paths)
        assert summary["failed"] == 1
        # Destination untouched, source NOT archived (still needs a human).
        assert (paths.config_dir / ".env").read_text(
            encoding="utf-8") == "PLEX_TOKEN=other\n"
        assert (paths.install_dir / ".env").is_file()

    def test_wal_sibling_is_checkpointed_before_copy(self, tmp_path):
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        db_file = paths.install_dir / "plex_reset_button.db"
        # Leave a live WAL sibling behind (connection still open elsewhere).
        # archive_legacy=False here: on Windows the open handle blocks the
        # rename; the checkpoint+copy is the behavior under test.
        keeper = sqlite3.connect(db_file)
        keeper.execute("PRAGMA journal_mode=WAL")
        keeper.execute("INSERT INTO requests (title) VALUES ('Andor')")
        keeper.commit()
        try:
            assert (paths.install_dir / "plex_reset_button.db-wal").exists()
            items = [i for i in
                     legacy_migration.plan_migration(paths, platform="linux")
                     if Path(i.src).name == "plex_reset_button.db"]
            summary = legacy_migration.execute_migration(
                items, paths, archive_legacy=False)
        finally:
            keeper.close()
        assert summary["failed"] == 0
        conn = sqlite3.connect(paths.data_dir / "plex_reset_button.db")
        titles = {r[0] for r in conn.execute("SELECT title FROM requests")}
        conn.close()
        assert titles == {"Severance", "Andor"}  # WAL content made it across

    def test_hot_wal_with_live_reader_aborts_cleanly(self, tmp_path):
        """Round 2 B2 — the verifier's probe shape: an open writer plus a
        reader pinning a WAL snapshot. The checkpoint result row must be
        read (sqlite3 never raises on a blocked checkpoint), the item must
        FAIL with a 'close the app' message, the source must stay untouched
        and un-renamed, and both live connections must keep working."""
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        db_file = paths.install_dir / "plex_reset_button.db"
        writer = sqlite3.connect(db_file)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO requests (title) VALUES ('Andor')")
        writer.commit()  # committed rows now live in the WAL
        reader = sqlite3.connect(db_file)
        try:
            reader.execute("BEGIN")
            before = reader.execute(
                "SELECT count(*) FROM requests").fetchone()[0]
            assert before == 2  # snapshot pinned — checkpoint cannot TRUNCATE

            items = [i for i in
                     legacy_migration.plan_migration(paths, platform="linux")
                     if Path(i.src).name == "plex_reset_button.db"]
            summary = legacy_migration.execute_migration(items, paths)

            assert summary["failed"] == 1 and summary["copied"] == 0
            # Source intact under its own name; nothing renamed or copied.
            assert db_file.is_file()
            assert not (paths.install_dir /
                        "plex_reset_button.db.migrated").exists()
            assert not (paths.data_dir / "plex_reset_button.db").exists()
            # Journal names the failure honestly.
            journal = (paths.data_dir / legacy_migration.JOURNAL_FILE
                       ).read_text(encoding="utf-8")
            assert '"failed"' in journal and "close the app" in journal
            # Both live connections still work with all their data.
            assert reader.execute(
                "SELECT count(*) FROM requests").fetchone()[0] == 2
            reader.execute("COMMIT")
            writer.execute("INSERT INTO requests (title) VALUES ('Pluribus')")
            writer.commit()
            assert writer.execute(
                "SELECT count(*) FROM requests").fetchone()[0] == 3
        finally:
            reader.close()
            writer.close()
        # With every connection closed, the same plan now succeeds.
        summary = legacy_migration.execute_migration(items, paths)
        assert summary["failed"] == 0 and summary["copied"] == 1
        conn = sqlite3.connect(paths.data_dir / "plex_reset_button.db")
        titles = {r[0] for r in conn.execute("SELECT title FROM requests")}
        conn.close()
        assert titles == {"Severance", "Andor", "Pluribus"}

    def test_empty_destination_db_self_heals(self, tmp_path):
        """Round 2 B3 — simulate the old broken ordering: initialize a fresh
        schema-only DB at the XDG destination FIRST, then migrate. The empty
        file must be archived aside (never deleted) and the legacy database
        must migrate in."""
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        paths.data_dir.mkdir(parents=True)
        dest = paths.data_dir / "plex_reset_button.db"
        conn = sqlite3.connect(dest)  # "initialize_queue_db ran first"
        conn.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("CREATE TABLE downloads (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        items = [i for i in
                 legacy_migration.plan_migration(paths, platform="linux")
                 if Path(i.src).name == "plex_reset_button.db"]
        summary = legacy_migration.execute_migration(items, paths)
        assert summary == {"copied": 1, "skipped": 0, "failed": 0, "total": 1}
        conn = sqlite3.connect(dest)
        rows = conn.execute("SELECT title FROM requests").fetchall()
        conn.close()
        assert rows == [("Severance",)]  # legacy data won
        empties = list(paths.data_dir.glob("plex_reset_button.db.empty-*"))
        assert len(empties) == 1  # the schema-only file was archived, not lost

    def test_destination_db_with_user_rows_still_refuses(self, tmp_path):
        """The self-heal must never touch a destination holding real data."""
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        paths.data_dir.mkdir(parents=True)
        dest = paths.data_dir / "plex_reset_button.db"
        conn = sqlite3.connect(dest)
        conn.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO requests (title) VALUES ('Precious')")
        conn.commit()
        conn.close()

        items = [i for i in
                 legacy_migration.plan_migration(paths, platform="linux")
                 if Path(i.src).name == "plex_reset_button.db"]
        summary = legacy_migration.execute_migration(items, paths)
        assert summary["failed"] == 1 and summary["copied"] == 0
        conn = sqlite3.connect(dest)
        rows = conn.execute("SELECT title FROM requests").fetchall()
        conn.close()
        assert rows == [("Precious",)]  # destination untouched
        assert (paths.install_dir / "plex_reset_button.db").is_file()


class TestMigrationOfferOrdering:
    """Round 2 B3 — the offer must run before any DB initialization."""

    def test_offer_precedes_desktop_app_import_in_main(self):
        """Static fence: in main(), _offer_legacy_migration() is called
        before the desktop_app import that constructs DesktopApp (whose
        __init__ initializes databases at the destination)."""
        src = (REPO / "main.py").read_text(encoding="utf-8")
        body = src[src.index("def main()"):]
        offer_at = body.index("_offer_legacy_migration()")
        import_at = body.index("from desktop_app import run_desktop_app")
        assert offer_at < import_at

    def test_hoisted_offer_migrates_before_any_db_init(self, monkeypatch,
                                                       tmp_path):
        """Drive main._offer_legacy_migration end-to-end with injected
        dialogs against a fake path contract: the legacy DB lands at the
        XDG destination with its data intact — no schema-only file ever
        got in the way because nothing initialized the destination."""
        import main as main_module
        paths = _fake_paths(tmp_path)
        _seed_legacy(paths.install_dir)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(app_paths, "PATHS", paths)
        notices = []
        main_module._offer_legacy_migration(
            confirm=lambda _plan: True,
            notify=lambda title, text: notices.append((title, text)))
        dest = paths.data_dir / "plex_reset_button.db"
        assert dest.is_file()
        conn = sqlite3.connect(dest)
        rows = conn.execute("SELECT title FROM requests").fetchall()
        conn.close()
        assert rows == [("Severance",)]
        assert notices and "Copied 3" in notices[0][1]
        # Declining leaves everything alone (fresh layout, confirm=False).
        paths2 = _fake_paths(tmp_path / "second")
        _seed_legacy(paths2.install_dir)
        monkeypatch.setattr(app_paths, "PATHS", paths2)
        main_module._offer_legacy_migration(
            confirm=lambda _plan: False,
            notify=lambda *_a: notices.append("nope"))
        assert not (paths2.data_dir / "plex_reset_button.db").exists()
        assert (paths2.install_dir / ".env").is_file()


# ---------------------------------------------------------------------------
# POSIX-safe routing / probing
# ---------------------------------------------------------------------------

class TestVolumeAwareRouting:
    def test_win32_branch_still_compares_drive_letters(self, monkeypatch):
        # PureWindowsPath: .drive parses drive letters on every host OS, so
        # this fence also runs (and means something) on the Linux CI leg.
        from pathlib import PureWindowsPath as WP
        from typing import Any, cast
        monkeypatch.setattr(sys, "platform", "win32")
        assert torrent_routing._same_volume(
            cast(Any, WP("C:/media")), cast(Any, WP("c:/other")))
        assert not torrent_routing._same_volume(
            cast(Any, WP("C:/a")), cast(Any, WP("D:/a")))

    def test_posix_branch_never_calls_everything_same_drive(self, monkeypatch,
                                                            tmp_path):
        """The old Path.drive comparison returned '' == '' for every POSIX
        path. The st_dev comparison must at least say 'same' for two dirs on
        one filesystem and survive nonexistent paths."""
        monkeypatch.setattr(sys, "platform", "linux")
        a = tmp_path / "mnt disk1" / "Ünïcode's Movies"
        b = tmp_path / "media" / "TV {tmdb-1}"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        assert torrent_routing._same_volume(a, b) is True
        # Nonexistent path resolves through its deepest existing ancestor.
        assert torrent_routing._same_volume(a / "nope" / "deeper", b) is True

    def test_posix_branch_distinguishes_devices(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        real_stat = os.stat
        devs = {str(a): 111, str(b): 222}

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            dev = devs.get(str(path))
            if dev is None:
                return st
            values = list(st)
            values[2] = dev  # st_dev slot
            return os.stat_result(values)

        monkeypatch.setattr(torrent_routing.os, "stat", fake_stat)
        assert torrent_routing._same_volume(a, b) is False

    def test_pick_root_override_narrows_by_volume_on_posix(self, monkeypatch,
                                                           tmp_path):
        import config
        monkeypatch.setattr(sys, "platform", "linux")
        same = tmp_path / "mnt" / "disk1" / "Movies (US)"
        other = tmp_path / "media" / "disk2" / "Movies"
        override = tmp_path / "mnt" / "disk1"
        same.mkdir(parents=True)
        other.mkdir(parents=True)
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            values = list(st)
            values[2] = 111 if "disk1" in str(path) else 222
            return os.stat_result(values)

        monkeypatch.setattr(torrent_routing.os, "stat", fake_stat)
        monkeypatch.setattr(config, "DOWNLOAD_ROOT_OVERRIDE", str(override))
        picked = torrent_routing.pick_root_by_free_space(
            [str(same), str(other)])
        assert picked == str(same)


class TestHostileNamesAndCrossFilesystemMoves:
    """Spaces, Unicode, apostrophes, brace-tagged folders, and EXDEV-style
    cross-filesystem moves — Path semantics, so these run on Windows too."""

    HOSTILE = "L'Étranger — Amélie's Cut (2021) {tmdb-42}"

    def test_move_into_brace_tagged_unicode_folder(self, tmp_path):
        import movie_migration
        src = tmp_path / "staging dir" / "Amélie's movie file.mkv"
        src.parent.mkdir()
        src.write_bytes(b"video")
        dest = tmp_path / "Movies" / self.HOSTILE / f"{self.HOSTILE}.mkv"
        movie_migration._perform_move(src, dest)
        assert dest.is_file() and dest.read_bytes() == b"video"
        assert not src.exists()

    def test_cross_filesystem_move_survives_exdev(self, tmp_path, monkeypatch):
        """shutil.move must fall back to copy+delete when rename crosses
        filesystems — simulated with a raised EXDEV."""
        import movie_migration
        src = tmp_path / "fs1" / "Naomi's Show S01E01 — 目玉焼き.mkv"
        src.parent.mkdir()
        src.write_bytes(b"episode")
        dest = tmp_path / "fs2" / "TV {tvdb-9}" / src.name
        real_rename = os.rename

        def exdev_rename(a, b, *args, **kwargs):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(a))

        monkeypatch.setattr(os, "rename", exdev_rename)
        try:
            movie_migration._perform_move(src, dest)
        finally:
            monkeypatch.setattr(os, "rename", real_rename)
        assert dest.is_file() and dest.read_bytes() == b"episode"
        assert not src.exists()

    def test_pick_root_handles_hostile_candidate_names(self, tmp_path,
                                                       monkeypatch):
        import config
        monkeypatch.setattr(config, "DOWNLOAD_ROOT_OVERRIDE", "")
        roots = [tmp_path / "Films  d'auteur", tmp_path / "映画 {edition}"]
        for r in roots:
            r.mkdir()
        picked = torrent_routing.pick_root_by_free_space(
            [str(r) for r in roots])
        assert picked in {str(r) for r in roots}


class TestFfprobeDiscovery:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        video_quality._ffprobe_path = None
        video_quality._ffprobe_resolved = False
        yield
        video_quality._ffprobe_path = None
        video_quality._ffprobe_resolved = False

    def test_path_hit_wins_everywhere(self, monkeypatch):
        monkeypatch.setattr(video_quality.shutil, "which",
                            lambda _n: "/usr/bin/ffprobe")
        assert video_quality._find_ffprobe() == "/usr/bin/ffprobe"

    def test_windows_fallback_globs_never_run_on_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(video_quality.shutil, "which", lambda _n: None)
        import glob as glob_module

        def boom(*a, **k):
            raise AssertionError("Windows glob fallback ran on Linux")

        monkeypatch.setattr(glob_module, "glob", boom)
        assert video_quality._find_ffprobe() is None


# ---------------------------------------------------------------------------
# Updater capability
# ---------------------------------------------------------------------------

class TestUpdaterCapability:
    def _info(self, zip_url: str | None = "https://example.invalid/build.zip"):
        return updater.UpdateInfo(version="9.9", html_url="x", zip_url=zip_url,
                                  notes="", urgent=False, urgent_message="")

    def test_self_update_is_windows_frozen_only(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert updater.can_self_update(self._info()) is True
        assert updater.can_self_update(self._info(zip_url=None)) is False

        monkeypatch.setattr(sys, "platform", "linux")
        assert updater.can_self_update(self._info()) is False

    def test_stage_self_update_refuses_non_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(RuntimeError, match="only available on Windows"):
            updater.stage_self_update(self._info())


# ---------------------------------------------------------------------------
# The --smoke-test flag (the same bounded run CI drives under Xvfb)
# ---------------------------------------------------------------------------

def _tk_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("tkinter") is not None


@pytest.mark.skipif(
    not _tk_available()
    or (sys.platform != "win32" and not os.environ.get("DISPLAY")),
    reason="needs tkinter (python3-tk) and a display — the linux-smoke.yml "
           "CI job runs this exact flag under xvfb with everything installed")
def test_smoke_flag_exits_zero_in_bounded_time():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SENSARR_") and k != "APP_DB_PATH"}
    result = subprocess.run(
        [sys.executable, str(REPO / "main.py"), "--smoke-test"],
        capture_output=True, text=True, timeout=150, cwd=str(REPO), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SMOKE RESULT: PASS" in result.stdout

def test_packaged_run_never_loads_env_from_cwd(tmp_path):
    """A .env sitting in the CURRENT WORKING DIRECTORY must never load.

    dotenv's bare load_dotenv() walks the cwd upward, so a packaged binary
    launched from an unrelated folder would silently adopt that folder's
    tokens — proven live when a Linux exercise run picked up the repo's real
    .env through /mnt/c and polled the production Telegram bot. Only the two
    known homes (CONFIG_DIR, install dir) may ever be read."""
    trap_cwd = tmp_path / "cwd"
    trap_cwd.mkdir()
    (trap_cwd / ".env").write_text(
        "SENSARR_ENV_TRAP=leaked\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SENSARR_") and k != "SENSARR_ENV_TRAP"}
    env["SENSARR_CONFIG_DIR"] = str(config_dir)   # empty: no .env here
    env["PYTHONPATH"] = str(REPO)
    env["APP_DB_PATH"] = str(tmp_path / "trap.db")
    code = ("import os, config; "
            "print('TRAP=' + repr(os.getenv('SENSARR_ENV_TRAP')))")
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=60, cwd=str(trap_cwd), env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TRAP=None" in result.stdout, result.stdout

    # The pinned CONFIG_DIR home still loads normally.
    (config_dir / ".env").write_text(
        "SENSARR_ENV_TRAP=from_config_home\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        timeout=60, cwd=str(trap_cwd), env=env)
    assert "TRAP='from_config_home'" in result.stdout, result.stdout
