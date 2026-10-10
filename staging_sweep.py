"""Sweep partial files left in staging by stalled or cancelled downloads.

The torrent runner keeps partials on purpose so a retry of the same torrent can
resume from them. This module removes the ones that are no longer worth
resuming. It is a data-loss path, so every doubt resolves to "keep the file":

* only error/cancelled rows are candidates, never quarantined ones
* only paths listed in a row's files_json (plus "<path>.part") are touched
* every path must resolve inside the staging root
* a path also owned by any live row, or by a row kept this run, is left alone
* a row whose torrent (info hash) is also live or kept is left alone entirely:
  a retry of the same torrent writes the same paths, and engines that only
  record their file list at the end (Real-Debrid, qBittorrent) own nothing yet
* a file written to recently is left alone whatever the rows say
"""
from __future__ import annotations

import json
import logging
import os
import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import downloads_store

logger = logging.getLogger(__name__)

_CANDIDATE_STATUSES = ("error", "cancelled")


@dataclass
class SweepReport:
    rows_considered: int = 0
    rows_swept: int = 0
    files_deleted: int = 0
    bytes_freed: int = 0
    kept_resumable: int = 0
    skipped_shared: int = 0
    skipped_outside_root: int = 0
    skipped_recent: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    # (download_id, path, size) for dry runs and real runs alike
    planned: list[tuple[int, str, int]] = field(default_factory=list)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return _naive_utc(datetime.fromisoformat(str(text).strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _within(path: Path, root: Path) -> bool:
    return path != root and root in path.parents


_BTIH_RE = re.compile(r"btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)


def _infohash(row: downloads_store.DownloadRow) -> str:
    m = _BTIH_RE.search(row.magnet or "")
    return m.group(1).lower() if m else ""


def _manifest(row: downloads_store.DownloadRow) -> list[str]:
    if not row.files_json:
        return []
    try:
        data = json.loads(row.files_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str) and item.strip()]


def _row_paths(row: downloads_store.DownloadRow, root: Path):
    """Yield (resolved_path | None, raw) for each manifest entry and its .part.

    None means the entry resolved outside the root (or could not be resolved).
    """
    base = Path(row.staging_dir) if row.staging_dir else root
    for rel in _manifest(row):
        for candidate in (rel, rel + ".part"):
            try:
                resolved = (base / candidate).resolve()
            except (OSError, ValueError, RuntimeError):
                yield None, candidate
                continue
            yield (resolved if _within(resolved, root) else None), candidate


_LIVE_STATUSES = ("queued", "downloading", "stopped", "downloaded")


def _is_pinned(row: downloads_store.DownloadRow) -> bool:
    """True for a download of ONE file inside a multi-file torrent (a
    Torrentio pick from a season pack)."""
    try:
        want = json.loads(row.want_json or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(want, dict) and (
        bool(want.get("file_name")) or want.get("file_idx") is not None)


def _recorded_neighbours(row: downloads_store.DownloadRow) -> list[str]:
    """The exact neighbour paths the runner reported for this download."""
    paths: list[str] = []
    for entry in downloads_store.history_for_download(row.download_id):
        if entry.action != "neighbours" or not entry.after_value:
            continue
        try:
            data = json.loads(entry.after_value)
        except (TypeError, ValueError):
            continue
        if isinstance(data, list):
            paths.extend(str(p) for p in data if isinstance(p, str) and p.strip())
    return paths


def remove_pack_neighbours(row: downloads_store.DownloadRow, *,
                           staging_root: Path | None = None,
                           min_age_hours: float = 0.0,
                           now: datetime | None = None) -> int:
    """Delete the partial neighbour files a pinned download leaves behind.

    A piece that straddles two files is written into both, so fetching one
    episode of a pack also writes the edges of the episodes beside it (and on
    NTFS a write near the end of a new file allocates all of it). The runner
    reports exactly which files those are; only those paths are candidates.
    Nothing is deleted when another download of the same torrent is live, or
    when any other live download has not published its file list yet (it may
    be writing a same-named file of a different torrent), or for a path any
    download row lists. Returns the number of files deleted."""
    if not _is_pinned(row):
        return 0
    candidates = _recorded_neighbours(row)
    if not candidates:
        return 0
    root = Path(staging_root if staging_root is not None
                else config.TORRENT_DOWNLOAD_DIR).resolve()
    base = Path(row.staging_dir).resolve() if row.staging_dir else root
    ih = _infohash(row)
    owned: set[str] = set()
    for other in downloads_store.list_downloads(limit=10**9, include_removed=True):
        if other.download_id == row.download_id:
            continue
        live = other.status in _LIVE_STATUSES and other.removed_at is None
        if live and ih and _infohash(other) == ih:
            return 0  # the same torrent is being fetched by someone else
        if (other.status == "downloading" and other.removed_at is None
                and not _manifest(other)):
            # A running engine is writing files it has not named yet (only a
            # running one can: queued, stopped and finished rows write nothing).
            return 0
        for resolved, _ in _row_paths(other, root):
            if resolved is not None:
                owned.add(_key(resolved))
    for resolved, _ in _row_paths(row, root):
        if resolved is not None:
            owned.add(_key(resolved))
    current = _naive_utc(now) if now is not None else _naive_utc(datetime.now(timezone.utc))
    cutoff = calendar.timegm(current.timetuple()) - min_age_hours * 3600
    removed = 0
    for rel in candidates:
        try:
            path = (base / rel).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        # Every single path is checked against the root (resolve() follows
        # links and junctions, so one pointing outside is refused here).
        if not _within(path, root) or _key(path) in owned:
            continue
        try:
            if not path.is_file():
                continue
            if min_age_hours and path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.debug("Neighbour cleanup skipped %s: %s", path, exc)
            continue
        parent = path.parent
        while _within(parent, root):
            try:
                parent.rmdir()  # only when empty
            except OSError:
                break
            parent = parent.parent
    if removed:
        logger.info("Removed %d partial neighbour file(s) of download #%s",
                    removed, row.download_id)
    return removed


def sweep_stalled_leftovers(
    *, now: datetime | None = None, dry_run: bool = False,
    min_age_days: float = 2.0, keep_progress: float = 0.5,
    keep_days: float = 30.0, staging_root: Path | None = None,
    recent_write_hours: float = 24.0,
) -> SweepReport:
    report = SweepReport(dry_run=dry_run)
    current = _naive_utc(now) if now is not None else _naive_utc(datetime.now(timezone.utc))
    root = Path(staging_root if staging_root is not None
                else config.TORRENT_DOWNLOAD_DIR).resolve()

    rows = downloads_store.list_downloads(limit=10**9, include_removed=True)

    protected: set[str] = set()
    protected_hashes: set[str] = set()
    to_sweep: list[downloads_store.DownloadRow] = []

    for row in rows:
        is_candidate = row.status in _CANDIDATE_STATUSES
        quarantined = bool(row.removed_reason and row.removed_reason.startswith("quarantine:"))
        if not is_candidate or quarantined:
            # live rows, and quarantined bytes kept for adoption, own their
            # paths. Only a LIVE row (or quarantine) shields the whole torrent
            # by hash: a finished one already moved its files out, and letting
            # it shield forever left failed sibling partials of a pack behind.
            if _infohash(row) and (quarantined or (
                    row.status in _LIVE_STATUSES and row.removed_at is None)):
                protected_hashes.add(_infohash(row))
            for resolved, _ in _row_paths(row, root):
                if resolved is not None:
                    protected.add(_key(resolved))
            continue

        report.rows_considered += 1
        stamp = _parse_ts(row.completed_at) or _parse_ts(row.created_at)
        keep = False
        if stamp is None:
            keep = True  # unknown age: never delete
        else:
            age = current - stamp
            if age < timedelta(days=min_age_days):
                keep = True
            elif (float(row.progress or 0.0) >= keep_progress
                  and age < timedelta(days=keep_days)):
                keep = True
                report.kept_resumable += 1
        if keep:
            if _infohash(row):
                protected_hashes.add(_infohash(row))
            for resolved, _ in _row_paths(row, root):
                if resolved is not None:
                    protected.add(_key(resolved))
        else:
            to_sweep.append(row)

    seen: set[str] = set()
    # Same clock as the age rules (`now` is naive UTC).
    recent_cutoff = (calendar.timegm(current.timetuple())
                     - recent_write_hours * 3600)
    for row in to_sweep:
        deleted_n = 0
        deleted_bytes = 0
        if _infohash(row) and _infohash(row) in protected_hashes:
            # The same torrent is being (re)fetched or kept: its paths are
            # someone else's now, even where that row has no file list yet.
            report.skipped_shared += 1
            continue
        for resolved, raw in _row_paths(row, root):
            if resolved is None:
                report.skipped_outside_root += 1
                continue
            k = _key(resolved)
            if k in seen:
                continue
            try:
                if not resolved.is_file():
                    continue
            except OSError as exc:
                report.errors.append(f"{resolved}: {exc}")
                continue
            if k in protected:
                report.skipped_shared += 1
                continue
            seen.add(k)
            try:
                stat = resolved.stat()
            except OSError as exc:
                report.errors.append(f"{resolved}: {exc}")
                continue
            if stat.st_mtime >= recent_cutoff:
                # Something wrote here recently: an engine whose row we cannot
                # see owning it (or a hand copy). Not ours to judge.
                report.skipped_recent += 1
                continue
            size = stat.st_size
            report.planned.append((row.download_id, str(resolved), size))
            if dry_run:
                continue
            try:
                resolved.unlink()
            except OSError as exc:
                report.errors.append(f"{resolved}: {exc}")
                continue
            deleted_n += 1
            deleted_bytes += size
            parent = resolved.parent
            while _within(parent, root):
                try:
                    parent.rmdir()  # only succeeds when empty
                except OSError:
                    break
                parent = parent.parent

        if not dry_run and _is_pinned(row):
            try:
                deleted_n += remove_pack_neighbours(
                    row, staging_root=root, min_age_hours=recent_write_hours,
                    now=current)
            except Exception as exc:
                report.errors.append(f"neighbours of {row.download_id}: {exc}")
        if deleted_n:
            report.rows_swept += 1
            report.files_deleted += deleted_n
            report.bytes_freed += deleted_bytes
            try:
                downloads_store.add_history(
                    row.download_id, "swept", before=None,
                    after=(f"staging leftovers removed: {deleted_n} file(s), "
                           f"{deleted_bytes // (1024 * 1024)} MB"))
            except Exception as exc:  # history is best-effort
                report.errors.append(f"history for {row.download_id}: {exc}")

    if dry_run:
        report.rows_swept = len({p[0] for p in report.planned})

    logger.info(
        "Staging sweep%s: %d row(s) considered, %d swept, %d file(s) %s, "
        "%.1f MB, %d kept resumable, %d shared, %d recent, %d outside root, "
        "%d error(s)",
        " (dry run)" if dry_run else "", report.rows_considered,
        report.rows_swept,
        len(report.planned) if dry_run else report.files_deleted,
        "planned" if dry_run else "deleted",
        (sum(p[2] for p in report.planned) if dry_run else report.bytes_freed) / (1024 * 1024),
        report.kept_resumable, report.skipped_shared, report.skipped_recent,
        report.skipped_outside_root, len(report.errors))
    return report
