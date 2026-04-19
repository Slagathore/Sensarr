import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import psutil
import pyautogui

import config
from icon_finder import (find_exit_menu_item, find_icon,
                         find_plex_taskbar_icon, find_plex_tray_icon)

logger = logging.getLogger(__name__)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ACTION_LOCK = threading.Lock()
_CURRENT_PID = os.getpid()
_KNOWN_PLEX_PROCESS_NAMES = {
    "plex media server.exe",
    "plex media server",
    "plex dlna server.exe",
    "plex dlna server",
    "plex transcoder.exe",
    "plex transcoder",
    "plex script host.exe",
    "plex script host",
    "plex tuner service.exe",
    "plex tuner service",
    "plex commercial skipper.exe",
    "plex commercial skipper",
    "plex relay.exe",
    "plex relay",
    "plex update service.exe",
    "plex update service",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_plex_gone() -> bool:
    """Returns True when no real Plex server processes are running."""
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            if _is_plex_process(proc):
                return False
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return True


def _is_plex_process(proc: psutil.Process) -> bool:
    """Best-effort match for Plex Media Server processes on Windows."""
    try:
        if proc.pid == _CURRENT_PID:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    try:
        name = (proc.info.get("name") or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        name = ""

    if name in _KNOWN_PLEX_PROCESS_NAMES:
        return True

    try:
        exe_value = proc.info.get("exe")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        exe_value = None

    exe_path = Path(str(exe_value)).expanduser() if exe_value else None
    configured_path = Path(config.PLEX_MEDIA_SERVER_PATH).expanduser()

    if exe_path is not None:
        exe_name = exe_path.name.lower()
        if exe_name in _KNOWN_PLEX_PROCESS_NAMES:
            return True

        if configured_path.name and exe_name == configured_path.name.lower():
            return True

        configured_parent = configured_path.parent
        if configured_parent and configured_parent != Path("."):
            try:
                exe_path.resolve().relative_to(configured_parent.resolve())
                return exe_name.startswith("plex ")
            except (OSError, ValueError):
                pass

    return False


def _list_plex_processes() -> list[psutil.Process]:
    processes: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _is_plex_process(proc):
                processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return processes


def _taskkill_process_tree(proc: psutil.Process) -> tuple[bool, str]:
    """Use Windows taskkill to force-stop the process and any child tree."""
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "taskkill timed out"
    except OSError as exc:
        return False, str(exc)

    details = " ".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    if completed.returncode == 0:
        return True, details or "terminated"

    lowered = details.lower()
    if "not found" in lowered or "no running instance" in lowered:
        return True, details or "already exited"

    return False, details or f"taskkill exit code {completed.returncode}"


def _list_primary_plex_server_pids() -> set[int]:
    """Return PIDs for the main Plex Media Server executable only."""
    pids: set[int] = set()
    configured_path = Path(config.PLEX_MEDIA_SERVER_PATH).expanduser()
    configured_resolved: Path | None = None
    try:
        configured_resolved = configured_path.resolve()
    except OSError:
        configured_resolved = None

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == "plex media server.exe":
                pids.add(proc.pid)
                continue

            exe_value = proc.info.get("exe")
            if not exe_value or configured_resolved is None:
                continue

            exe_path = Path(str(exe_value)).expanduser()
            try:
                if exe_path.resolve() == configured_resolved:
                    pids.add(proc.pid)
            except OSError:
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def _kill_plex_processes() -> tuple[list[str], list[str]]:
    """Kill every Plex-related process tree and return successes and failures."""
    killed: list[str] = []
    failed: list[str] = []
    processes = _list_plex_processes()

    if not processes:
        logger.info("No Plex processes found to kill.")
        return killed, failed

    for proc in processes:
        try:
            name = proc.info.get("name") or f"PID {proc.pid}"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = f"PID {proc.pid}"

        success, details = _taskkill_process_tree(proc)
        detail_suffix = f" ({details})" if details else ""
        if success:
            killed.append(f"{name} [{proc.pid}]{detail_suffix}")
        else:
            failed.append(f"{name} [{proc.pid}]{detail_suffix}")

    if killed:
        logger.info("Killed Plex processes: %s", killed)
    if failed:
        logger.warning("Failed to kill Plex processes: %s", failed)
    return killed, failed


def _lingering_plex_processes() -> list[str]:
    """Return names of all currently-running processes that contain 'plex'."""
    names: list[str] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _is_plex_process(proc):
                name = proc.info.get("name") or f"PID {proc.pid}"
                names.append(f"{name} [{proc.pid}]")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return names


def _wait_for_plex_exit() -> str | None:
    """
    Poll until Plex is gone.
    Checks immediately (t=0), then waits PLEX_EXIT_GRACE seconds before the
    first timed poll, then polls every PLEX_EXIT_WAIT seconds up to
    MAX_EXIT_RETRIES more times.
    Returns None on success, or an error string if Plex never stopped.
    """
    # Immediate check — catches the case where Plex closes in under a second
    if _is_plex_gone():
        logger.info("Plex confirmed stopped immediately after exit.")
        return None

    # Short grace period to let Plex begin its shutdown sequence
    logger.info(
        "Plex still running; waiting %.1fs grace period before polling…",
        config.PLEX_EXIT_GRACE,
    )
    time.sleep(config.PLEX_EXIT_GRACE)

    for attempt in range(1, config.MAX_EXIT_RETRIES + 1):
        if _is_plex_gone():
            logger.info("Plex confirmed stopped on poll %d.", attempt)
            return None
        lingering = _lingering_plex_processes()
        logger.warning(
            "Plex still running after poll %d/%d. Processes: %s",
            attempt,
            config.MAX_EXIT_RETRIES,
            lingering,
        )
        time.sleep(config.PLEX_EXIT_WAIT)

    lingering = _lingering_plex_processes()
    return (
        f"Plex did not shut down after {config.MAX_EXIT_RETRIES} polls "
        f"({config.PLEX_EXIT_GRACE}s grace + {config.PLEX_EXIT_WAIT}s intervals). "
        f"Still running: {', '.join(lingering) if lingering else 'unknown'}. "
        "Please check manually."
    )


def _launch_plex_from_taskbar() -> str:
    """Click the pinned Plex taskbar icon to launch Plex."""
    existing_server_pids = _list_primary_plex_server_pids()
    pos = find_plex_taskbar_icon()
    if pos is None:
        return (
            "Could not find the Plex taskbar icon. "
            "Ensure assets/taskbar_icon.png is set up correctly."
        )
    pyautogui.click(*pos)
    logger.info(
        "Clicked taskbar icon at %s. Waiting %.1fs for Plex to start.",
        pos,
        config.PLEX_LAUNCH_WAIT,
    )
    if _wait_for_plex_start(existing_server_pids):
        return "Plex launched successfully."
    return (
        "Clicked the Plex taskbar icon, but Plex did not appear to start. "
        "Check the pinned taskbar shortcut manually."
    )


def _wait_for_plex_start(previous_server_pids: set[int] | None = None) -> bool:
    """Wait for the main Plex Media Server executable to appear or restart."""
    existing = previous_server_pids or set()
    deadline = time.time() + config.PLEX_LAUNCH_WAIT
    while time.time() < deadline:
        current = _list_primary_plex_server_pids()
        if current and (not existing or current != existing):
            return True
        time.sleep(0.5)
    current = _list_primary_plex_server_pids()
    return bool(current and (not existing or current != existing))


def _launch_plex_from_executable() -> str | None:
    """Launch Plex directly from its configured Windows executable path."""
    plex_path = Path(config.PLEX_MEDIA_SERVER_PATH)
    if not plex_path.is_file():
        logger.warning("Configured Plex executable does not exist: %s", plex_path)
        return None

    existing_server_pids = _list_primary_plex_server_pids()

    try:
        subprocess.Popen(
            [str(plex_path)],
            cwd=str(plex_path.parent),
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        logger.exception("Failed to launch Plex directly from %s", plex_path)
        return f"Could not launch Plex from '{plex_path}': {exc}"

    logger.info("Launched Plex directly from %s", plex_path)
    if _wait_for_plex_start(existing_server_pids):
        return "Plex launched successfully."
    if existing_server_pids:
        return (
            f"Sent a launch request to '{plex_path}'. "
            "Some Plex server processes were already present, so startup could not be "
            "positively confirmed."
        )
    return (
        f"Sent a launch request to '{plex_path}', but no Plex process appeared. "
        "Check that the path is correct and that Windows allows it to start."
    )


def _launch_plex(allow_taskbar_fallback: bool = True) -> str:
    """Launch Plex, preferring the configured executable path first."""
    direct_result = _launch_plex_from_executable()
    if direct_result is not None:
        return direct_result
    if allow_taskbar_fallback:
        return _launch_plex_from_taskbar()
    return (
        f"Plex executable was not found at '{config.PLEX_MEDIA_SERVER_PATH}'. "
        "Update PLEX_MEDIA_SERVER_PATH in your .env file."
    )


# ---------------------------------------------------------------------------
# Public reset functions
# ---------------------------------------------------------------------------

def control_busy() -> bool:
    return _ACTION_LOCK.locked()


def _run_exclusive_action(action_name: str, action) -> str:
    if not _ACTION_LOCK.acquire(blocking=False):
        logger.info("%s requested while another Plex action is already running.", action_name)
        return "Another Plex action is already in progress. Please wait."

    try:
        return action()
    finally:
        _ACTION_LOCK.release()


def _soft_reset_impl() -> str:
    """
    Soft reset: right-click tray icon → click Exit → verify stopped → relaunch.
    Mimics what you'd do manually; least disruptive.
    """
    logger.info("Starting soft reset.")

    pos = find_plex_tray_icon()
    if pos is None:
        running = not _is_plex_gone()
        if running:
            return (
                "Could not find the Plex tray icon, but Plex IS detected running. "
                "The tray_icon.png asset likely needs to be recaptured at your current DPI. "
                "See assets/README.txt for instructions. Try /hardreset as an alternative."
            )
        return (
            "Could not find the Plex tray icon, and Plex does not appear to be running. "
            "Ensure Plex is started and assets/tray_icon.png is captured. "
            "See assets/README.txt for instructions."
        )

    logger.info("Right-clicking tray icon at %s.", pos)
    pyautogui.rightClick(*pos)
    time.sleep(0.5)

    exit_pos = find_exit_menu_item()
    if exit_pos is None:
        pyautogui.press("escape")
        return (
            "Could not find 'Exit Plex Media Server' in the context menu. "
            "Check assets/exit_menu_item.png."
        )

    logger.info("Clicking Exit menu item at %s.", exit_pos)
    pyautogui.click(*exit_pos)

    # Build a narrow region centred on where we found the icon (±100px wide, ±40px tall).
    # This prevents the taskbar icon from being mistaken for the tray icon still being present.
    icon_x, icon_y = pos
    icon_region = (
        max(0, icon_x - 100),
        max(0, icon_y - 40),
        200,   # width
        80,    # height
    )
    logger.info(
        "Watching region %s for tray icon to disappear (up to 5s)…", icon_region
    )

    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.5)
        # locateOnScreen inside find_icon always takes a fresh screenshot each call
        still_there = find_icon(
            config.TRAY_ICON_PATH,
            confidence=config.TRAY_ICON_CONFIDENCE,
            timeout=0.1,   # single fast attempt per poll
            region=icon_region,
        )
        if still_there is None:
            logger.info("Tray icon gone from region — Plex has closed.")
            return _launch_plex()

    return (
        "Plex tray icon was still visible after 5s. "
        "Plex may not have fully closed. Try /hardreset."
    )


def soft_reset() -> str:
    return _run_exclusive_action("Soft reset", _soft_reset_impl)


def _hard_reset_impl() -> str:
    """
    Hard reset: kill all Plex processes → verify stopped → relaunch.
    May interrupt active Plex sessions.
    """
    logger.info("Starting hard reset.")

    _killed, failed = _kill_plex_processes()
    lingering: list[str] = []

    if not _is_plex_gone():
        logger.info(
            "Plex still running after initial taskkill; waiting %.1fs grace period.",
            config.PLEX_EXIT_GRACE,
        )
        time.sleep(config.PLEX_EXIT_GRACE)

        for attempt in range(1, config.MAX_EXIT_RETRIES + 1):
            if _is_plex_gone():
                logger.info("All Plex processes confirmed stopped on poll %d.", attempt)
                lingering = []
                break

            lingering = _lingering_plex_processes()
            logger.warning(
                "Plex still running after poll %d/%d. Retrying taskkill. Processes: %s",
                attempt,
                config.MAX_EXIT_RETRIES,
                lingering,
            )
            _retry_killed, retry_failed = _kill_plex_processes()
            failed.extend(retry_failed)
            time.sleep(config.PLEX_EXIT_WAIT)
        else:
            lingering = _lingering_plex_processes()

    permission_note = ""
    if failed:
        permission_note = (
            "\n\nSome Plex processes initially resisted termination:\n- "
            + "\n- ".join(sorted(set(failed)))
            + "\n\nThe reset still completed, but if this keeps happening, run the bot elevated."
        )

    result = _launch_plex()
    if result == "Plex launched successfully.":
        if lingering:
            return (
                "Plex relaunch was attempted after an incomplete hard reset."
                "\n\nThese Plex processes were still running:\n- "
                + "\n- ".join(lingering)
                + permission_note
            )
        return "Plex force-restarted successfully." + permission_note

    if lingering:
        return (
            result
            + "\n\nPlex relaunch was attempted even though some processes were still running:\n- "
            + "\n- ".join(lingering)
            + permission_note
        )
    return result + permission_note


def hard_reset() -> str:
    return _run_exclusive_action("Hard reset", _hard_reset_impl)


def _launch_plex_impl() -> str:
    """Launch Plex without performing any reset."""
    logger.info("Starting direct Plex launch request.")
    return _launch_plex(allow_taskbar_fallback=False)


def launch_plex() -> str:
    return _run_exclusive_action("Launch Plex", _launch_plex_impl)


# ---------------------------------------------------------------------------
# Status / diagnostics
# ---------------------------------------------------------------------------

def get_status() -> str:
    """
    Return a plain-text diagnostic summary:
    - Which Plex processes (if any) are currently running.
    - Whether each required image asset file exists on disk.
    """
    # --- process check ---
    plex_procs: list[str] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if _is_plex_process(proc):
                plex_procs.append(f"{proc.info['name']} [{proc.info['pid']}]")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if plex_procs:
        proc_line = (
            f"Plex is RUNNING — {len(plex_procs)} process(es): "
            + ", ".join(sorted(set(plex_procs)))
        )
    else:
        proc_line = "Plex is NOT running"

    # --- asset check ---
    asset_checks = [
        ("Tray icon    ", config.TRAY_ICON_PATH),
        ("Taskbar icon ", config.TASKBAR_ICON_PATH),
        ("Tray arrow   ", config.TRAY_EXPAND_ARROW_PATH),
        ("Exit menu    ", config.EXIT_MENU_ITEM_PATH),
    ]
    asset_lines = []
    for label, path in asset_checks:
        exists = Path(path).is_file()
        mark = "OK" if exists else "MISSING"
        asset_lines.append(f"  [{mark}] {label}: {path}")

    plex_exe_path = Path(config.PLEX_MEDIA_SERVER_PATH)
    plex_exe_mark = "OK" if plex_exe_path.is_file() else "MISSING"

    parts = [
        "=== Plex Bot Status ===",
        "",
        proc_line,
        "",
        f"Plex executable: [{plex_exe_mark}] {plex_exe_path}",
        "",
        "Image assets:",
        *asset_lines,
        "",
        "If any asset is MISSING, screenshot it with Win+Shift+S and save to assets/.",
        "See assets/README.txt for capture instructions.",
    ]
    return "\n".join(parts)

    # #todo: add a live on-screen test (pyautogui.locateOnScreen) so /status can
    #        report whether the tray icon is *visible right now*, not just on disk.
