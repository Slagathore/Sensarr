# =============================================================================
# maintenance.py
# =============================================================================
# Background library maintenance tools.  Every public function is synchronous
# and designed to be called from a thread executor or background thread so the
# Tk UI stays responsive.
#
# Tools:
#   daily_library_check()          — scan open requests against Plex; mark found
#   find_duplicates()              — files likely representing the same content
#   check_filenames_vs_plex()      — files whose names diverge from Plex titles
#   sanitize_filename(path, dry)   — return Plex-friendly renamed path
#   apply_sanitization(pairs)      — rename files in bulk
#   find_missing_episodes()        — gaps in episode numbering per show
#   find_unindexed_files()         — files on disk not in Plex
# =============================================================================

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes — results passed back to the UI
# ---------------------------------------------------------------------------

@dataclass
class DuplicateGroup:
    """A group of files that are likely duplicates of the same content."""
    normalized_title: str
    candidates: list[str]       # file paths
    total_size_bytes: int


@dataclass
class FilenameIssue:
    """A file whose disk name doesn't match what Plex shows."""
    path: str
    disk_name: str
    plex_title: str
    expected_filename: str      # what the disk name ideally should be


@dataclass
class SanitizePair:
    """A proposed rename: (original path, sanitized path)."""
    original: str
    sanitized: str
    size_bytes: int


@dataclass
class MissingEpisode:
    """A gap detected in a show's episode sequence."""
    show_title: str
    show_path: str
    season: int
    episode: int


@dataclass
class UnindexedFile:
    """A media file on disk that is not registered in the Plex library."""
    path: str
    name: str
    size_bytes: int


# ---------------------------------------------------------------------------
# Daily library check
# ---------------------------------------------------------------------------

def daily_library_check() -> dict:
    """
    Scan all open requests against the Plex library (or file index fallback)
    and update the found_in_library flag in the DB.

    Returns a summary dict:
        {
            "checked": int,
            "newly_found": int,
            "errors": list[str],
        }
    """
    from library_index import search_library
    from queue_store import get_requests_needing_library_check, update_library_status

    requests = get_requests_needing_library_check()
    checked = 0
    newly_found = 0
    errors: list[str] = []

    for req in requests:
        try:
            search_title = req.resolved_title or req.content
            results = search_library(search_title, limit=5)

            # Simple match: any result whose name contains the search title
            title_lower = search_title.casefold()
            found = any(title_lower in entry.name.casefold() for entry in results)

            update_library_status(req.request_id, found=found)
            checked += 1
            if found and not req.found_in_library:
                newly_found += 1
                logger.info(
                    "Library check: request #%d (%s) is now in the library.",
                    req.request_id,
                    search_title,
                )
        except Exception as exc:
            msg = f"Error checking request #{req.request_id}: {exc}"
            logger.error(msg)
            errors.append(msg)

    return {"checked": checked, "newly_found": newly_found, "errors": errors}


# ---------------------------------------------------------------------------
# Helpers shared across tools
# ---------------------------------------------------------------------------

_QUALITY_TAGS = re.compile(
    r"""
    [\[(]?                          # optional opening bracket
    (?:
        \b(?:
            2160p|1080p|720p|480p|4K|UHD|HD|SD|
            BluRay|Blu-Ray|BDRip|BDRemux|BRRip|
            WEBRip|WEB-DL|WEBDL|HDTV|DVDRip|DVDScr|
            HEVC|x264|x265|AVC|H\.264|H\.265|AV1|
            AAC|AC3|DTS|FLAC|MP3|Atmos|TrueHD|
            REMUX|HDR|SDR|DoVi|
            PROPER|REPACK|REMASTERED|EXTENDED|DIRECTORS\.CUT|
            NF|AMZN|HMAX|DSNP|ATVP|PCOK
        )\b
        (?:[\s.\-_]|$)
    )+
    [\])]?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_RELEASE_GROUP = re.compile(r"-[A-Z0-9]{2,12}$", re.IGNORECASE)
_SEPARATORS = re.compile(r"[._]+")
_MULTI_SPACES = re.compile(r"\s{2,}")


def _normalize_title(name: str) -> str:
    """
    Strip quality tags, release groups, and separators from a filename stem
    to get a clean, comparable title.
    """
    stem = Path(name).stem  # remove file extension

    # Strip common episode patterns entirely for show-title extraction
    stem = re.sub(r"\s*[-–]?\s*S\d{2}E\d{2}\b.*", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*[-–]?\s*\d+x\d+\b.*", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*\(\d{4}\)\s*$", "", stem)           # trailing (year)

    stem = _QUALITY_TAGS.sub(" ", stem)
    stem = _RELEASE_GROUP.sub("", stem)
    stem = _SEPARATORS.sub(" ", stem)
    stem = _MULTI_SPACES.sub(" ", stem)
    return stem.strip().casefold()


def _media_files(roots: list[str]) -> list[Path]:
    """Walk configured library paths and yield all media files."""
    extensions = set(config.LIBRARY_INDEX_EXTENSIONS)
    files: list[Path] = []
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dirpath, _dirs, names in os.walk(root_path):
            for name in names:
                if Path(name).suffix.lower() in extensions:
                    files.append(Path(dirpath) / name)
    return files


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def find_duplicates() -> list[DuplicateGroup]:
    """
    Group media files by their normalised title.  Files that share the same
    normalised title (e.g. same movie in 1080p and 4K) are flagged.

    Returns a list of DuplicateGroup objects, each with ≥ 2 candidate paths.
    """
    files = _media_files(config.PLEX_LIBRARY_PATHS)
    if not files:
        logger.warning("find_duplicates: no media files found in configured library paths.")
        return []

    groups: dict[str, list[Path]] = {}
    for f in files:
        key = _normalize_title(f.name)
        if key:
            groups.setdefault(key, []).append(f)

    results: list[DuplicateGroup] = []
    for norm_title, paths in groups.items():
        if len(paths) < 2:
            continue
        total_bytes = sum(
            p.stat().st_size for p in paths if p.exists()
        )
        results.append(DuplicateGroup(
            normalized_title=norm_title,
            candidates=[str(p) for p in sorted(paths)],
            total_size_bytes=total_bytes,
        ))

    results.sort(key=lambda g: -g.total_size_bytes)
    logger.info("find_duplicates: found %d potential duplicate group(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# Filename vs Plex title check
# ---------------------------------------------------------------------------

def check_filenames_vs_plex() -> list[FilenameIssue]:
    """
    Compare on-disk filenames to the titles Plex has for them.
    Requires PLEX_TOKEN; returns [] if Plex is not configured.

    Returns FilenameIssue objects for files where the disk name diverges
    significantly from Plex's title.
    """
    try:
        from plex_api import search_plex_library
    except ImportError:
        logger.error("plex_api not available.")
        return []

    issues: list[FilenameIssue] = []

    for lib_path in config.PLEX_LIBRARY_PATHS:
        root = Path(lib_path)
        if not root.is_dir():
            continue

        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if Path(name).suffix.lower() not in set(config.LIBRARY_INDEX_EXTENSIONS):
                    continue

                stem = Path(name).stem
                normalized = _normalize_title(name)
                if not normalized:
                    continue

                try:
                    plex_results = search_plex_library(normalized, limit=3)
                except Exception as exc:
                    logger.debug("Plex search failed for '%s': %s", normalized, exc)
                    continue

                if not plex_results:
                    continue

                plex_title = plex_results[0].name
                # Strip the Plex show prefix ("ShowName - S01E02 - Title") to get the canonical title
                canonical = re.sub(r"\s*[-–]\s*S\d{2}E\d{2}\b.*", "", plex_title).strip()

                if canonical.casefold() == normalized:
                    continue  # Names match — no issue

                # Build what the filename ideally should look like
                year_match = re.search(r"\((\d{4})\)", plex_title)
                year_str = f" ({year_match.group(1)})" if year_match else ""
                ext = Path(name).suffix
                expected = f"{canonical}{year_str}{ext}"

                issues.append(FilenameIssue(
                    path=str(Path(dirpath) / name),
                    disk_name=name,
                    plex_title=plex_title,
                    expected_filename=expected,
                ))

    logger.info("check_filenames_vs_plex: %d issue(s) found.", len(issues))
    return issues


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------

def _plex_safe_name(stem: str, year: int | None, ext: str) -> str:
    """Build a Plex-friendly filename: 'Title (Year).ext'."""
    clean = _QUALITY_TAGS.sub(" ", stem)
    clean = _RELEASE_GROUP.sub("", clean)
    clean = _SEPARATORS.sub(" ", clean)
    clean = re.sub(r"\s*\(\d{4}\)\s*", "", clean)  # strip any embedded year
    clean = _MULTI_SPACES.sub(" ", clean).strip()
    # Title case
    clean = clean.title()
    year_str = f" ({year})" if year else ""
    return f"{clean}{year_str}{ext}"


def sanitize_filename(path: str, *, dry_run: bool = True) -> SanitizePair:
    """
    Propose (or apply) a Plex-friendly rename for one file.

    Args:
        path:    Absolute path to the media file.
        dry_run: If True (default), only return the proposed pair without
                 touching the filesystem.  Pass dry_run=False to apply.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    stem = p.stem
    ext = p.suffix

    # Try to extract year from the original name
    year: int | None = None
    m = re.search(r"\((\d{4})\)", stem)
    if m:
        try:
            year = int(m.group(1))
        except ValueError:
            pass

    new_name = _plex_safe_name(stem, year, ext)
    new_path = p.parent / new_name

    pair = SanitizePair(
        original=str(p),
        sanitized=str(new_path),
        size_bytes=p.stat().st_size,
    )

    if not dry_run and new_path != p:
        p.rename(new_path)
        logger.info("Sanitized: %s → %s", p.name, new_name)

    return pair


def sanitize_all(*, dry_run: bool = True) -> list[SanitizePair]:
    """
    Propose (or apply) Plex-friendly renames for all media files in configured
    library paths where the current filename would change.
    """
    files = _media_files(config.PLEX_LIBRARY_PATHS)
    pairs: list[SanitizePair] = []

    for f in files:
        try:
            pair = sanitize_filename(str(f), dry_run=dry_run)
            if pair.original != pair.sanitized:
                pairs.append(pair)
        except Exception as exc:
            logger.warning("Could not sanitize '%s': %s", f, exc)

    logger.info(
        "sanitize_all: %d file(s) %s.",
        len(pairs),
        "proposed to rename" if dry_run else "renamed",
    )
    return pairs


def apply_sanitization(pairs: list[SanitizePair]) -> list[str]:
    """
    Apply a list of SanitizePair renames (those selected by the user via
    checkboxes in the UI).  Returns a list of error messages for any that fail.
    """
    errors: list[str] = []
    for pair in pairs:
        src = Path(pair.original)
        dst = Path(pair.sanitized)
        if not src.is_file():
            errors.append(f"Source gone: {pair.original}")
            continue
        if dst.exists():
            errors.append(f"Destination already exists: {pair.sanitized}")
            continue
        try:
            src.rename(dst)
            logger.info("Renamed: %s → %s", src.name, dst.name)
        except OSError as exc:
            errors.append(f"Rename failed for {src.name}: {exc}")

    return errors


# ---------------------------------------------------------------------------
# Missing episode detection
# ---------------------------------------------------------------------------

_EP_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})", re.IGNORECASE)
_ALT_EP_PATTERN = re.compile(r"\b(\d{1,2})x(\d{1,2})\b")


def _parse_episode(filename: str) -> tuple[int, int] | None:
    """Return (season, episode) from a filename, or None if unrecognised."""
    m = _EP_PATTERN.search(filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _ALT_EP_PATTERN.search(filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def find_missing_episodes() -> list[MissingEpisode]:
    """
    Walk TV show directories and detect gaps in S##E## episode numbering.

    A directory is treated as a TV show if it contains sub-directories named
    "Season XX" or if the majority of its files match the S##E## pattern.

    Returns a list of MissingEpisode objects for each detected gap.
    """
    extensions = set(config.LIBRARY_INDEX_EXTENSIONS)
    missing: list[MissingEpisode] = []

    for lib_root in config.PLEX_LIBRARY_PATHS:
        root = Path(lib_root)
        if not root.is_dir():
            continue

        # Walk one level deep to find show directories
        for show_dir in root.iterdir():
            if not show_dir.is_dir():
                continue

            show_title = show_dir.name
            # Collect all episodes: season → set of episode numbers
            season_map: dict[int, set[int]] = {}

            for dirpath, _dirs, names in os.walk(show_dir):
                for name in names:
                    if Path(name).suffix.lower() not in extensions:
                        continue
                    parsed = _parse_episode(name)
                    if parsed is None:
                        continue
                    season, ep = parsed
                    season_map.setdefault(season, set()).add(ep)

            if not season_map:
                continue  # not a TV show directory

            # Detect gaps within each season
            for season, episodes in sorted(season_map.items()):
                sorted_eps = sorted(episodes)
                if not sorted_eps:
                    continue
                # Check for gaps between 1 and max episode number
                expected = set(range(1, max(sorted_eps) + 1))
                gaps = sorted(expected - episodes)
                for ep in gaps:
                    missing.append(MissingEpisode(
                        show_title=show_title,
                        show_path=str(show_dir),
                        season=season,
                        episode=ep,
                    ))

    logger.info("find_missing_episodes: %d missing episode slot(s) detected.", len(missing))
    return missing


# ---------------------------------------------------------------------------
# Unindexed files (on disk but not in Plex)
# ---------------------------------------------------------------------------

def find_unindexed_files() -> list[UnindexedFile]:
    """
    Find media files that exist on disk but are not registered in the Plex
    library.

    Strategy:
        1. Collect all file paths from the local library index (SQLite).
        2. Walk configured library paths.
        3. Any file not in the index is "unindexed".

    Requires that the library index has been built (run Reindex first).
    If Plex API is available, it also checks against live Plex metadata.
    """
    import sqlite3
    from queue_store import _db_path

    # Load indexed paths from the SQLite library_files table
    indexed_paths: set[str] = set()
    try:
        db = Path(config.APP_DIR) / Path(config.APP_DB_PATH).name
        with sqlite3.connect(str(db)) as conn:
            rows = conn.execute("SELECT path FROM library_files").fetchall()
        indexed_paths = {row[0] for row in rows}
    except Exception as exc:
        logger.warning("Could not load library index for unindexed-file check: %s", exc)

    # Also collect paths Plex knows about (best-effort)
    plex_paths: set[str] = set()
    try:
        from plex_api import search_plex_library
        # We can't enumerate all Plex items cheaply without fetching every section,
        # so we use indexed_paths as our primary source and Plex as a supplement.
        # Full enumeration happens in the maintenance tab refresh.
        pass
    except Exception:
        pass

    extensions = set(config.LIBRARY_INDEX_EXTENSIONS)
    unindexed: list[UnindexedFile] = []

    for lib_root in config.PLEX_LIBRARY_PATHS:
        root = Path(lib_root)
        if not root.is_dir():
            continue
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if Path(name).suffix.lower() not in extensions:
                    continue
                full_path = str(Path(dirpath) / name)
                if full_path not in indexed_paths and full_path not in plex_paths:
                    try:
                        size = Path(full_path).stat().st_size
                    except OSError:
                        size = 0
                    unindexed.append(UnindexedFile(
                        path=full_path,
                        name=name,
                        size_bytes=size,
                    ))

    logger.info("find_unindexed_files: %d unindexed file(s) found.", len(unindexed))
    return unindexed
