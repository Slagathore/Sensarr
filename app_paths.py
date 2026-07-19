# =============================================================================
# app_paths.py — Task H item 1
# =============================================================================
# The ONE typed contract for where the app reads and writes. Every module that
# needs a writable location gets it from here; nothing else derives its own
# "APP_DIR / something" path (tests/test_task_h_app_paths.py greps for that).
#
# Windows keeps the exact pre-Task-H layout: everything lives beside the
# executable (or the source checkout), byte-identical to what shipped before —
# a Windows user upgrading sees nothing move.
#
# Linux uses the XDG base directories, because a packaged install under /opt
# or /usr must never write beside read-only code:
#   CONFIG_DIR  = $XDG_CONFIG_HOME/sensarr   (~/.config/sensarr)    .env
#   DATA_DIR    = $XDG_DATA_HOME/sensarr     (~/.local/share/sensarr)
#                 SQLite databases, durable job/provenance state
#   CACHE_DIR   = $XDG_CACHE_HOME/sensarr    (~/.cache/sensarr)
#                 expendable scan caches, downloaded metadata dumps
#   RUNTIME_DIR = $XDG_RUNTIME_DIR/sensarr   (fallback: CACHE_DIR/runtime)
#                 pid/lock files
#   DOWNLOAD_DIR default = DATA_DIR/torrent_staging (TORRENT_DOWNLOAD_DIR wins).
#                 Deliberately NOT "downloads": that name is the repo's routing
#                 test-fixture folder, and staging into it caused permission
#                 collisions with read-only fixtures / a locked indexed path.
#
# Explicit environment overrides always win, on every platform:
#   SENSARR_CONFIG_DIR / SENSARR_DATA_DIR / SENSARR_CACHE_DIR /
#   SENSARR_RUNTIME_DIR — direct directory pins (tests, CI, the smoke run).
#   APP_DB_PATH and TORRENT_DOWNLOAD_DIR keep working exactly as before
#   (config.py applies them after .env loads).
# =============================================================================

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

APP_NAME = "sensarr"

# XDG folder names this app shipped under before the rename; legacy_migration
# scans them so a pre-rename Linux install's data follows the app over.
LEGACY_APP_NAMES = ("plexxarr",)


def _install_dir() -> Path:
    """Where the executable (frozen) or the source tree lives. Read-only on a
    packaged Linux install; the historical APP_DIR on Windows."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    """PyInstaller's unpacked read-only asset dir (_MEIPASS), else install."""
    return Path(getattr(sys, "_MEIPASS", _install_dir()))


@dataclass(frozen=True)
class AppPaths:
    """Typed path contract (Task H item 1). All absolute."""
    bundle_dir: Path    # read-only bundled assets
    install_dir: Path   # executable/source location (== legacy APP_DIR)
    config_dir: Path    # .env
    data_dir: Path      # SQLite + durable job/provenance data
    cache_dir: Path     # expendable scans, refreshable metadata dumps
    runtime_dir: Path   # pid/lock state
    download_dir: Path  # torrent staging default (TORRENT_DOWNLOAD_DIR wins)


def _dir_from_env(environ: Mapping[str, str], key: str) -> Path | None:
    raw = (environ.get(key) or "").strip()
    return Path(raw).expanduser() if raw else None


def _xdg(environ: Mapping[str, str], key: str, fallback: Path,
         app_name: str = APP_NAME) -> Path:
    raw = (environ.get(key) or "").strip()
    base = Path(raw).expanduser() if raw else fallback
    return base / app_name


def compute_paths(platform: str | None = None,
                  environ: Mapping[str, str] | None = None) -> AppPaths:
    """Pure computation — injectable platform/environ so tests can assert the
    win32 layout and the Linux XDG layout from any host OS."""
    platform = platform if platform is not None else sys.platform
    environ = os.environ if environ is None else environ
    install = _install_dir()
    bundle = _bundle_dir()

    if platform == "win32":
        # Byte-identical legacy layout: every writable file beside the exe.
        config_dir = data_dir = cache_dir = runtime_dir = install
    else:
        home = Path((environ.get("HOME") or "").strip() or Path.home())
        config_dir = _xdg(environ, "XDG_CONFIG_HOME", home / ".config")
        data_dir = _xdg(environ, "XDG_DATA_HOME", home / ".local" / "share")
        cache_dir = _xdg(environ, "XDG_CACHE_HOME", home / ".cache")
        xdg_runtime = (environ.get("XDG_RUNTIME_DIR") or "").strip()
        if xdg_runtime:
            runtime_dir = Path(xdg_runtime).expanduser() / APP_NAME
        else:
            # Documented fallback: no login-session runtime dir (headless CI,
            # containers) — keep pid/lock state under the cache tree.
            runtime_dir = cache_dir / "runtime"

    # Explicit directory pins beat the platform layout everywhere.
    config_dir = _dir_from_env(environ, "SENSARR_CONFIG_DIR") or config_dir
    data_dir = _dir_from_env(environ, "SENSARR_DATA_DIR") or data_dir
    cache_dir = _dir_from_env(environ, "SENSARR_CACHE_DIR") or cache_dir
    runtime_dir = _dir_from_env(environ, "SENSARR_RUNTIME_DIR") or runtime_dir

    # Download staging default. TORRENT_DOWNLOAD_DIR is ALSO honoured by
    # config.py after .env loads; catching a process-level value here keeps
    # the contract self-consistent for early callers.
    tdd = (environ.get("TORRENT_DOWNLOAD_DIR") or "").strip()
    if tdd:
        download_dir = Path(tdd).expanduser()
    elif platform == "win32":
        # A dedicated staging folder, NOT install/"downloads": in a source
        # checkout that name doubles as the routing/matching test fixtures, and
        # staging torrents into it produced the "permission denied" plague
        # (name collisions with read-only fixture folders / an indexed, locked
        # library path). Keep staging on its own.
        download_dir = install / "torrent_staging"
    else:
        download_dir = data_dir / "torrent_staging"

    return AppPaths(
        bundle_dir=bundle, install_dir=install, config_dir=config_dir,
        data_dir=data_dir, cache_dir=cache_dir, runtime_dir=runtime_dir,
        download_dir=download_dir,
    )


def legacy_xdg_dirs(platform: str | None = None,
                    environ: Mapping[str, str] | None = None
                    ) -> list[tuple[Path, Path, Path]]:
    """(config, data, cache) triplets for the old app names, for the
    legacy migration to scan. Empty on Windows, where the layout is the
    install folder and never moved."""
    platform = platform if platform is not None else sys.platform
    environ = os.environ if environ is None else environ
    if platform == "win32":
        return []
    home = Path((environ.get("HOME") or "").strip() or Path.home())
    return [(
        _xdg(environ, "XDG_CONFIG_HOME", home / ".config", name),
        _xdg(environ, "XDG_DATA_HOME", home / ".local" / "share", name),
        _xdg(environ, "XDG_CACHE_HOME", home / ".cache", name),
    ) for name in LEGACY_APP_NAMES]


def ensure_dirs(paths: AppPaths) -> None:
    """Create the writable directories, user-only where the OS supports it.
    The download dir stays on-demand (created at first use, as before)."""
    for d in (paths.config_dir, paths.data_dir, paths.cache_dir,
              paths.runtime_dir):
        try:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            # Surfaced later by whoever actually writes there — creating
            # directories at import time must never crash startup.
            pass


PATHS: AppPaths = compute_paths()
ensure_dirs(PATHS)

# Convenience module-level names matching the contract wording.
BUNDLE_DIR: Path = PATHS.bundle_dir
INSTALL_DIR: Path = PATHS.install_dir
CONFIG_DIR: Path = PATHS.config_dir
DATA_DIR: Path = PATHS.data_dir
CACHE_DIR: Path = PATHS.cache_dir
RUNTIME_DIR: Path = PATHS.runtime_dir
DOWNLOAD_DIR: Path = PATHS.download_dir
