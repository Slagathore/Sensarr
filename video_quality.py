# =============================================================================
# video_quality.py
# =============================================================================
# Movie quality tooling for the Library tab:
#
#   is_cam_release(name)        — release-name check for cam/telesync/etc.
#   duration_minutes(path)      — runtime via ffprobe (cached in SQLite);
#                                 falls back to a 120-minute assumption.
#   mb_per_minute(path, size)   — the actual quality metric the app uses.
#   find_low_quality_movies()   — cam-named or under-bitrate movies, with a
#                                 redundancy check against better versions
#                                 of the same title already in the library.
#
# Sorting contract (per Cole): cam-keyword hits first (lowest MB/min first),
# then keyword-free movies under the threshold, again lowest-first. Anything
# the regex hits is listed regardless of its rate.
# =============================================================================

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import config
import db
from maintenance import _normalize_title, media_type_for_path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Cam / telesync / screener markers. Word-bounded so "cats" doesn't hit "ts".
CAM_RE = re.compile(
    r"(?:^|[\s._\-\[(])(?:"
    r"cam(?:rip)?|hd-?cam|hq-?cam|"
    r"ts|hd-?ts|p-?dvd|"
    r"telesync|tele-?sync|"
    r"telecine|hd-?tc|tc-?rip|"
    r"dvd-?scr(?:eener)?|screener|scr|"
    r"workprint|wp"
    r")(?:[\s._\-\])]|$)",
    re.IGNORECASE,
)


def is_cam_release(name: str) -> bool:
    """True when a release/file name carries a cam/telesync/screener marker."""
    return CAM_RE.search(Path(name).stem) is not None


def cam_markers(name: str) -> list[str]:
    """The matched cam markers in a name, cleaned up ("CAM", "TS", …)."""
    return [m.strip(" ._-[]()").upper() for m in CAM_RE.findall(Path(name).stem)]


# ---------------------------------------------------------------------------
# Duration probing (ffprobe, cached)
# ---------------------------------------------------------------------------

_ASSUMED_MOVIE_MINUTES = 120.0


def _init_probe_cache() -> None:
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS media_probe (
                path         TEXT PRIMARY KEY,
                size_bytes   INTEGER NOT NULL,
                duration_min REAL,
                probed_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


_ffprobe_path: str | None = None
_ffprobe_resolved = False


def _find_ffprobe() -> str | None:
    """ffprobe from PATH, or the usual install spots (choco/winget/scoop/
    C:\\ffmpeg) — plenty of users have ffmpeg installed but not on PATH."""
    global _ffprobe_path, _ffprobe_resolved
    if _ffprobe_resolved:
        return _ffprobe_path
    _ffprobe_resolved = True

    found = shutil.which("ffprobe")
    if found is None:
        import glob
        import os
        candidates = [
            r"C:\ffmpeg\bin\ffprobe.exe",
            r"C:\ProgramData\chocolatey\bin\ffprobe.exe",
            os.path.expandvars(r"%USERPROFILE%\scoop\shims\ffprobe.exe"),
        ]
        candidates += glob.glob(os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*FFmpeg*\**\bin\ffprobe.exe"),
            recursive=True)
        found = next((c for c in candidates if Path(c).is_file()), None)
    if found:
        logger.info("ffprobe found at %s — exact runtimes enabled.", found)
    else:
        logger.info("ffprobe not found — movie runtimes assume 2 h.")
    _ffprobe_path = found
    return found


def _ffprobe_minutes(path: str) -> float | None:
    ffprobe = _find_ffprobe()
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
        seconds = float(result.stdout.strip())
        return seconds / 60.0 if seconds > 0 else None
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def duration_minutes(path: str, size_bytes: int) -> tuple[float, bool]:
    """(minutes, exact) — ffprobe result cached per (path, size); when
    ffprobe isn't installed, assumes a 2-hour movie (exact=False)."""
    _init_probe_cache()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT duration_min FROM media_probe WHERE path = ? AND size_bytes = ?",
            (path, size_bytes),
        ).fetchone()
    if row is not None and row[0]:
        return float(row[0]), True

    minutes = _ffprobe_minutes(path)
    if minutes is not None:
        with db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO media_probe (path, size_bytes, duration_min) "
                "VALUES (?, ?, ?)", (path, size_bytes, minutes))
            conn.commit()
        return minutes, True
    return _ASSUMED_MOVIE_MINUTES, False


def mb_per_minute(path: str, size_bytes: int) -> tuple[float, bool]:
    """(MB/min, exact_duration) for one file."""
    minutes, exact = duration_minutes(path, size_bytes)
    if minutes <= 0:
        minutes = _ASSUMED_MOVIE_MINUTES
    return (size_bytes / (1024 * 1024)) / minutes, exact


# ---------------------------------------------------------------------------
# Low-quality scan
# ---------------------------------------------------------------------------

def _movie_title_key(name: str) -> str:
    """Grouping key for 'same movie, different release': strips cam markers
    and the year BEFORE the generic normalizer, so 'Movie (2020) HDCAM' and
    'Movie (2020) 1080p' land in the same bucket."""
    stem = Path(name).stem
    stem = CAM_RE.sub(" ", stem)
    stem = re.sub(r"[\[(]?\b(?:19|20)\d{2}\b[\])]?", " ", stem)
    return _normalize_title(stem)


@dataclass
class LowQualityMovie:
    path: str
    name: str
    size_bytes: int
    rate_mb_min: float
    rate_exact: bool               # ffprobe-backed vs assumed 2 h
    cam_hit: str                   # "" or the matched marker(s), e.g. "CAM, TS"
    normalized_title: str
    redundant_with: str | None     # better version's path, if one exists


def find_low_quality_movies(
    movie_files: list[tuple[str, str, int]],  # (path, name, size_bytes)
    *, threshold_mb_min: float | None = None,
    progress=None,
) -> list[LowQualityMovie]:
    """Scan movie files for cams and under-bitrate encodes.

    Redundancy check: every flagged file is compared against OTHER files with
    the same normalized title — if a non-flagged (non-cam, at/above
    threshold) version exists, redundant_with points at it, meaning the
    flagged copy can simply be deleted instead of replaced.
    """
    threshold = (config.LOW_QUALITY_MB_PER_MIN
                 if threshold_mb_min is None else threshold_mb_min)

    # Load the entire probe cache in ONE query — the first scan pays the
    # ffprobe cost per file (persisted to SQLite), reruns and restarts only
    # probe files that are new or changed size.
    _init_probe_cache()
    with db.connect() as conn:
        cached = {(row[0], row[1]): row[2] for row in conn.execute(
            "SELECT path, size_bytes, duration_min FROM media_probe")}

    scanned: list[tuple[str, str, int, float, bool, bool]] = []
    new_probes: list[tuple[str, int, float]] = []
    for i, (path, name, size) in enumerate(movie_files):
        if progress is not None and i % 25 == 0:
            progress(i, len(movie_files))
        minutes = cached.get((path, size))
        exact = minutes is not None and minutes > 0
        if not exact:
            probed = _ffprobe_minutes(path)
            if probed is not None:
                minutes, exact = probed, True
                new_probes.append((path, size, probed))
            else:
                minutes = _ASSUMED_MOVIE_MINUTES
        rate = (size / (1024 * 1024)) / (minutes or _ASSUMED_MOVIE_MINUTES)
        scanned.append((path, name, size, rate, exact, is_cam_release(name)))

    if new_probes:
        with db.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO media_probe (path, size_bytes, duration_min) "
                "VALUES (?, ?, ?)", new_probes)
            conn.commit()

    by_title: dict[str, list[tuple[str, str, int, float, bool, bool]]] = {}
    for entry in scanned:
        by_title.setdefault(_movie_title_key(entry[1]), []).append(entry)

    results: list[LowQualityMovie] = []
    for title, entries in by_title.items():
        good_versions = [e for e in entries if not e[5] and e[3] >= threshold]
        for path, name, size, rate, exact, cam in entries:
            if not cam and rate >= threshold:
                continue  # fine as-is
            better = next((g[0] for g in good_versions if g[0] != path), None)
            markers = ", ".join(dict.fromkeys(cam_markers(name))) if cam else ""
            results.append(LowQualityMovie(
                path=path, name=name, size_bytes=size,
                rate_mb_min=rate, rate_exact=exact, cam_hit=markers,
                normalized_title=title, redundant_with=better,
            ))

    # Cam hits first (lowest rate first), then keyword-free low-bitrate.
    results.sort(key=lambda m: (0 if m.cam_hit else 1, m.rate_mb_min))
    logger.info("find_low_quality_movies: %d flagged of %d scanned.",
                len(results), len(movie_files))
    return results
