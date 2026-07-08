# =============================================================================
# health.py
# =============================================================================
# One-shot health report for the Status tab: is everything this app depends
# on actually working right now? Each check returns (ok, label, detail) and
# the report renders as a simple pass/warn list.
#
# Also: Plex server update check (compares the running server's version to
# plex.tv's public downloads feed) and an app update check against GitHub
# releases.
# =============================================================================

import json
import logging
import shutil
import ssl
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import config

logger = logging.getLogger(__name__)

GITHUB_REPO = "Slagathore/Plexxarr"

_PLEX_DOWNLOADS_URL = "https://plex.tv/api/downloads/5.json"
_TIMEOUT = 10


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    label: str
    detail: str

    def line(self) -> str:
        mark = "OK " if self.ok else "!! "
        return f"[{mark.strip()}] {self.label}: {self.detail}"


def _http_json(url: str, headers: dict | None = None) -> dict | list | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"{config.APP_PRODUCT_NAME}/{config.APP_VERSION}",
        **(headers or {}),
    })
    ctx = None
    if not config.PLEX_VERIFY_SSL and url.startswith(config.PLEX_SERVER_URL):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def plex_server_version() -> str | None:
    """Version string of the local Plex server, via its /identity endpoint."""
    try:
        url = config.PLEX_SERVER_URL.rstrip("/") + "/identity"
        data = _http_json(url, headers={"Accept": "application/json"})
        if isinstance(data, dict):
            container = data.get("MediaContainer", data)
            return str(container.get("version")) if container.get("version") else None
    except Exception as exc:
        logger.debug("Plex /identity failed: %s", exc)
    return None


def plex_latest_version() -> str | None:
    """Newest public Plex Media Server version for Windows, from plex.tv."""
    try:
        data = _http_json(_PLEX_DOWNLOADS_URL)
        if isinstance(data, dict):
            windows = (data.get("computer") or {}).get("Windows") or {}
            return str(windows.get("version")) if windows.get("version") else None
    except Exception as exc:
        logger.debug("plex.tv downloads feed failed: %s", exc)
    return None


def app_latest_release() -> str | None:
    """Tag of the newest GitHub release for this app, or None."""
    try:
        data = _http_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
        if isinstance(data, dict) and data.get("tag_name"):
            return str(data["tag_name"])
    except Exception as exc:
        logger.debug("GitHub release check failed: %s", exc)
    return None


def run_health_checks(*, bot_running: bool | None = None) -> list[CheckResult]:
    """All local checks — cheap enough to run on demand from the Status tab."""
    checks: list[CheckResult] = []

    # Plex process + API
    try:
        from plex_control import is_plex_running
        running = is_plex_running()
        checks.append(CheckResult(running, "Plex process",
                                  "running" if running else "not running"))
    except Exception as exc:
        checks.append(CheckResult(False, "Plex process", f"check failed: {exc}"))

    version = plex_server_version()
    if version:
        checks.append(CheckResult(True, "Plex API", f"reachable (server v{version})"))
    else:
        checks.append(CheckResult(False, "Plex API",
                                  f"no response from {config.PLEX_SERVER_URL} — token or server down?"))

    checks.append(CheckResult(
        bool(config.PLEX_TOKEN), "Plex token",
        "configured" if config.PLEX_TOKEN else "missing — click 'Get Plex Token'"))

    if bot_running is not None:
        checks.append(CheckResult(bot_running, "Telegram bot",
                                  "running" if bot_running else "not running"))

    # External API keys
    checks.append(CheckResult(bool(config.TMDB_API_KEY), "TMDB key",
                              "configured" if config.TMDB_API_KEY else "missing — TV/anime matching degraded"))
    checks.append(CheckResult(bool(config.TVDB_API_KEY), "TVDB key",
                              "configured" if config.TVDB_API_KEY else "missing — TV episode lists degraded"))

    # Torrent pipeline
    node = shutil.which(config.NODE_PATH)
    checks.append(CheckResult(bool(node), "Node.js",
                              node or f"'{config.NODE_PATH}' not found — downloads won't start"))
    runner = Path(config.APP_DIR) / "torrent_runner" / "download.mjs"
    modules = runner.parent / "node_modules"
    if runner.is_file():
        detail = "ready" if modules.is_dir() else "download.mjs found but node_modules missing — run 'npm install' in torrent_runner/"
        checks.append(CheckResult(modules.is_dir(), "Torrent runner", detail))
    else:
        checks.append(CheckResult(False, "Torrent runner", f"missing: {runner}"))

    # Library paths + free space
    for entry in config.MEDIA_LIBRARY_PATHS:
        p = Path(entry.path)
        if not p.is_dir():
            checks.append(CheckResult(False, f"Library path ({entry.media_type})",
                                      f"NOT FOUND: {entry.path}"))
            continue
        try:
            usage = shutil.disk_usage(p)
            free_gb = usage.free / (1024 ** 3)
            checks.append(CheckResult(free_gb > 10, f"Library path ({entry.media_type})",
                                      f"{entry.path} — {free_gb:.0f} GB free"))
        except OSError as exc:
            checks.append(CheckResult(False, f"Library path ({entry.media_type})",
                                      f"{entry.path} — {exc}"))

    # Staging dir
    staging = Path(config.TORRENT_DOWNLOAD_DIR)
    try:
        staging.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(staging).free / (1024 ** 3)
        checks.append(CheckResult(free_gb > 5, "Staging folder",
                                  f"{staging} — {free_gb:.0f} GB free"))
    except OSError as exc:
        checks.append(CheckResult(False, "Staging folder", f"{staging} — {exc}"))

    # Ollama (optional — LLM features degrade gracefully)
    try:
        import llm_service
        available = llm_service.llm_available()
        checks.append(CheckResult(True, "Ollama (optional)",
                                  f"reachable at {config.OLLAMA_HOST}" if available
                                  else "not reachable — fuzzy matching falls back to rapidfuzz"))
    except Exception as exc:
        checks.append(CheckResult(True, "Ollama (optional)", f"check failed: {exc}"))

    return checks


def format_health_report(*, bot_running: bool | None = None,
                         include_updates: bool = True) -> str:
    lines = ["=== Server Health ===", ""]
    checks = run_health_checks(bot_running=bot_running)
    problems = [c for c in checks if not c.ok]
    lines.append(f"{len(checks) - len(problems)}/{len(checks)} checks passing"
                 + (f" — {len(problems)} issue(s) below" if problems else " — all good ✔"))
    lines.append("")
    lines.extend(c.line() for c in checks)

    if include_updates:
        lines.append("")
        lines.append("=== Updates ===")
        current = plex_server_version()
        latest = plex_latest_version()
        if current and latest:
            base_current = current.split("-")[0]
            base_latest = latest.split("-")[0]
            if base_current == base_latest:
                lines.append(f"Plex Media Server: up to date (v{current})")
            else:
                lines.append(f"Plex Media Server: v{current} installed, v{latest} available")
        else:
            lines.append("Plex Media Server: version check unavailable")
        release = app_latest_release()
        if release:
            lines.append(f"{config.APP_PRODUCT_NAME}: v{config.APP_VERSION} installed, "
                         f"latest GitHub release is {release}")
        else:
            lines.append(f"{config.APP_PRODUCT_NAME}: v{config.APP_VERSION} "
                         "(no GitHub releases published yet)")
    return "\n".join(lines)
