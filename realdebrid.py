# =============================================================================
# realdebrid.py
# =============================================================================
# Real-Debrid REST client plus a "fetch this magnet" pipeline.
#
# Why this exists: a cached torrent on Real-Debrid is an instant HTTPS download
# at full line speed, with no swarm, no seeders and no ratio. The pipeline here
# adds the magnet, picks the files we want, waits (or notices it is already
# cached), then streams the files into the same staging layout the webtorrent
# runner produces so the rest of the app cannot tell the difference.
#
# Network access goes through ONE injectable transport so tests never touch
# the network:
#   transport(method, url, headers, data: bytes | None, timeout)
#       -> (status, body_bytes)
# File bytes go through a second injectable, `download` (see fetch()).
#
# The API token never appears in a log line, exception message or repr.
# =============================================================================

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

logger = logging.getLogger(__name__)

API_BASE = "https://api.real-debrid.com/rest/1.0"
USER_AGENT = "Sensarr/1.6"
CHUNK_SIZE = 1024 * 1024
# Tries per file before a short or dropped HTTPS stream counts as a failure.
DOWNLOAD_ATTEMPTS = 4

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts",
              ".m2ts", ".webm", ".mpg", ".mpeg")
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".sub", ".vtt")

ACTIVE_STATUSES = {"magnet_conversion", "queued", "downloading",
                   "compressing", "uploading"}
# Seeing one of these means it was NOT instantly cached.
_NOT_CACHED_STATUSES = {"queued", "downloading", "compressing", "uploading"}
DEAD_STATUSES = {"magnet_error", "error", "virus", "dead"}

Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, bytes]"]


class RealDebridError(Exception):
    """Any Real-Debrid failure. `status` is the HTTP status (or None),
    `error_code` is RD's own numeric code when it sent one."""

    def __init__(self, message: str, status: int | None = None,
                 error_code: int | None = None):
        super().__init__(message)
        self.status = status
        self.error_code = error_code


class RealDebridAuthError(RealDebridError):
    """401 bad token or 403 permission (for example, not premium)."""


class RealDebridStalled(RealDebridError):
    """The remote download made no progress for too long."""


class RealDebridCancelled(RealDebridError):
    """The caller asked to stop."""


class RealDebridDead(RealDebridError):
    """RD reports the torrent as magnet_error, error, virus or dead."""


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

def _default_transport(method: str, url: str, headers: dict,
                       data: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, body


class RealDebridClient:
    """Thin wrapper over the REST API. 429 gets a short bounded retry."""

    MAX_RETRIES = 3

    def __init__(self, token: str, *, transport: Transport | None = None,
                 timeout: float = 20, sleep: Callable[[float], None] = time.sleep):
        self._token = token
        self._transport = transport or _default_transport
        self._timeout = timeout
        self._sleep = sleep

    def __repr__(self) -> str:
        return "RealDebridClient(token=***)"

    def _scrub(self, text: str) -> str:
        if self._token:
            text = text.replace(self._token, "***")
        return text

    def _request(self, method: str, path: str, form: dict | None = None):
        url = API_BASE + path
        headers = {"Authorization": f"Bearer {self._token}",
                   "User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        attempt = 0
        while True:
            try:
                status, body = self._transport(method, url, headers, data,
                                               self._timeout)
            except Exception as exc:
                raise RealDebridError(
                    f"Real-Debrid {method} {path} failed: "
                    f"{self._scrub(str(exc))}") from None
            if status == 429 and attempt < self.MAX_RETRIES:
                self._sleep(2 ** attempt)
                attempt += 1
                continue
            break
        if 200 <= status < 300:
            if not body:
                return {}
            try:
                return json.loads(body.decode("utf-8"))
            except ValueError:
                raise RealDebridError(
                    f"Real-Debrid {method} {path}: bad JSON", status) from None
        message, code = f"HTTP {status}", None
        try:
            err = json.loads(body.decode("utf-8"))
            if isinstance(err, dict):
                message = str(err.get("error") or message)
                code = err.get("error_code")
        except (ValueError, UnicodeDecodeError):
            pass
        text = self._scrub(f"Real-Debrid {method} {path}: {message}")
        cls = RealDebridAuthError if status in (401, 403) else RealDebridError
        raise cls(text, status, code if isinstance(code, int) else None)

    def user(self) -> dict:
        return self._request("GET", "/user")

    def add_magnet(self, magnet: str) -> str:
        return str(self._request("POST", "/torrents/addMagnet",
                                 {"magnet": magnet})["id"])

    def info(self, torrent_id: str) -> dict:
        return self._request("GET", f"/torrents/info/{torrent_id}")

    def select_files(self, torrent_id: str, file_ids: list[int] | str) -> None:
        files = file_ids if isinstance(file_ids, str) else \
            ",".join(str(i) for i in file_ids)
        self._request("POST", f"/torrents/selectFiles/{torrent_id}",
                      {"files": files})

    def delete(self, torrent_id: str) -> None:
        self._request("DELETE", f"/torrents/delete/{torrent_id}")

    def unrestrict(self, link: str) -> dict:
        return self._request("POST", "/unrestrict/link", {"link": link})


# --------------------------------------------------------------------------
# File choice
# --------------------------------------------------------------------------

def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _name_key(name: str) -> str:
    """A file name with case and punctuation dropped, so Torrentio's spelling
    of a pack file still matches RD's."""
    return re.sub(r"[^0-9a-z]+", "", _basename(name).lower())


def served_wanted_file(all_files: list[dict], want_file_name: str | None) -> bool:
    """False only when the pinned file IS in the torrent but RD selected other
    files instead. Nothing pinned by name, or a name the torrent does not list
    (the index fallback picked), cannot be judged and passes."""
    if not want_file_name:
        return True
    wanted = _name_key(want_file_name)
    listed = [f for f in all_files if _name_key(str(f.get("path", ""))) == wanted]
    return not listed or any(f.get("selected") for f in listed)


def choose_files(files: list[dict], *, want_file_name: str | None = None,
                 want_file_idx: int | None = None,
                 video_exts: tuple[str, ...] = VIDEO_EXTS,
                 subtitle_exts: tuple[str, ...] = SUBTITLE_EXTS) -> list[int]:
    """Pick RD file ids (1-based) to select. Pure.

    Prefer the exact file name Torrentio named, then Torrentio's 0-based
    file index, else every video (plus subtitles named after a chosen video).
    Never empty while a video exists; with no video at all, take everything.
    """
    def is_video(f): return _basename(str(f.get("path", ""))).lower().endswith(video_exts)

    if want_file_name:
        wanted = want_file_name.lower()
        for f in files:
            if _basename(str(f.get("path", ""))).lower() == wanted:
                return [f["id"]]
        loose = [f for f in files
                 if _name_key(str(f.get("path", ""))) == _name_key(want_file_name)]
        if len(loose) == 1:
            return [loose[0]["id"]]
    if want_file_idx is not None:
        for f in files:
            if f.get("id") == want_file_idx + 1 and is_video(f):
                return [f["id"]]
    videos = [f for f in files if is_video(f)]
    if not videos:
        return [f["id"] for f in files]
    stems = [_stem(_basename(str(v["path"]))).lower() for v in videos]
    chosen = []
    for f in files:
        if f in videos:
            chosen.append(f["id"])
            continue
        name = _basename(str(f.get("path", ""))).lower()
        if name.endswith(subtitle_exts):
            s = _stem(name)
            if any(s == st or s.startswith(st + ".") for st in stems):
                chosen.append(f["id"])
    return chosen


# --------------------------------------------------------------------------
# Fetch pipeline
# --------------------------------------------------------------------------

@dataclass
class FetchResult:
    rel_paths: list[str]
    torrent_id: str
    cached: bool
    total_bytes: int


def _default_download(url: str, start: int) -> tuple[int, Iterator[bytes]]:
    """Stream `url` from byte `start`. Returns (status, chunk iterator)."""
    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        return exc.code, iter(())

    def gen():
        try:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk
        finally:
            resp.close()
    return resp.status, gen()


def _safe_rel(path: str, folder: str | None) -> str:
    """POSIX relative path with any '..' / empty parts removed."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if folder:
        parts = [p for p in folder.replace("\\", "/").split("/")
                 if p not in ("", ".", "..")][:1] + parts
    return "/".join(parts)


def fetch(client: RealDebridClient, magnet: str, dest_dir: Path, *,
          want_file_name: str | None = None, want_file_idx: int | None = None,
          on_progress: Callable[[str, float], None] | None = None,
          should_cancel: Callable[[], bool] | None = None,
          stall_timeout_s: float = 900, poll_s: float = 5,
          max_total_s: float | None = None, delete_after: bool = True,
          chooser: Callable[[list[dict]], list[int]] | None = None,
          on_files: Callable[[list[str]], None] | None = None,
          download: Callable[[str, int], tuple[int, Iterable[bytes]]] | None = None,
          sleep: Callable[[float], None] = time.sleep,
          monotonic: Callable[[], float] = time.monotonic) -> FetchResult:
    """Add `magnet` to RD, wait for it, and download the files into dest_dir.

    Raises RealDebridStalled / RealDebridDead / RealDebridCancelled; in each
    case the RD torrent is deleted first (a cancelled download keeps its
    .part file on disk so it can resume).

    `chooser(files) -> ids` overrides the file pick (the caller knows the
    wanted season/episode); an empty answer falls back to choose_files.
    """
    download = download or _default_download
    dest_dir = Path(dest_dir)
    progress = on_progress or (lambda stage, frac: None)

    def cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    def drop(tid: str) -> None:
        try:
            client.delete(tid)
        except RealDebridError as exc:
            logger.warning("Could not delete RD torrent %s: %s", tid, exc)

    tid = client.add_magnet(magnet)
    started = monotonic()
    best_progress, last_gain = -1.0, started
    seen_active, selected = False, False
    info: dict = {}

    while True:
        if cancelled():
            drop(tid)
            raise RealDebridCancelled("Cancelled")
        info = client.info(tid)
        status = str(info.get("status") or "")
        if status in DEAD_STATUSES:
            drop(tid)
            raise RealDebridDead(f"Real-Debrid reports torrent {status}")
        if status == "downloaded":
            break
        if status == "waiting_files_selection" and not selected:
            files = info.get("files") or []
            ids = chooser(files) if chooser is not None else []
            if not ids:
                ids = choose_files(files, want_file_name=want_file_name,
                                   want_file_idx=want_file_idx)
            client.select_files(tid, ids)
            selected = True
            last_gain = monotonic()
            continue
        if status in ACTIVE_STATUSES or status == "waiting_files_selection":
            if status in _NOT_CACHED_STATUSES:
                seen_active = True
            pct = float(info.get("progress") or 0)
            progress("remote", pct / 100.0)
            now = monotonic()
            if pct > best_progress:
                best_progress, last_gain = pct, now
            if now - last_gain >= stall_timeout_s:
                drop(tid)
                raise RealDebridStalled(
                    f"No progress for {int(stall_timeout_s)}s ({status})")
            if max_total_s is not None and now - started >= max_total_s:
                drop(tid)
                raise RealDebridStalled(f"Exceeded {int(max_total_s)}s total")
        sleep(poll_s)

    cached = not seen_active
    all_files = info.get("files") or []
    chosen = [f for f in all_files if f.get("selected")]
    if not served_wanted_file(all_files, want_file_name):
        # Seen 2026-10-10: the S02E07 and S02E08 pins of a Monster pack came
        # back as S02E06. Never save one episode under another's download.
        drop(tid)
        served = ", ".join(_basename(str(f.get("path", ""))) for f in chosen)
        raise RealDebridError(
            f"Real-Debrid served {served[:160] or 'nothing'} instead of "
            f"{want_file_name}")
    links = info.get("links") or []
    # The torrent's own name is the root folder webtorrent would create. RD's
    # "filename" becomes the SELECTED file's name when only part of a pack is
    # selected (seen live 2026-10-10), so prefer "original_filename".
    folder = (str(info.get("original_filename") or info.get("filename") or "")
              if len(all_files) > 1 else None)

    # Resolve every link first so the local progress total is known.
    plan = []
    for f, link in zip(chosen, links):
        un = client.unrestrict(link)
        plan.append((_safe_rel(str(f.get("path", "")) or un.get("filename", ""),
                               folder),
                     str(un["download"]), int(un.get("filesize") or f.get("bytes") or 0)))
    total = sum(p[2] for p in plan)
    done = 0
    rel_paths: list[str] = []
    if on_files is not None:
        # Tell the caller what will appear in dest_dir BEFORE writing it, so
        # cleanup elsewhere knows these paths are taken.
        on_files([p[0] for p in plan])

    for rel, url, size in plan:
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == size:
            done += size
            rel_paths.append(rel)
            continue
        part = target.with_name(target.name + ".part")
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        # A stream that ends early (seen live 2026-10-10: 7 KB short of 583 MB)
        # or drops mid-way resumes from where it stopped with a Range request,
        # instead of throwing the whole file away. Gives up after
        # DOWNLOAD_ATTEMPTS tries, or as soon as a try makes no progress.
        attempts = 0
        while have < size or (size == 0 and attempts == 0):
            attempts += 1
            before = have
            try:
                status_code, chunks = download(url, have)
                if status_code == 416:
                    part.unlink(missing_ok=True)
                    have = 0
                    status_code, chunks = download(url, 0)
                if status_code not in (200, 206):
                    raise RealDebridError(f"Download failed: HTTP {status_code}",
                                          status_code)
                mode = "ab" if (have and status_code == 206) else "wb"
                if mode == "wb":
                    have = 0
                written = have
                try:
                    with open(part, mode) as fh:
                        for chunk in chunks:
                            if cancelled():
                                fh.flush()
                                close = getattr(chunks, "close", None)
                                if close:
                                    close()
                                drop(tid)
                                raise RealDebridCancelled("Cancelled")
                            fh.write(chunk)
                            written += len(chunk)
                            progress("local", (done + written) / total if total else 1.0)
                finally:
                    close = getattr(chunks, "close", None)
                    if close:
                        close()
                have = written
            except (OSError, http.client.HTTPException) as exc:
                have = part.stat().st_size if part.exists() else 0
                if attempts >= DOWNLOAD_ATTEMPTS:
                    raise RealDebridError(
                        f"Download of {rel} kept failing: {exc}") from None
                logger.info("RD download of %s dropped at %d/%d bytes (%s); "
                            "resuming.", rel, have, size, exc)
                sleep(min(2 ** attempts, 10))
                continue
            if have >= size or size == 0:
                break
            if have <= before or attempts >= DOWNLOAD_ATTEMPTS:
                break
            logger.info("RD download of %s ended at %d/%d bytes; resuming.",
                        rel, have, size)
            sleep(1)
        if have != size:
            part.unlink(missing_ok=True)
            raise RealDebridError(
                f"Size mismatch for {rel}: got {have}, expected {size}")
        part.replace(target)
        done += size
        rel_paths.append(rel)

    progress("local", 1.0)
    if delete_after:
        drop(tid)
    return FetchResult(rel_paths=rel_paths, torrent_id=tid, cached=cached,
                       total_bytes=total)
