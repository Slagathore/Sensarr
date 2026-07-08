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
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable

import config
import downloads_store
import show_tracker
import shows_store
import torrent_routing
from queue_store import get_request, list_requests
from torrent_search import TorrentResult, search_torrents

logger = logging.getLogger(__name__)


def _resolve_runner_path() -> Path:
    """Locate download.mjs across the layouts it can live in.

    - From source: <repo>/torrent_runner/download.mjs (APP_DIR == repo).
    - Frozen EXE: node_modules can only sit beside the script, so we prefer a
      writable torrent_runner NEXT TO the exe (APP_DIR/torrent_runner);
      PyInstaller also bundles a read-only copy under _internal, and older
      builds used BUNDLE_DIR — check those as fallbacks so the path always
      resolves even if the sibling folder wasn't seeded.
    """
    candidates = [
        Path(config.APP_DIR) / "torrent_runner" / "download.mjs",
        Path(config.APP_DIR) / "_internal" / "torrent_runner" / "download.mjs",
        Path(config.BUNDLE_DIR) / "torrent_runner" / "download.mjs",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]  # default; surfaced as a clear error at grab time


_RUNNER_PATH = _resolve_runner_path()


def _size_prefs(media_type: str) -> tuple[float, float, int]:
    """(preferred MB/min, max MB/min, assumed runtime minutes) per type."""
    prefs = {
        "movie": (config.SIZE_PREF_MB_PER_MIN_MOVIE, config.SIZE_MAX_MB_PER_MIN_MOVIE, 120),
        "tv": (config.SIZE_PREF_MB_PER_MIN_TV, config.SIZE_MAX_MB_PER_MIN_TV, 30),
        "anime": (config.SIZE_PREF_MB_PER_MIN_ANIME, config.SIZE_MAX_MB_PER_MIN_ANIME, 30),
        "xanime": (config.SIZE_PREF_MB_PER_MIN_XANIME, config.SIZE_MAX_MB_PER_MIN_XANIME, 30),
    }
    return prefs.get(media_type, (0.0, 0.0, 30))


def filter_viable_results(results: list[TorrentResult], media_type: str,
                          *, block_cams: bool | None = None) -> list[TorrentResult]:
    """Auto-grab hard filters (vetoes, not preferences):
    - size 0 is never downloaded (unverifiable garbage)
    - cam/telesync releases are dropped for movies when BLOCK_CAMS is on
    - results whose implied MB/min exceeds the max slider are dropped
    """
    from video_quality import is_cam_release

    block = config.BLOCK_CAMS if block_cams is None else block_cams
    _pref, max_rate, minutes = _size_prefs(media_type)

    viable = [r for r in results if r.size_bytes > 0]
    if block and media_type == "movie":
        viable = [r for r in viable if not is_cam_release(r.title)]
    if max_rate > 0:
        cap = max_rate * minutes * 1024 * 1024
        viable = [r for r in viable if r.size_bytes <= cap]
    return viable


def pick_best_result(results: list[TorrentResult],
                     media_type: str) -> TorrentResult | None:
    """Best auto-grab candidate honouring the admin's size preference.

    Assumes hard filters (filter_viable_results) already ran. With no
    preference set (0), the top-seeded result wins. With a MB/min target,
    prefer the result whose size lands closest to the target (2 h for
    movies, 30 min for episodes) on a log scale, seeders as tie-breaker.
    """
    if not results:
        return None
    pref, _max_rate, minutes = _size_prefs(media_type)
    if pref <= 0:
        return results[0]

    target_bytes = pref * minutes * 1024 * 1024

    import math

    def sort_key(r: TorrentResult):
        if not r.size_bytes:
            return (99.0, -r.seeders)
        distance = abs(math.log2(r.size_bytes / target_bytes))
        return (distance, -r.seeders)

    return sorted(results, key=sort_key)[0]

# Windows: suppress the console window for the Node subprocess.
_CREATE_NO_WINDOW = 0x08000000

# ---------------------------------------------------------------------------
# Public tracker list — appended to every magnet before download. Poorly
# announced magnets (one dead tracker) often go from 0 peers to dozens with
# these. Refreshed daily from ngosang/trackerslist; the baked-in list is the
# fallback when offline.
# ---------------------------------------------------------------------------

_TRACKERS_URL = "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
_TRACKERS_CACHE = Path(config.APP_DIR) / "trackers_cache.txt"
_TRACKERS_MAX_AGE_S = 24 * 3600

_BUILTIN_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.theoks.net:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://opentracker.io:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
]


def _public_trackers() -> list[str]:
    """Current tracker list — cached download of trackers_best, else builtin."""
    try:
        if (not _TRACKERS_CACHE.is_file()
                or time.time() - _TRACKERS_CACHE.stat().st_mtime > _TRACKERS_MAX_AGE_S):
            req = urllib.request.Request(_TRACKERS_URL, headers={"User-Agent": "PlexResetButton"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                _TRACKERS_CACHE.write_bytes(resp.read())
    except Exception as exc:
        logger.debug("Tracker list refresh failed (using cache/builtin): %s", exc)
    try:
        if _TRACKERS_CACHE.is_file():
            lines = [ln.strip() for ln in _TRACKERS_CACHE.read_text().splitlines()]
            trackers = [ln for ln in lines if ln.startswith(("udp://", "http://", "https://"))]
            if trackers:
                return trackers
    except OSError:
        pass
    return _BUILTIN_TRACKERS


def add_public_trackers(magnet: str) -> str:
    """Append the public tracker list to a magnet URI (skipping ones present)."""
    if not magnet.startswith("magnet:"):
        return magnet
    existing = set(re.findall(r"tr=([^&]+)", magnet))
    extra = ""
    for tr in _public_trackers():
        quoted = urllib.parse.quote(tr, safe="")
        if quoted not in existing and tr not in existing:
            extra += f"&tr={quoted}"
    return magnet + extra


class _QBitClient:
    """Tiny qBittorrent Web API client (urllib only, cookie auth).

    Used when QBITTORRENT_ENABLED is on — downloads are delegated to a
    running qBittorrent instance instead of the built-in webtorrent runner.
    """

    def __init__(self) -> None:
        self._base = config.QBITTORRENT_URL.rstrip("/")
        jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

    def _post(self, path: str, data: dict[str, str]) -> str:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self._base + path, data=body)
        with self._opener.open(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _get_json(self, path: str) -> Any:
        with self._opener.open(self._base + path, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def login(self) -> None:
        out = self._post("/api/v2/auth/login", {
            "username": config.QBITTORRENT_USERNAME,
            "password": config.QBITTORRENT_PASSWORD,
        })
        if "Ok" not in out:
            raise RuntimeError("qBittorrent login failed — check URL/username/password in Settings.")

    def add_magnet(self, magnet: str, save_path: str, tag: str) -> None:
        self._post("/api/v2/torrents/add", {
            "urls": magnet, "savepath": save_path, "tags": tag,
            "sequentialDownload": "false",
        })

    def info_by_tag(self, tag: str) -> dict | None:
        torrents = self._get_json(f"/api/v2/torrents/info?tag={urllib.parse.quote(tag)}")
        return torrents[0] if torrents else None

    def files(self, torrent_hash: str) -> list[dict]:
        return self._get_json(f"/api/v2/torrents/files?hash={torrent_hash}") or []

    def set_file_priority(self, torrent_hash: str, file_ids: list[int], priority: int) -> None:
        if not file_ids:
            return
        self._post("/api/v2/torrents/filePrio", {
            "hash": torrent_hash,
            "id": "|".join(str(i) for i in file_ids),
            "priority": str(priority),
        })

    def delete_by_tag(self, tag: str, *, delete_files: bool) -> None:
        info = self.info_by_tag(tag)
        if info is None:
            return
        self._post("/api/v2/torrents/delete", {
            "hashes": info.get("hash", ""),
            "deleteFiles": "true" if delete_files else "false",
        })


class DownloadManager:
    """Owns runner subprocesses and post-processing. One instance per app."""

    def __init__(self, *, on_update: Callable[[int], None] | None = None) -> None:
        # on_update(download_id) is called (from worker threads!) whenever a
        # download's row changed — the desktop app marshals it to the UI.
        self._on_update = on_update
        self._processes: dict[int, subprocess.Popen] = {}
        # download_id → qBittorrent tag, for cancel when delegating to qBit.
        self._qbit_tags: dict[int, str] = {}
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
        episode_context: tuple[int, int, int] | None = None,  # (show_id, season, episode)
        replace_path: str | None = None,  # old low-quality file to delete after move
    ) -> int:
        """Start downloading a search result. Returns the download row id."""
        auto_rename = config.TORRENT_AUTO_RENAME if auto_rename is None else auto_rename
        auto_move = config.TORRENT_AUTO_MOVE if auto_move is None else auto_move

        show_id = season = episode = None
        if episode_context is not None:
            show_id, season, episode = episode_context
            show = shows_store.get_show(show_id)
            plan = (
                show_tracker.plan_for_episode(show, season, episode)
                if show is not None
                else torrent_routing.plan_route(result.title, result.media_type)
            )
        else:
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
            show_id=show_id, season=season, episode=episode,
            replace_path=replace_path,
        )
        if episode_context is not None:
            shows_store.set_episode_grab(
                episode_context[0], episode_context[1], episode_context[2], download_id,
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
            qbit_tag = self._qbit_tags.get(download_id)
        if qbit_tag is not None:
            try:
                client = _QBitClient()
                client.login()
                client.delete_by_tag(qbit_tag, delete_files=True)
            except Exception:
                logger.exception("qBittorrent cancel failed for #%s", download_id)
                return False
        elif proc is None or proc.poll() is not None:
            return False
        else:
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
            viable = filter_viable_results(results, media_type)
            if not viable:
                continue
            seeded = [r for r in viable if r.seeders > 0]
            if not seeded:
                # Nothing has seeders — race a handful and keep the winner.
                started.extend(self.start_zero_seeder_race(
                    viable, media_type,
                    request_id=req.request_id, request_title=query,
                ))
                continue
            best = pick_best_result(seeded, media_type)
            if best is None:
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

    def auto_grab_missing_episodes(self, *, limit: int | None = None) -> list[int]:
        """Grab the best torrent for each missing episode of every tracked show.

        The full Sonarr-replacement loop: shows whose episode data is stale
        get re-synced first (so freshly-aired episodes show up as missing),
        then each missing episode without a live grab is searched and the
        best-seeded result downloaded with rename+move forced ON — routing
        for tracked episodes is deterministic (plan_for_episode), so
        auto-placement is safe.

        Guarded by the same lock as scan/sync so a scheduled pass never runs
        concurrently with a manual scan or sync (which would multiply the API
        rate). Returns [] if a Shows operation is already in progress.
        """
        try:
            return show_tracker.run_exclusive(
                "Auto-grab missing", lambda: self._auto_grab_missing_impl(limit),
            )
        except show_tracker.ShowsBusyError:
            logger.info("Auto-grab missing skipped — a Shows scan/sync is already running.")
            return []

    def _auto_grab_missing_impl(self, limit: int | None) -> list[int]:
        limit = config.SHOWS_GRAB_LIMIT_PER_PASS if limit is None else limit
        started: list[int] = []

        for show in shows_store.list_shows():
            if len(started) >= limit:
                break
            # Global toggle grabs for every show; otherwise only shows the
            # admin marked auto-grab (e.g. from the Upcoming boxes).
            if not (config.SHOWS_AUTO_GRAB or show.auto_grab):
                continue
            # Re-sync stale shows so "missing" reflects reality.
            stale = True
            if show.last_synced:
                try:
                    from datetime import datetime, timedelta, timezone
                    synced = datetime.fromisoformat(show.last_synced).replace(tzinfo=timezone.utc)
                    stale = datetime.now(timezone.utc) - synced > timedelta(
                        hours=config.SHOWS_SYNC_MAX_AGE_HOURS
                    )
                except ValueError:
                    pass
            if stale:
                try:
                    show_tracker.sync_show(show.show_id)
                except Exception:
                    logger.exception("Auto-grab: sync failed for '%s'", show.title)
                    continue

            for ep in shows_store.missing_episodes(show.show_id):
                if len(started) >= limit:
                    break
                if ep.grab_download_id is not None:
                    linked = downloads_store.get_download(ep.grab_download_id)
                    if linked is not None and linked.status not in ("error", "cancelled"):
                        continue  # already being handled
                query = f"{show.title} S{ep.season:02d}E{ep.episode:02d}"
                try:
                    results = search_torrents(query, show.media_type, limit=10)
                    if not results and show.media_type in ("anime", "xanime"):
                        # Absolute-numbered releases are common for anime.
                        results = search_torrents(
                            f"{show.title} {ep.episode:02d}", show.media_type, limit=10,
                        )
                except Exception:
                    logger.exception("Auto-grab search failed for %s", query)
                    continue
                viable = filter_viable_results(results, show.media_type)
                if not viable:
                    continue
                seeded = [r for r in viable if r.seeders > 0]
                if not seeded:
                    started.extend(self.start_zero_seeder_race(
                        viable, show.media_type,
                        episode_context=(show.show_id, ep.season, ep.episode),
                    ))
                    continue
                best = pick_best_result(seeded, show.media_type)
                if best is None:
                    continue
                download_id = self.grab(
                    best,
                    episode_context=(show.show_id, ep.season, ep.episode),
                    auto_rename=True, auto_move=True,
                )
                logger.info(
                    "Auto-grabbed %s → download #%s (%s, %s seeders)",
                    query, download_id, best.title, best.seeders,
                )
                started.append(download_id)
        return started

    # ------------------------------------------------------------------
    # Zero-seeder race — when nothing has seeders, try several at once
    # ------------------------------------------------------------------

    def start_zero_seeder_race(
        self, results: list[TorrentResult], media_type: str, *,
        request_id: int | None = None, request_title: str | None = None,
        episode_context: tuple[int, int, int] | None = None,
    ) -> list[int]:
        """All results report 0 seeders: grab up to 5 and monitor for an hour.

        Rules (per Cole): if one finishes, cancel the rest. At the hour mark,
        keep the single most-progressed download (if any moved at all) and
        cancel the others; if nothing progressed, cancel them all.
        """
        pref, _mx, minutes = _size_prefs(media_type)
        target = pref * minutes * 1024 * 1024 if pref > 0 else None

        import math

        def rank(r: TorrentResult):
            if target and r.size_bytes:
                return abs(math.log2(r.size_bytes / target))
            return 0.0

        picks = sorted(results, key=rank)[:5]
        ids = [
            self.grab(r, request_id=request_id, request_title=request_title,
                      episode_context=episode_context)
            for r in picks
        ]
        logger.info("Zero-seeder race started: %d candidate download(s) %s", len(ids), ids)
        threading.Thread(target=self._race_monitor, args=(ids,),
                         name="dl-zero-seeder-race", daemon=True).start()
        return ids

    def _race_monitor(self, ids: list[int], *, duration_s: int = 3600) -> None:
        deadline = time.time() + duration_s

        def cancel_all_except(keep: int | None) -> None:
            for did in ids:
                if did == keep:
                    continue
                row = downloads_store.get_download(did)
                if row is not None and row.status in ("queued", "downloading"):
                    self.cancel(did)

        while time.time() < deadline:
            time.sleep(120)
            rows = [downloads_store.get_download(d) for d in ids]
            finished = [r for r in rows if r is not None
                        and r.status in ("downloaded", "moved")]
            if finished:
                # Two finished at once? Keep the bigger file (better quality).
                winner = max(finished, key=lambda r: r.progress).download_id
                cancel_all_except(winner)
                logger.info("Zero-seeder race won by download #%s", winner)
                return
            alive = [r for r in rows if r is not None
                     and r.status in ("queued", "downloading")]
            if not alive:
                return  # everything errored/cancelled on its own

        rows = [downloads_store.get_download(d) for d in ids]
        progressing = [r for r in rows if r is not None
                       and r.status == "downloading" and r.progress > 0.0]
        if progressing:
            winner = max(progressing, key=lambda r: r.progress).download_id
            logger.info("Zero-seeder race: keeping #%s after 1h, cancelling rest", winner)
            cancel_all_except(winner)
        else:
            logger.info("Zero-seeder race: nothing progressed in 1h — cancelling all")
            cancel_all_except(None)

    # ------------------------------------------------------------------
    # Quality replacement — swap a cam/low-bitrate movie for a proper one
    # ------------------------------------------------------------------

    def replace_low_quality_movie(self, title_query: str, old_path: str) -> int | None:
        """Search for a NON-cam release of a movie and download it; the old
        file is deleted automatically once the new one lands in the library.

        Hard rules: never a cam/telesync (regardless of the global toggle —
        that's what we're replacing), never size 0, and at least the
        low-quality threshold in MB/min so we don't swap junk for junk.
        Returns the download id, or None when nothing acceptable was found.
        """
        try:
            results = search_torrents(title_query, "movie", limit=25)
        except Exception:
            logger.exception("Replacement search failed for %s", title_query)
            return None

        viable = filter_viable_results(results, "movie", block_cams=True)
        floor_bytes = config.LOW_QUALITY_MB_PER_MIN * 120 * 1024 * 1024
        viable = [r for r in viable if r.size_bytes >= floor_bytes and r.seeders > 0]
        best = pick_best_result(viable, "movie")
        if best is None:
            logger.info("No acceptable non-cam replacement found for %s", title_query)
            return None
        download_id = self.grab(
            best, request_title=title_query,
            auto_rename=True, auto_move=True, replace_path=old_path,
        )
        logger.info("Replacement grab for %s → download #%s (%s)",
                    title_query, download_id, best.title)
        return download_id

    # ------------------------------------------------------------------
    # Runner subprocess
    # ------------------------------------------------------------------

    def _run_download(self, download_id: int, magnet: str, staging: str,
                      request_title: str | None) -> None:
        # Extra public trackers help poorly-announced magnets find peers.
        magnet = add_public_trackers(magnet)
        if config.QBITTORRENT_ENABLED:
            self._run_download_qbit(download_id, magnet, staging, request_title)
            return
        self._run_download_node(download_id, magnet, staging, request_title)

    def _run_download_qbit(self, download_id: int, magnet: str, staging: str,
                           request_title: str | None) -> None:
        """Delegate one download to qBittorrent and poll it to completion."""
        tag = f"prb-{download_id}"
        try:
            client = _QBitClient()
            client.login()
            client.add_magnet(magnet, staging, tag)
        except Exception as exc:
            logger.exception("qBittorrent add failed.")
            downloads_store.set_status(
                download_id, "error", error=f"qBittorrent: {exc}", completed=True)
            downloads_store.add_history(download_id, "error", before=None, after=str(exc))
            self._notify(download_id)
            return

        with self._lock:
            self._qbit_tags[download_id] = tag

        error_message: str | None = None
        last_progress_at = time.time()
        last_progress = -1.0
        files_pruned = False
        try:
            while True:
                time.sleep(3)
                row = downloads_store.get_download(download_id)
                if row is None or row.status == "cancelled":
                    return
                info = client.info_by_tag(tag)
                if info is None:
                    # Torrent may take a moment to appear after add.
                    if time.time() - last_progress_at > 60:
                        error_message = "torrent never appeared in qBittorrent"
                        break
                    continue
                # Season pack, single wanted episode: deselect every other
                # video file so only the target episode downloads.
                if (not files_pruned and row.season is not None
                        and row.episode is not None and info.get("hash")):
                    files_pruned = True
                    try:
                        self._qbit_prune_to_episode(
                            client, str(info["hash"]), row.season, row.episode)
                    except Exception:
                        logger.debug("qBittorrent file pruning failed.", exc_info=True)
                progress = float(info.get("progress") or 0.0)
                state = str(info.get("state") or "")
                if progress > last_progress:
                    last_progress = progress
                    last_progress_at = time.time()
                    downloads_store.set_progress(download_id, progress)
                    self._notify(download_id)
                if state in ("error", "missingFiles"):
                    error_message = f"qBittorrent state: {state}"
                    break
                if progress >= 1.0 or state in ("uploading", "stalledUP", "pausedUP",
                                                "queuedUP", "checkingUP", "stoppedUP"):
                    break  # download complete (seeding states)
                if time.time() - last_progress_at > config.TORRENT_STALL_TIMEOUT_SECONDS:
                    error_message = "stalled — no progress within the timeout"
                    break
        except Exception as exc:
            error_message = str(exc)
        finally:
            # Stop seeding + drop the torrent (files stay on disk).
            try:
                client.delete_by_tag(tag, delete_files=False)
            except Exception:
                logger.debug("qBittorrent post-download delete failed.", exc_info=True)
            with self._lock:
                self._qbit_tags.pop(download_id, None)

        if error_message:
            downloads_store.set_status(download_id, "error", error=error_message, completed=True)
            downloads_store.add_history(download_id, "error", before=None, after=error_message)
            self._notify(download_id)
            return

        downloads_store.set_progress(download_id, 1.0)
        downloads_store.set_status(download_id, "downloaded", completed=True)
        downloads_store.add_history(download_id, "downloaded", before=None,
                                    after=f"{staging} (via qBittorrent)")
        self._notify(download_id)
        outcome = self._post_process(download_id, request_title=request_title)
        logger.info("Download #%s post-process: %s", download_id, outcome)
        self._notify(download_id)

    @staticmethod
    def _qbit_prune_to_episode(client: _QBitClient, torrent_hash: str,
                               season: int, episode: int) -> None:
        """Inside a multi-file torrent, keep only the wanted episode's video
        (plus subtitles); everything else is set to priority 0 (skip)."""
        files = client.files(torrent_hash)
        if len(files) < 2:
            return
        keep_exts = torrent_routing.VIDEO_EXTENSIONS | torrent_routing.SUBTITLE_EXTENSIONS
        wanted_videos: list[int] = []
        skip: list[int] = []
        for idx, f in enumerate(files):
            name = Path(str(f.get("name") or "")).name
            suffix = Path(name).suffix.lower()
            file_id = int(f.get("index", idx))
            if suffix not in keep_exts:
                skip.append(file_id)
                continue
            parsed = torrent_routing.parse_torrent_name(name)
            matches = (parsed.episode == episode
                       and (parsed.season is None or parsed.season == season))
            if suffix in torrent_routing.VIDEO_EXTENSIONS:
                (wanted_videos if matches else skip).append(file_id)
        # Only prune when we positively identified the target episode —
        # otherwise download everything rather than guess wrong.
        if wanted_videos:
            client.set_file_priority(torrent_hash, skip, 0)
            logger.info("qBittorrent: pruned pack to S%02dE%02d (%d file(s) skipped)",
                        season, episode, len(skip))

    def _run_download_node(self, download_id: int, magnet: str, staging: str,
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

        # Episode-linked grabs route deterministically — we KNOW the show,
        # season, and episode, so no fuzzy folder matching is involved.
        plan = None
        if row.show_id is not None and row.season is not None and row.episode is not None:
            show = shows_store.get_show(row.show_id)
            if show is not None:
                plan = show_tracker.plan_for_episode(show, row.season, row.episode)
        if plan is None:
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

        # Canonical show name for renames: the tracked show's title for
        # episode-linked grabs, else the matched library folder's name.
        show_name = None
        if row.show_id is not None:
            show = shows_store.get_show(row.show_id)
            if show is not None:
                show_name = show.title
        if show_name is None and plan.show_folder:
            show_name = Path(plan.show_folder).name

        video_files = [f for f in files if f.suffix.lower() in torrent_routing.VIDEO_EXTENSIONS]

        dest_dir = Path(plan.dest_dir)
        moved_any = False
        for src in files:
            target_name = src.name
            if do_rename and src.suffix.lower() in torrent_routing.VIDEO_EXTENSIONS:
                new_stem = self._episode_stem_for_file(
                    src, show_name=show_name, plan=plan,
                    single_video=(len(video_files) == 1),
                )
                if new_stem:
                    target_name = f"{new_stem}{src.suffix.lower()}"
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
            # Quality replacement: the new file is in place — retire the old
            # cam/low-bitrate copy it replaces (recycle bin when available).
            if row.replace_path:
                old = Path(row.replace_path)
                if old.is_file():
                    try:
                        try:
                            from send2trash import send2trash
                            send2trash(str(old))
                        except ImportError:
                            old.unlink()
                        downloads_store.add_history(
                            download_id, "replaced", before=str(old),
                            after=f"deleted — superseded by download #{download_id}",
                        )
                        try:
                            from library_index import remove_from_index
                            remove_from_index([str(old)])
                        except Exception:
                            pass
                        logger.info("Replaced low-quality file: %s", old)
                    except OSError as exc:
                        downloads_store.add_history(
                            download_id, "error", before=str(old),
                            after=f"could not delete replaced file: {exc}",
                        )
            # Close the loop for tracked episodes: mark it on-disk right away
            # instead of waiting for the next full sync.
            if row.show_id is not None and row.season is not None and row.episode is not None:
                moved_video = next(
                    (h.after_value for h in downloads_store.list_history(limit=20)
                     if h.download_id == download_id and h.action == "moved"
                     and h.after_value),
                    str(dest_dir),
                ) or str(dest_dir)
                shows_store.set_episode_file(row.show_id, row.season, row.episode, moved_video)
            return f"moved to {dest_dir}"
        return f"processed (no move) — planned: {plan.describe()}"

    @staticmethod
    def _episode_stem_for_file(
        src: Path, *, show_name: str | None,
        plan: torrent_routing.RoutePlan, single_video: bool,
    ) -> str | None:
        """Canonical stem ("Show - S01E05") for one video file.

        Season packs are the reason this parses PER FILE: the pack-level plan
        only knows the season, so renaming every file to the same plan-level
        name would collide (and previously they were left unrenamed). A file
        whose own name parses to an episode gets its own SxxEyy; otherwise a
        lone video can still use the plan's single-episode name."""
        if show_name:
            parsed = torrent_routing.parse_torrent_name(src.name)
            if parsed.episode is not None:
                season = parsed.season
                if season is None:
                    season = (plan.parsed.season if plan.parsed and plan.parsed.season else 1)
                return torrent_routing.sanitize_for_filesystem(
                    f"{show_name} - S{season:02d}E{parsed.episode:02d}"
                )
        if plan.new_filename and single_video:
            return plan.new_filename
        return None

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


# Re-exported for backwards compatibility; the canonical home is
# torrent_routing.sanitize_for_filesystem.
sanitize_for_filesystem = torrent_routing.sanitize_for_filesystem
