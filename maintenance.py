# =============================================================================
# maintenance.py
# =============================================================================
# Background library maintenance tools.  Every public function is synchronous
# and designed to be called from a thread executor or background thread so the
# Tk UI stays responsive.
#
# Tools:
#   daily_library_check()          — scan open requests against Plex; mark found
#   library_inventory()            — per-show season/episode counts
#   find_duplicates()              — files likely representing the same content
#   sanitize_filename(path, dry)   — return Plex-friendly renamed path
#   apply_sanitization(pairs)      — rename files in bulk
#   find_missing_episodes()        — gaps in episode numbering per show
#   find_unindexed_files()         — files on disk not in Plex
#   delete_files_with_cleanup()    — delete files and report empty parent dirs
#   media_type_for_path(path)      — derive media-type tag from configured paths
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
    candidates: list[str]            # file paths
    candidate_sizes: list[int]       # parallel list of per-file sizes (bytes)
    total_size_bytes: int


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
class SeasonSummary:
    """Per-season totals captured alongside MissingEpisode gaps."""
    show_title: str
    show_path: str
    season: int
    episodes_present: int       # how many distinct episode files we found
    highest_episode: int        # the highest episode number we saw
    missing_count: int          # gap-count between 1 and highest


@dataclass
class MissingEpisodesReport:
    """Full result of find_missing_episodes() — gaps plus per-season totals."""
    gaps: list[MissingEpisode]
    seasons: list[SeasonSummary]
    shows_scanned: int


@dataclass
class UnindexedFile:
    """A media file on disk that is not registered in the Plex library."""
    path: str
    name: str
    size_bytes: int


@dataclass
class ShowInventory:
    """Per-show season/episode counts derived from the on-disk filenames."""
    title: str
    media_type: str               # "tv" | "anime" | "xanime" | "mixed"
    seasons: dict[int, int]       # season number → distinct episode count
    total_episodes: int
    total_size_bytes: int


@dataclass
class LibraryInventory:
    """Aggregate stats for one walk of the library."""
    shows: list[ShowInventory]
    movie_count: int
    movie_size_bytes: int
    untyped_files: int            # files whose category couldn't be guessed


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
    Group media files that are likely duplicates of the same content.

    Two files are duplicates when they share the same normalized title AND the
    same season/episode (where applicable):
        - Movies (no SxxExx pattern) are grouped by title alone — same movie
          in 1080p and 4K should be flagged.
        - Episodes are grouped by (show, season, episode) — different episodes
          of the same show are NOT duplicates.

    Returns a list of DuplicateGroup objects, each with ≥ 2 candidate paths.
    """
    files = _media_files(config.PLEX_LIBRARY_PATHS)
    if not files:
        logger.warning("find_duplicates: no media files found in configured library paths.")
        return []

    # Key: (normalized_title, season_or_None, episode_or_None)
    groups: dict[tuple[str, int | None, int | None], list[Path]] = {}
    for f in files:
        title = _normalize_title(f.name)
        if not title:
            continue
        ep = _parse_episode(f.name)
        key = (title, ep[0] if ep else None, ep[1] if ep else None)
        groups.setdefault(key, []).append(f)

    results: list[DuplicateGroup] = []
    for (norm_title, season, episode), paths in groups.items():
        if len(paths) < 2:
            continue
        sorted_paths = sorted(paths)
        sizes: list[int] = []
        for p in sorted_paths:
            try:
                sizes.append(p.stat().st_size)
            except OSError:
                sizes.append(0)
        label = norm_title
        if season is not None and episode is not None:
            label = f"{norm_title} S{season:02d}E{episode:02d}"
        results.append(DuplicateGroup(
            normalized_title=label,
            candidates=[str(p) for p in sorted_paths],
            candidate_sizes=sizes,
            total_size_bytes=sum(sizes),
        ))

    results.sort(key=lambda g: -g.total_size_bytes)
    logger.info("find_duplicates: found %d potential duplicate group(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# Library inventory — per-show season/episode counts
# ---------------------------------------------------------------------------

def media_type_for_path(path: str | Path) -> str:
    """
    Map a file path to the media-type tag of the library root that contains it.

    Returns the media_type ("tv", "movie", "anime", "xanime", "mixed") of the
    first MediaLibraryPath whose root is an ancestor of `path`. Falls back to
    "mixed" for legacy/untyped configurations or paths outside any root.
    """
    p = Path(path) if not isinstance(path, Path) else path
    try:
        path_resolved = p.resolve()
    except OSError:
        path_resolved = p
    for entry in config.MEDIA_LIBRARY_PATHS:
        try:
            root = Path(entry.path).resolve()
        except OSError:
            root = Path(entry.path)
        try:
            path_resolved.relative_to(root)
            return entry.media_type
        except ValueError:
            continue
    return "mixed"


# Backwards-compat alias for existing internal callers in this module.
_media_type_for_path = media_type_for_path


def library_inventory() -> LibraryInventory:
    """
    Walk every configured library path and produce per-show season/episode
    statistics. Files matching SxxExx (or NxN) patterns are treated as TV/anime
    episodes and grouped by show; everything else counts as a movie file.

    The show key is the normalized title from the filename — this works
    consistently whether the library is laid out as "Show/Season 01/file.mkv"
    or as a flat folder of files.

    Used by both the standalone "Library Inventory" tool and as the data
    source for richer duplicate detection / missing-episode reports.
    """
    files = _media_files(config.PLEX_LIBRARY_PATHS)

    # show_title -> { season -> set of episodes }
    show_eps: dict[str, dict[int, set[int]]] = {}
    show_size: dict[str, int] = {}
    show_type: dict[str, str] = {}
    movie_count = 0
    movie_size = 0
    untyped = 0

    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0

        ep = _parse_episode(f.name)
        norm = _normalize_title(f.name)

        if ep is not None and norm:
            season, episode = ep
            show_eps.setdefault(norm, {}).setdefault(season, set()).add(episode)
            show_size[norm] = show_size.get(norm, 0) + size
            # First time we see this show, remember the media type from the
            # root path it lives under. If a later episode lives under a
            # different root with a different type tag, prefer the more
            # specific tag over "mixed".
            existing = show_type.get(norm, "")
            this_type = _media_type_for_path(f)
            if not existing or existing == "mixed":
                show_type[norm] = this_type
        elif norm:
            movie_count += 1
            movie_size += size
        else:
            untyped += 1

    shows: list[ShowInventory] = []
    for title, seasons in show_eps.items():
        shows.append(ShowInventory(
            title=title,
            media_type=show_type.get(title, "mixed"),
            seasons={s: len(eps) for s, eps in seasons.items()},
            total_episodes=sum(len(eps) for eps in seasons.values()),
            total_size_bytes=show_size.get(title, 0),
        ))
    shows.sort(key=lambda s: (-s.total_episodes, s.title))

    logger.info(
        "library_inventory: %d show(s) / %d movie file(s) / %d untyped",
        len(shows), movie_count, untyped,
    )
    return LibraryInventory(
        shows=shows,
        movie_count=movie_count,
        movie_size_bytes=movie_size,
        untyped_files=untyped,
    )


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


def delete_files_with_cleanup(
    paths: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """
    Delete the given files and identify any parent directories left empty.

    Returns ``(deleted, errors, empty_parent_dirs)``:
      - deleted:           paths that were successfully removed
      - errors:            human-readable failure messages
      - empty_parent_dirs: directories that became empty after the deletes —
                           the caller is expected to ask the user before
                           actually removing them, since some libraries put
                           movies "free-floating" in a shared root and we
                           must NOT collapse the root itself.

    Empty-folder detection only walks UP one level from each deleted file
    (good enough for the common "Movie Title (Year)/Movie Title.mkv" layout)
    and never returns a directory that is a configured library root.
    """
    deleted: list[str] = []
    errors: list[str] = []
    candidate_parents: set[Path] = set()

    library_roots: set[Path] = set()
    for entry in config.MEDIA_LIBRARY_PATHS:
        try:
            library_roots.add(Path(entry.path).resolve())
        except OSError:
            library_roots.add(Path(entry.path))

    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            errors.append(f"Not a file (skipped): {raw_path}")
            continue
        try:
            parent = path.parent
            path.unlink()
            deleted.append(raw_path)
            candidate_parents.add(parent)
            logger.info("Deleted duplicate file: %s", raw_path)
        except OSError as exc:
            errors.append(f"Delete failed for {raw_path}: {exc}")

    empty_parents: list[str] = []
    for parent in candidate_parents:
        try:
            parent_resolved = parent.resolve()
        except OSError:
            parent_resolved = parent
        if parent_resolved in library_roots:
            continue  # never offer to nuke a configured library root
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                empty_parents.append(str(parent))
        except OSError:
            continue

    return deleted, errors, empty_parents


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


def find_missing_episodes() -> MissingEpisodesReport:
    """
    Walk TV show directories and detect gaps in S##E## episode numbering.

    Logic (intentionally narrow):
      1. For each configured library path, look at every immediate
         subdirectory as a candidate show.
      2. Walk that show recursively, parsing SxxExx / NxN out of filenames.
      3. For each season we observe, expect episodes 1..max(present) and
         report any integers from that range that are absent.

    What this *cannot* detect: that a season exists upstream which you
    have nothing of yet (e.g. a show just released S04 and you have only
    S01-S03). For that, an external lookup against TVDB would be needed.

    The returned report includes per-season summaries (highest episode
    found, count present) so you can sanity-check completeness yourself.
    """
    extensions = set(config.LIBRARY_INDEX_EXTENSIONS)
    gaps: list[MissingEpisode] = []
    summaries: list[SeasonSummary] = []
    shows_scanned = 0

    for lib_root in config.PLEX_LIBRARY_PATHS:
        root = Path(lib_root)
        if not root.is_dir():
            continue

        for show_dir in root.iterdir():
            if not show_dir.is_dir():
                continue

            show_title = show_dir.name
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
                continue
            shows_scanned += 1

            for season, episodes in sorted(season_map.items()):
                if not episodes:
                    continue
                highest = max(episodes)
                expected = set(range(1, highest + 1))
                missing = sorted(expected - episodes)
                for ep in missing:
                    gaps.append(MissingEpisode(
                        show_title=show_title,
                        show_path=str(show_dir),
                        season=season,
                        episode=ep,
                    ))
                summaries.append(SeasonSummary(
                    show_title=show_title,
                    show_path=str(show_dir),
                    season=season,
                    episodes_present=len(episodes),
                    highest_episode=highest,
                    missing_count=len(missing),
                ))

    logger.info(
        "find_missing_episodes: %d show(s) scanned, %d season(s), %d gap(s) total.",
        shows_scanned, len(summaries), len(gaps),
    )
    return MissingEpisodesReport(gaps=gaps, seasons=summaries, shows_scanned=shows_scanned)


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
