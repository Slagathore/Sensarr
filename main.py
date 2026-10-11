# =============================================================================
# Sensarr — main.py
# =============================================================================
# Mission: Entry point for Sensarr, a Windows system-tray application
# and Telegram bot that lets you remotely control Plex Media Server from your
# phone. Goals: start the desktop app with sufficient OS privileges, ensure
# clean startup, and delegate all functionality to the desktop layer.
#
# This module is intentionally minimal — it owns the UAC elevation check and
# the top-level `main()` entry point. All real work lives in desktop_app.py.
#
# UAC elevation strategy:
#   - When running as a PyInstaller EXE: the .spec sets uac_admin=True, so
#     Windows automatically prompts for elevation at launch. No action needed.
#   - When running as a Python script (development): this module uses
#     ctypes.windll.shell32.ShellExecuteW("runas") to self-elevate. The
#     original un-elevated instance exits immediately; a new elevated instance
#     takes over.
# =============================================================================

import ctypes
import logging
import os
import sys

# --smoke-test (Task H item 9): a bounded, network-free, display-optional
# startup/clean-shutdown proof used by CI (xvfb) and the packaged-artifact
# smoke. The env redirection MUST happen before any app module import —
# config resolves every path the moment it first loads.
if "--smoke-test" in sys.argv:
    import atexit as _atexit
    import shutil as _shutil
    import tempfile as _tempfile
    _smoke_root = _tempfile.mkdtemp(prefix="sensarr-smoke-")

    def _remove_smoke_root() -> None:
        logging.shutdown()   # release the log file so Windows can delete it
        _shutil.rmtree(_smoke_root, ignore_errors=True)

    _atexit.register(_remove_smoke_root)
    for _key, _sub in (("SENSARR_CONFIG_DIR", "config"),
                       ("SENSARR_DATA_DIR", "data"),
                       ("SENSARR_CACHE_DIR", "cache"),
                       ("SENSARR_RUNTIME_DIR", "runtime")):
        os.environ[_key] = os.path.join(_smoke_root, _sub)
    os.environ["APP_DB_PATH"] = os.path.join(_smoke_root, "data", "smoke.db")
    os.environ["TORRENT_DOWNLOAD_DIR"] = os.path.join(_smoke_root, "downloads")
    os.environ["TELEGRAM_BOT_TOKEN"] = ""   # never start the bot
    os.environ["SENSARR_SKIP_ELEVATION"] = "1"

from app_logging import configure_logging

configure_logging()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Windows UAC elevation helpers
# ---------------------------------------------------------------------------

def _is_admin() -> bool:
    """Return True if the current process holds administrator privileges.

    On non-Windows platforms this always returns True so the check is a no-op.
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except AttributeError:
        return True  # Non-Windows: assume fine, skip elevation


def _relaunch_as_admin() -> None:
    """Trigger a UAC prompt and re-launch the current process with elevation.

    Uses ShellExecuteW with the "runas" verb, which is the canonical Windows
    approach. The return value > 32 indicates success; ≤ 32 means the user
    cancelled or the operation failed.
    """
    params = " ".join(f'"{arg}"' for arg in sys.argv)
    result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", sys.executable, params, None, 1
    )
    if result <= 32:
        logger.warning(
            "UAC elevation via ShellExecuteW returned %s — "
            "the user may have declined the prompt or elevation is unavailable.",
            result,
        )


def _ensure_admin() -> None:
    """Gate the application behind an admin check on Windows.

    - Frozen EXE (PyInstaller): the spec already sets uac_admin=True, so
      Windows handles the elevation before Python code even runs. Skip.
    - Python script: check IsUserAnAdmin(); if not elevated, call
      ShellExecuteW("runas") to spawn an elevated instance and exit this one.
    - Non-Windows: no-op.
    """
    if sys.platform != "win32":
        return
    if os.environ.get("SENSARR_SKIP_ELEVATION") == "1":
        return  # capture/dev run — admin-only actions degrade gracefully
    if getattr(sys, "frozen", False):
        # PyInstaller EXE is already running with the privileges the spec
        # requested (uac_admin=True). Nothing to do here.
        return
    if not _is_admin():
        logger.info(
            "Sensarr requires administrator privileges to force-kill "
            "Plex processes. Requesting UAC elevation now…"
        )
        _relaunch_as_admin()
        sys.exit(0)  # Exit un-elevated instance; elevated instance takes over.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _already_running() -> bool:
    """Single-instance guard: two Sensarr instances mean two Telegram
    pollers (constant conflicts), two schedulers, and two download queues
    fighting over the same staging folder. PID lock lives in the RUNTIME dir
    of the path contract (beside the exe on Windows, exactly as before).
    The pre-rename lock (plexxarr.pid) is honoured too, so launching the
    renamed build beside a still-running old instance refuses the same way
    instead of double-polling the bot."""
    import os
    import app_paths

    def _holds_live_instance(pid_file) -> bool:
        try:
            if not pid_file.is_file():
                return False
            old_pid = int(pid_file.read_text().strip() or 0)
            if not old_pid or old_pid == os.getpid():
                return False
            import psutil
            proc = psutil.Process(old_pid)
            blob = ((proc.name() or "") + " "
                    + " ".join(proc.cmdline())).lower()
            # Frozen EXE under either name, or a source run of main.py.
            return bool(proc.is_running() and (
                "sensarr" in blob or "plexxarr" in blob
                or "main.py" in blob))
        except Exception:
            return False  # stale/undecodable lock — take it over

    lock = app_paths.PATHS.runtime_dir / "sensarr.pid"
    legacy_lock = app_paths.PATHS.runtime_dir / "plexxarr.pid"
    if _holds_live_instance(lock) or _holds_live_instance(legacy_lock):
        return True
    try:
        lock.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass
    return False


def _run_smoke_test() -> int:
    """Bounded startup/clean-shutdown proof (Task H item 9).

    Every writable path was redirected to a fresh temp tree at module import
    (above), so this run touches no real database, no real .env, and no real
    caches. No network: the Telegram bot never starts, no refresh timers run
    (the Tk mainloop is never entered), and the update check is never
    scheduled. A watchdog hard-exits non-zero if anything hangs.
    """
    import threading

    def _abort() -> None:
        print("SMOKE FAIL: timed out after 120s", flush=True)
        os._exit(2)

    watchdog = threading.Timer(120.0, _abort)
    watchdog.daemon = True
    watchdog.start()

    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            print(f"SMOKE OK: {name}", flush=True)
        except Exception as exc:
            logger.exception("Smoke check failed: %s", name)
            failures.append(f"{name}: {exc}")
            print(f"SMOKE FAIL: {name}: {exc}", flush=True)

    import app_paths

    def paths_check() -> None:
        for d in (app_paths.PATHS.config_dir, app_paths.PATHS.data_dir,
                  app_paths.PATHS.cache_dir, app_paths.PATHS.runtime_dir):
            if not d.is_dir():
                raise RuntimeError(f"contract dir missing: {d}")
        import config
        expected_dir = os.environ["SENSARR_DATA_DIR"]
        if os.path.dirname(config.APP_DB_PATH) != expected_dir:
            raise RuntimeError(f"DB not in smoke dir: {config.APP_DB_PATH}")

    check("path contract dirs exist under the smoke temp root", paths_check)

    app_holder: dict = {}

    def build_app() -> None:
        from desktop_app import DesktopApp
        app_holder["app"] = DesktopApp()

    check("DesktopApp builds (Tk UI + DB init + tray adapter)", build_app)

    app = app_holder.get("app")
    if app is not None:
        check("Tk renders one idle pass",
              lambda: app.root.update_idletasks())

        def clean_shutdown() -> None:
            app._shutdown(bot_timeout=1.0)

        check("clean shutdown", clean_shutdown)

    def db_reopen() -> None:
        import sqlite3
        import config
        conn = sqlite3.connect(config.APP_DB_PATH, timeout=15)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            conn.close()

    check("smoke database reopens after shutdown", db_reopen)

    watchdog.cancel()
    if failures:
        print(f"SMOKE RESULT: FAIL ({len(failures)} check(s) failed)",
              flush=True)
        return 1
    print("SMOKE RESULT: PASS", flush=True)
    return 0


def _run_migration_offer(items, plan_text: str, confirm, notify) -> None:
    import legacy_migration
    if not confirm(plan_text):
        return
    summary = legacy_migration.execute_migration(items)
    logger.info("Legacy migration summary: %s", summary)
    notify(
        "Migration finished",
        f"Copied {summary['copied']}, skipped {summary['skipped']} "
        f"(already done), failed {summary['failed']}."
        + ("\n\nFailed items are named in the log. A database that is in "
           "use fails safely — close whatever is using it and relaunch."
           if summary["failed"] else ""))


def _offer_legacy_migration(confirm=None, notify=None) -> None:
    """Linux upgrade path (Task H item 2): offer the journalled legacy-path
    migration BEFORE the desktop app constructs — DesktopApp.__init__ runs
    initialize_*_db(), which creates a fresh schema-only database at the
    XDG destination and would leave the real DB copy stuck behind the
    anti-clobber guard on every launch. This call must stay ahead of the
    desktop_app import in main().

    `confirm(plan_text) -> bool` and `notify(title, text)` default to Tk
    dialogs and are injectable for tests. Windows never migrates (the plan
    is empty on win32 by definition)."""
    if sys.platform == "win32":
        return
    try:
        import legacy_migration
        items = legacy_migration.plan_migration()
        if not items:
            return
        plan_text = legacy_migration.format_plan(items)
        if confirm is not None and notify is not None:
            _run_migration_offer(items, plan_text, confirm, notify)
            return
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
        except Exception:
            logger.info(
                "Legacy data detected but no dialog is available — run from "
                "a desktop session to migrate. Plan:\n%s", plan_text)
            return
        try:
            _run_migration_offer(
                items, plan_text,
                confirm or (lambda text: messagebox.askyesno(
                    "Move data to the standard Linux locations?",
                    text + "\n\nProceed? (Choosing No keeps everything "
                    "where it is; you'll be asked again next launch.)")),
                notify or messagebox.showinfo)
        finally:
            root.destroy()
    except Exception:
        logger.exception("Legacy path migration offer failed.")


def main() -> None:
    if "--smoke-test" in sys.argv:
        # os._exit: daemon threads (tray/psutil scans) must not block CI.
        os._exit(_run_smoke_test())
    _ensure_admin()
    if _already_running():
        import platform_adapter
        platform_adapter.show_duplicate_instance_message(
            "Sensarr is already running (check the system tray).")
        return
    # Ordering matters (Task H item 2): the migration offer must precede the
    # desktop_app import below — DesktopApp.__init__ initializes databases
    # at the XDG destination as a side effect of construction.
    _offer_legacy_migration()
    try:
        from desktop_app import run_desktop_app

        run_desktop_app()
    except KeyboardInterrupt:
        logger.info("Received Ctrl+C in main. Exiting.")
    except ImportError as exc:
        logger.exception("Desktop UI dependencies are unavailable.")
        raise RuntimeError(
            "Desktop UI dependencies are unavailable. Install requirements.txt again "
            "so pystray is present."
        ) from exc


if __name__ == "__main__":
    main()

# #todo: add a --no-elevate CLI flag for CI/test environments where UAC is not available
# #todo: log the Windows integrity level (low/medium/high) at startup for diagnostics
# #todo: surface a user-friendly tkinter dialog if elevation is declined instead of silent exit
