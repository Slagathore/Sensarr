# =============================================================================
# PlexResetButton — main.py
# =============================================================================
# Mission: Entry point for PlexResetButton, a Windows system-tray application
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
import sys

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
    if getattr(sys, "frozen", False):
        # PyInstaller EXE is already running with the privileges the spec
        # requested (uac_admin=True). Nothing to do here.
        return
    if not _is_admin():
        logger.info(
            "PlexResetButton requires administrator privileges to force-kill "
            "Plex processes. Requesting UAC elevation now…"
        )
        _relaunch_as_admin()
        sys.exit(0)  # Exit un-elevated instance; elevated instance takes over.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _ensure_admin()
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
