import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TypedDict, cast

import psutil

import config
import platform_adapter

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


class _ProcessInfo(TypedDict, total=False):
    pid: int
    name: str | None
    exe: str | None
    cmdline: list[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _proc_info(proc: psutil.Process) -> _ProcessInfo:
    """Return cached process_iter metadata when available."""
    info = getattr(proc, "info", None)
    if isinstance(info, dict):
        return cast(_ProcessInfo, info)
    return {}


# The desktop status tick calls is_plex_running() and get_status() back to back;
# caching one process_iter sweep for a few seconds lets the pair share a single
# enumeration instead of walking every process twice per tick.
_STATUS_SWEEP_TTL_SECONDS = 5.0
_status_sweep_lock = threading.Lock()
_status_sweep_procs: list[psutil.Process] | None = None
_status_sweep_at = 0.0


def _status_process_sweep() -> list[psutil.Process]:
    """Return a briefly cached process_iter sweep for the status checks.

    Only pid/name/exe are fetched — the matcher never inspects cmdline — and the
    snapshot is refreshed once its TTL lapses, so a burst of callers within one
    tick reuses a single sweep.
    """
    global _status_sweep_procs, _status_sweep_at
    with _status_sweep_lock:
        now = time.monotonic()
        if (_status_sweep_procs is None
                or now - _status_sweep_at >= _STATUS_SWEEP_TTL_SECONDS):
            _status_sweep_procs = list(psutil.process_iter(["pid", "name", "exe"]))
            _status_sweep_at = now
        return _status_sweep_procs


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
    info = _proc_info(proc)

    try:
        if proc.pid == _CURRENT_PID:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    try:
        name = (info.get("name") or "").lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        name = ""

    if name in _KNOWN_PLEX_PROCESS_NAMES:
        return True

    try:
        exe_value = info.get("exe")
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


def _stop_process_tree(proc: psutil.Process) -> tuple[bool, str]:
    """Force-stop the process and its child tree, per platform.

    Windows keeps the exact taskkill flow it always had. Everywhere else the
    platform adapter walks the tree with psutil: children first, terminate ->
    timed wait -> kill (Task H item 5 — never taskkill, never a hard-coded
    distro path)."""
    if sys.platform == "win32":
        return _taskkill_process_tree(proc)
    return platform_adapter.terminate_process_tree(proc.pid)


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
        info = _proc_info(proc)
        try:
            name = (info.get("name") or "").lower()
            if name == "plex media server.exe":
                pids.add(proc.pid)
                continue

            exe_value = info.get("exe")
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
        info = _proc_info(proc)
        try:
            name = info.get("name") or f"PID {proc.pid}"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = f"PID {proc.pid}"

        success, details = _stop_process_tree(proc)
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
        info = _proc_info(proc)
        try:
            if _is_plex_process(proc):
                name = info.get("name") or f"PID {proc.pid}"
                names.append(f"{name} [{proc.pid}]")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return names


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


def _launch_plex() -> str:
    """Launch Plex from the configured executable path."""
    direct_result = _launch_plex_from_executable()
    if direct_result is not None:
        return direct_result
    return (
        f"Plex executable was not found at '{config.PLEX_MEDIA_SERVER_PATH}'. "
        "Update PLEX_MEDIA_SERVER_PATH in your .env file."
    )


# ---------------------------------------------------------------------------
# Local-control capability (Task H item 5)
# ---------------------------------------------------------------------------

def local_control_available() -> tuple[bool, str]:
    """Whether the local Launch/Exit/Hard Reset controls make sense here.

    Windows: always on — the flow every existing install has used (the
    configured default path plus process-name matching cover it), so nothing
    changes for a Windows user. Elsewhere: only when a configured executable
    override exists on disk or a Plex process is actually running; a
    remote-only PLEX_SERVER_URL config gets (False, explanation) so the UI
    disables the buttons instead of treating the server as broken."""
    if sys.platform == "win32":
        return True, "windows"
    configured = (config.PLEX_MEDIA_SERVER_PATH or "").strip()
    if configured and Path(configured).expanduser().is_file():
        return True, f"configured executable: {configured}"
    if not _is_plex_gone():
        return True, "local Plex process detected"
    return False, (
        "No local Plex install was found on this machine. Remote features "
        "(library, requests, watchlist) keep working through "
        f"PLEX_SERVER_URL ({config.PLEX_SERVER_URL}); to enable local "
        "process control, set PLEX_MEDIA_SERVER_PATH to your Plex Media "
        "Server executable."
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
    return _launch_plex()


def launch_plex() -> str:
    return _run_exclusive_action("Launch Plex", _launch_plex_impl)


# ---------------------------------------------------------------------------
# Status / diagnostics
# ---------------------------------------------------------------------------

def is_plex_running() -> bool:
    """Cheap structured check: is any Plex process alive right now?

    Used by the desktop UI status indicator; get_status() below returns the
    human-readable diagnostic text.
    """
    for proc in _status_process_sweep():
        try:
            if _is_plex_process(proc):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def get_status() -> str:
    """
    Return a plain-text diagnostic summary:
    - Which Plex processes (if any) are currently running.
    - Whether the configured Plex executable exists on disk.
    """
    # --- process check ---
    plex_procs: list[str] = []
    for proc in _status_process_sweep():
        info = _proc_info(proc)
        try:
            if _is_plex_process(proc):
                proc_name = info.get("name") or f"PID {proc.pid}"
                plex_procs.append(f"{proc_name} [{proc.pid}]")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if plex_procs:
        proc_line = (
            f"Plex is RUNNING — {len(plex_procs)} process(es): "
            + ", ".join(sorted(set(plex_procs)))
        )
    else:
        proc_line = "Plex is NOT running"

    plex_exe_path = Path(config.PLEX_MEDIA_SERVER_PATH)
    plex_exe_mark = "OK" if plex_exe_path.is_file() else "MISSING"

    parts = [
        "=== Plex Bot Status ===",
        "",
        proc_line,
        "",
        f"Plex executable: [{plex_exe_mark}] {plex_exe_path}",
    ]
    return "\n".join(parts)
