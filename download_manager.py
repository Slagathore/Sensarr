# =============================================================================
# download_manager.py
# =============================================================================
# Orchestrates torrent downloads end-to-end:
#
#   grab(result) → downloads row → Node webtorrent runner (subprocess, JSONL
#   protocol) → staging dir → post-process (optional rename + move into the
#   routed library folder) → history rows for every download/rename/move.
#
# Seeding: the runner destroys its client the moment the torrent completes,
# so seeding stops automatically.
#
# Safety rules (per Cole):
#   - Everything downloads into ONE staging directory first.
#   - Files only move when the route is confident AND move is enabled (either
#     the auto_move flag set at grab time, or the admin's Apply Route click).
#   - Rename only applies to parsed-episode files with a confident show match.
#   - Every rename/move writes a before/after history row.
# =============================================================================

import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import config
import downloads_store
import torrent_routing
from queue_store import get_request, list_requests
from torrent_search import TorrentResult, search_torrents

logger = logging.getLogger(__name__)

_RUNNER_PATH = Path(config.APP_DIR) / "torrent_runner" / "download.mjs"

# Windows: suppress the console window for the Node subprocess.
_CREATE_NO_WINDOW = 0x08000000


class DownloadManager:
    """Owns runner subprocesses and post-processing. One instance per app."""

    def __init__(self, *, on_update: Callable[[int], None] | None = None) -> None:
        # on_update(download_id) is called (from worker threads!) whenever a
        # download's row changed — the desktop app marshals it to the UI.
        self._on_update = on_update
        self._processes: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        downloads_store.initialize_downloads_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def grab(
        self,
        result: TorrentResult,
        *,
        request_id: int | None = None,
        request_title: str | None = None,
        auto_rename: bool | None = None,
        auto_move: bool | None = None,
    ) -> int:
        """Start downloading a search result. Returns the download row id."""
        auto_rename = config.TORRENT_AUTO_RENAME if auto_rename is None else auto_rename
        auto_move = config.TORRENT_AUTO_MOVE if auto_move is None else auto_move

        plan = torrent_routing.plan_route(
            result.title, result.media_type, request_title=request_title
        )
        staging = Path(config.TORRENT_DOWNLOAD_DIR)
        staging.mkdir(parents=True, exist_ok=True)

        download_id = downloads_store.create_download(
            title=result.title, magnet=result.magnet, source=result.source,
            media_type=result.media_type, request_id=request_id,
            staging_dir=str(staging),
            planned_dest=plan.dest_dir if plan.confident else None,
            planned_name=plan.new_filename,
            route_reason=plan.reason,
            auto_rename=auto_rename, auto_move=auto_move,
        )

        thread = threading.Thread(
            target=self._run_download,
            args=(download_id, result.magnet, str(staging), request_title),
            name=f"torrent-dl-{download_id}",
            daemon=True,
        )
        thread.start()
        return download_id

    def cancel(self, download_id: int) -> bool:
        with self._lock:
            proc = self._processes.get(download_id)
        if proc is None or proc.poll() is not None:
            return False
        proc.kill()
        downloads_store.set_status(download_id, "cancelled", completed=True)
        downloads_store.add_history(download_id, "cancelled", before=None, after=None)
        self._notify(download_id)
        return True

    def apply_route(self, download_id: int) -> str:
        """Manually rename+move a completed download per its (re-computed)
        route plan. Returns a human-readable outcome message."""
        row = downloads_store.get_download(download_id)
        if row is None:
            return "Download not found."
        if row.status not in ("downloaded",):
            return f"Can't apply route while status is '{row.status}'."
        request_title = None
        if row.request_id is not None:
            req = get_request(row.request_id)
            if req is not None:
                request_title = req.resolved_title or req.content
        return self._post_process(
            row.download_id, force_move=True, force_rename=True,
            request_title=request_title,
        )

    def auto_grab_open_requests(self) -> list[int]:
        """Grab the best-seeded result for each open request that has no
        download yet. Only called when the auto-grab toggle is on."""
        started: list[int] = []
        already = downloads_store.request_ids_with_downloads()
        for req in list_requests(status="open", limit=100):
            if req.request_id in already or req.found_in_library:
                continue
            query = req.resolved_title or req.content
            media_type = req.media_type if req.media_type != "unknown" else "other"
            try:
                results = search_torrents(query, media_type, limit=10)
            except Exception:
                logger.exception("Auto-grab search failed for request #%s", req.request_id)
                continue
            if not results:
                continue
            best = results[0]  # already sorted by seeders
            if best.seeders <= 0:
                continue
            download_id = self.grab(
                best, request_id=req.request_id, request_title=query,
            )
            logger.info(
                "Auto-grabbed request #%s → download #%s (%s, %s seeders)",
                req.request_id, download_id, best.title, best.seeders,
            )
            started.append(download_id)
        return started

    # ------------------------------------------------------------------
    # Runner subprocess
    # ------------------------------------------------------------------

    def _run_download(self, download_id: int, magnet: str, staging: str,
                      request_title: str | None) -> None:
        cmd = [
            config.NODE_PATH, str(_RUNNER_PATH), magnet, staging,
            str(config.TORRENT_STALL_TIMEOUT_SECONDS),
        ]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as exc:
            logger.exception("Failed to start torrent runner.")
            downloads_store.set_status(
                download_id, "error",
                error=f"couldn't start Node runner: {exc}", completed=True,
            )
            downloads_store.add_history(download_id, "error", before=None, after=str(exc))
            self._notify(download_id)
            return

        with self._lock:
            self._processes[download_id] = proc

        torrent_files: list[dict[str, Any]] = []
        error_message: str | None = None
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("event")
                if kind == "progress":
                    downloads_store.set_progress(download_id, float(event.get("progress") or 0))
                    self._notify(download_id)
                elif kind == "metadata":
                    torrent_files = event.get("files") or []
                elif kind == "done":
                    torrent_files = event.get("files") or torrent_files
                elif kind == "error":
                    error_message = str(event.get("message") or "unknown runner error")
            proc.wait(timeout=30)
        except Exception as exc:
            error_message = error_message or str(exc)
        finally:
            with self._lock:
                self._processes.pop(download_id, None)

        row = downloads_store.get_download(download_id)
        if row is not None and row.status == "cancelled":
            return

        if error_message or proc.returncode not in (0, None):
            downloads_store.set_status(
                download_id, "error",
                error=error_message or f"runner exit code {proc.returncode}",
                completed=True,
            )
            downloads_store.add_history(
                download_id, "error", before=None,
                after=error_message or f"exit {proc.returncode}",
            )
            self._notify(download_id)
            return

        downloads_store.set_progress(download_id, 1.0)
        downloads_store.set_status(download_id, "downloaded", completed=True)
        file_list = ", ".join(f.get("path", "?") for f in torrent_files) or "?"
        downloads_store.add_history(
            download_id, "downloaded", before=None,
            after=f"{staging} :: {file_list}",
        )
        self._notify(download_id)

        # Post-process according to the flags chosen at grab time.
        outcome = self._post_process(download_id, request_title=request_title)
        logger.info("Download #%s post-process: %s", download_id, outcome)
        self._notify(download_id)

    # ------------------------------------------------------------------
    # Post-processing: rename + move with full history
    # ------------------------------------------------------------------

    def _media_files_in_staging(self, row: downloads_store.DownloadRow) -> list[Path]:
        """Locate this download's media files inside the staging dir.

        The runner reports paths relative to staging; we match on the torrent's
        top-level folder/file name to avoid touching other downloads' files.
        """
        staging = Path(row.staging_dir or config.TORRENT_DOWNLOAD_DIR)
        candidates: list[Path] = []
        wanted_exts = torrent_routing.VIDEO_EXTENSIONS | torrent_routing.SUBTITLE_EXTENSIONS
        if not staging.is_dir():
            return []
        # Newest entries first — the download that just finished.
        entries = sorted(staging.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for entry in entries:
            name_match = torrent_routing._folder_similarity(entry.name, row.title) >= 0.5
            if entry.is_file() and entry.suffix.lower() in wanted_exts and name_match:
                candidates.append(entry)
            elif entry.is_dir() and name_match:
                for f in entry.rglob("*"):
                    if f.is_file() and f.suffix.lower() in wanted_exts:
                        candidates.append(f)
                break
            if candidates and entry.is_file():
                break
        return candidates

    def _post_process(
        self, download_id: int, *, force_move: bool = False,
        force_rename: bool = False, request_title: str | None = None,
    ) -> str:
        row = downloads_store.get_download(download_id)
        if row is None:
            return "row vanished"

        do_rename = force_rename or row.auto_rename
        do_move = force_move or row.auto_move

        plan = torrent_routing.plan_route(
            row.title, row.media_type, request_title=request_title
        )
        downloads_store.set_route(
            download_id, planned_dest=plan.dest_dir,
            planned_name=plan.new_filename, route_reason=plan.reason,
        )

        if not do_move and not do_rename:
            return f"left in staging (auto rename/move off) — planned: {plan.describe()}"
        if not plan.confident:
            # Never move on a shaky route; the file stays findable in staging.
            return f"left in staging — route not confident: {plan.reason}"

        files = self._media_files_in_staging(row)
        if not files:
            return "no media files found in staging for this download"

        dest_dir = Path(plan.dest_dir)
        moved_any = False
        for src in files:
            target_name = src.name
            if do_rename and plan.new_filename and src.suffix.lower() in torrent_routing.VIDEO_EXTENSIONS:
                target_name = f"{plan.new_filename}{src.suffix.lower()}"
                if target_name != src.name:
                    downloads_store.add_history(
                        download_id, "renamed", before=src.name, after=target_name,
                    )
            if do_move:
                dest_dir.mkdir(parents=True, exist_ok=True)
                target = dest_dir / target_name
                if target.exists():
                    downloads_store.add_history(
                        download_id, "error", before=str(src),
                        after=f"NOT moved — target exists: {target}",
                    )
                    continue
                shutil.move(str(src), str(target))
                downloads_store.add_history(
                    download_id, "moved", before=str(src), after=str(target),
                )
                moved_any = True
            elif target_name != src.name:
                # Rename in place (staging) without moving.
                target = src.with_name(target_name)
                if not target.exists():
                    src.rename(target)

        if moved_any:
            downloads_store.set_status(download_id, "moved", completed=True)
            self._cleanup_staging_leftovers(row)
            return f"moved to {dest_dir}"
        return f"processed (no move) — planned: {plan.describe()}"

    def _cleanup_staging_leftovers(self, row: downloads_store.DownloadRow) -> None:
        """Remove the download's now-empty (or junk-only) staging folder."""
        staging = Path(row.staging_dir or config.TORRENT_DOWNLOAD_DIR)
        if not staging.is_dir():
            return
        for entry in staging.iterdir():
            if not entry.is_dir():
                continue
            if torrent_routing._folder_similarity(entry.name, row.title) < 0.5:
                continue
            remaining = [f for f in entry.rglob("*") if f.is_file()]
            junk_exts = {".nfo", ".txt", ".jpg", ".png", ".sfv", ".exe", ".url"}
            if all(f.suffix.lower() in junk_exts for f in remaining):
                shutil.rmtree(entry, ignore_errors=True)
            break

    def _notify(self, download_id: int) -> None:
        if self._on_update is not None:
            try:
                self._on_update(download_id)
            except Exception:
                logger.exception("Download update callback failed.")


_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize_for_filesystem(name: str) -> str:
    return _INVALID_FS_CHARS.sub("", name).strip()
