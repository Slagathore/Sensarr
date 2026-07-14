# =============================================================================
# updater.py
# =============================================================================
# In-app update channel against GitHub Releases.
#
# Publishing an update = tagging a release (v1.2, v1.3, …) on the repo with
# the zipped build attached. Every installed copy checks during the nightly
# pass and shows a dismissable banner on the Status tab.
#
# Emergencies: put a line starting with  PLEXXARR-URGENT:  in the release
# notes ("PLEXXARR-URGENT: fixes a bug that can delete library files —
# update today"). Urgent releases ignore the user's dismiss/mute choices,
# show a red banner every launch, and pop the message once per session.
# =============================================================================

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger(__name__)

REPO = "Slagathore/Plexxarr"
URGENT_MARKER = "PLEXXARR-URGENT:"
_API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


@dataclass(frozen=True)
class UpdateInfo:
    version: str            # "1.2"
    html_url: str           # release page
    zip_url: str | None     # first .zip asset, if any
    notes: str
    urgent: bool
    urgent_message: str


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def check_for_update(timeout: int = 15) -> UpdateInfo | None:
    """Latest release newer than the running version, else None.
    Network or API failures return None — the nightly pass just tries again
    tomorrow."""
    try:
        req = urllib.request.Request(_API_LATEST, headers={
            "User-Agent": f"{config.APP_PRODUCT_NAME}/{config.APP_VERSION}",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
        return None

    tag = str(data.get("tag_name") or "").lstrip("vV")
    if not tag or _version_tuple(tag) <= _version_tuple(config.APP_VERSION):
        return None

    notes = str(data.get("body") or "")
    urgent_message = ""
    for line in notes.splitlines():
        if line.strip().startswith(URGENT_MARKER):
            urgent_message = line.strip()[len(URGENT_MARKER):].strip()
            break
    zip_url = next(
        (a.get("browser_download_url") for a in data.get("assets", [])
         if str(a.get("name", "")).lower().endswith(".zip")),
        None,
    )
    return UpdateInfo(
        version=tag,
        html_url=str(data.get("html_url") or f"https://github.com/{REPO}/releases"),
        zip_url=zip_url,
        notes=notes,
        urgent=bool(urgent_message),
        urgent_message=urgent_message,
    )


def can_self_update(info: UpdateInfo) -> bool:
    """Self-update only makes sense for the packaged EXE with a zip asset;
    source checkouts update with git pull."""
    return bool(getattr(sys, "frozen", False) and info.zip_url)


def stage_self_update(info: UpdateInfo, on_status=None) -> str:
    """Download + extract the release zip and write the swap script.

    Returns the path of a .bat that: waits for this process to exit, copies
    the new build over the install folder (PRESERVING .env, databases, and
    caches), and relaunches. Caller starts the .bat and exits the app.
    """
    def status(msg: str) -> None:
        logger.info("Updater: %s", msg)
        if on_status:
            on_status(msg)

    assert info.zip_url, "no zip asset on the release"
    work = Path(tempfile.mkdtemp(prefix="plexxarr-update-"))
    zip_path = work / "update.zip"

    status(f"Downloading v{info.version}…")
    req = urllib.request.Request(info.zip_url, headers={
        "User-Agent": f"{config.APP_PRODUCT_NAME}/{config.APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=120) as resp, \
            open(zip_path, "wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)

    status("Extracting…")
    extract = work / "extracted"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract)

    # The zip may nest the build one folder deep (dist layout) — find the
    # directory that actually contains the EXE.
    exe_name = f"{config.APP_PRODUCT_NAME}.exe"
    src_dir = extract
    if not (src_dir / exe_name).is_file():
        hit = next((p.parent for p in extract.rglob(exe_name)), None)
        if hit is None:
            raise RuntimeError(f"{exe_name} not found inside the release zip")
        src_dir = hit

    install_dir = Path(config.APP_DIR)
    bat = work / "apply_update.bat"
    # Everything the USER owns stays: .env, SQLite databases (requests,
    # shows, downloads), JSON scan caches (plus any pickle-era leftovers),
    # the anime metadata DB, pid lock.
    bat.write_text(
        "@echo off\r\n"
        f"echo Waiting for {config.APP_PRODUCT_NAME} to close...\r\n"
        ":wait\r\n"
        f"tasklist /FI \"PID eq {os.getpid()}\" 2>NUL | find \"{os.getpid()}\" >NUL\r\n"
        "if not errorlevel 1 (timeout /t 2 /nobreak >NUL & goto wait)\r\n"
        f"echo Installing v{info.version}...\r\n"
        f"robocopy \"{src_dir}\" \"{install_dir}\" /E /R:3 /W:2 "
        "/XF .env *.db *.db-shm *.db-wal *.pkl *.sqlite *.sqlite-* "
        "plexxarr.pid unidentified_folders.json trackers_cache.txt "
        "maintenance_cache.json library_lowqual.json watchlist_recs.json\r\n"
        f"start \"\" \"{install_dir / exe_name}\"\r\n"
        f"rmdir /S /Q \"{work}\"\r\n",
        encoding="ascii", errors="replace",
    )
    status("Staged — restarting to finish.")
    return str(bat)


def launch_staged_update(bat_path: str) -> None:
    """Start the swap script detached; the caller must exit the app now."""
    subprocess.Popen(
        ["cmd", "/c", bat_path],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
    )
