# =============================================================================
# torrent_routing.py
# =============================================================================
# Decides where a downloaded file belongs and what it should be called.
#
# Design rule (per Cole): a wrong move or wrong rename is far worse than no
# move — a confused route leaves the file in the staging folder and says so,
# instead of guessing. Rename only happens when BOTH the episode parse and the
# show-folder match are confident.
#
# Season folders: the planner copies the naming style already used inside the
# matched show folder ("Season 1" vs "Season 01" vs "S01"), so it never
# introduces a second convention into an existing show.
# =============================================================================

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import config

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = set(config.LIBRARY_INDEX_EXTENSIONS)
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize_for_filesystem(name: str) -> str:
    """Strip characters Windows refuses in file/folder names."""
    return _INVALID_FS_CHARS.sub("", name).strip()

# Confidence needed before we'll route into an existing show folder.
_SHOW_MATCH_THRESHOLD = 0.85


def pick_root_by_free_space(candidates: list[str]) -> str | None:
    """Where NEW content should land, among the given candidate folders.

    DOWNLOAD_ROOT_OVERRIDE (Settings) hard-pins the choice: an exact folder
    wins outright; a drive-root-style override ("I:\\") narrows candidates
    to that drive. Otherwise the folder on the drive with the most free
    space wins — drive letters can shuffle, free space follows the disk.
    """
    dirs = [c for c in candidates if Path(c).is_dir()]
    override = config.DOWNLOAD_ROOT_OVERRIDE
    if override:
        override_path = Path(override)
        if override_path.is_dir() and not dirs:
            return str(override_path)
        for c in dirs:
            if Path(c) == override_path:
                return c
        on_override_drive = [
            c for c in dirs
            if Path(c).drive.upper() == override_path.drive.upper()
        ]
        if on_override_drive:
            dirs = on_override_drive
        elif override_path.is_dir():
            return str(override_path)
    if not dirs:
        return None

    def free_bytes(path: str) -> int:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return -1

    return max(dirs, key=free_bytes)

# --- Episode / title parsing -------------------------------------------------

# "(?:v\d+)?" — release version suffixes ("S01E05v2") sit flush against the
# episode number, which otherwise breaks the \b word boundary entirely.
_EPISODE_PATTERNS = [
    re.compile(r"\bS(?P<season>\d{1,2})[\s._-]*E(?P<episode>\d{1,3})(?:v\d+)?\b", re.IGNORECASE),
    re.compile(r"\b(?P<season>\d{1,2})x(?P<episode>\d{2,3})(?:v\d+)?\b", re.IGNORECASE),
]
_SEASON_ONLY_PATTERNS = [
    re.compile(r"\bS(?P<season>\d{1,2})\b(?![\s._-]*E\d)", re.IGNORECASE),
    re.compile(r"\bSeason[\s._-]*(?P<season>\d{1,2})\b", re.IGNORECASE),
]
# Junk that separates the title from release metadata in torrent names.
_TITLE_CUT_PATTERNS = [
    re.compile(r"\b(19|20)\d{2}\b.*"),          # year and beyond
    re.compile(r"\b(720p|1080p|2160p|480p|4k|hdr|x264|x265|h\.?264|h\.?265|hevc|web[\s.-]?dl|webrip|bluray|blu-ray|brrip|hdtv|dvdrip|aac|ac3|ddp?5\.1|10bit).*", re.IGNORECASE),
    re.compile(r"\[[^\]]*\]"),                   # [release group] blocks
    re.compile(r"\([^)]*\)"),                    # (parenthetical) blocks
]


@dataclass(frozen=True)
class ParsedName:
    show_title: str          # cleaned title portion ("" if none survived)
    season: int | None
    episode: int | None


@dataclass
class RoutePlan:
    confident: bool
    dest_dir: str                    # where the media should land
    reason: str                      # human explanation shown in the UI
    new_filename: str | None = None  # None = keep original name
    show_folder: str | None = None
    season_folder: str | None = None
    parsed: ParsedName | None = None
    library_root: str | None = None

    def describe(self) -> str:
        target = self.dest_dir
        if self.new_filename:
            target = str(Path(target) / self.new_filename)
        prefix = "→" if self.confident else "⚠ staging —"
        return f"{prefix} {target}  ({self.reason})"


def parse_torrent_name(name: str) -> ParsedName:
    """Extract show title + season/episode from a torrent or file name."""
    working = name.replace("_", " ")

    season = episode = None
    cut_at = len(working)
    for pat in _EPISODE_PATTERNS:
        m = pat.search(working)
        if m:
            season = int(m.group("season"))
            episode = int(m.group("episode"))
            cut_at = min(cut_at, m.start())
            break
    if season is None:
        for pat in _SEASON_ONLY_PATTERNS:
            m = pat.search(working)
            if m:
                season = int(m.group("season"))
                cut_at = min(cut_at, m.start())
                break

    title_part = working[:cut_at]
    title_part = re.sub(r"[.]", " ", title_part)
    for pat in _TITLE_CUT_PATTERNS:
        title_part = pat.sub("", title_part)
    title_part = re.sub(r"[\s\-–]+$", "", title_part).strip()
    title_part = re.sub(r"\s{2,}", " ", title_part)
    return ParsedName(show_title=title_part, season=season, episode=episode)


# --- Show folder matching ----------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _folder_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    try:
        from rapidfuzz import fuzz
        return fuzz.token_sort_ratio(na, nb) / 100.0
    except ImportError:
        return 1.0 if na == nb else (0.9 if na in nb or nb in na else 0.0)


def find_show_folder(show_title: str, media_type: str) -> tuple[Path | None, float, str | None]:
    """Find the best existing show folder across roots of this media type.

    Returns (folder, score, root). Only roots typed for the media type (plus
    'mixed') are scanned — one show's seasons may live on different drives, so
    ALL matching roots are considered and the best-scoring folder wins.
    """
    best: tuple[Path | None, float, str | None] = (None, 0.0, None)
    for root_str in config.media_paths_for_types(media_type):
        root = Path(root_str)
        if not root.is_dir():
            continue
        try:
            subdirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in subdirs:
            score = _folder_similarity(show_title, d.name)
            if score > best[1]:
                best = (d, score, root_str)
    return best


def _season_folder_name(show_dir: Path, season: int) -> str:
    """Match the season-folder naming style already used in this show dir."""
    pattern = re.compile(r"^(?P<prefix>Season[\s._-]*|S)(?P<num>\d{1,2})$", re.IGNORECASE)
    try:
        existing = [d.name for d in show_dir.iterdir() if d.is_dir()]
    except OSError:
        existing = []
    for name in existing:
        m = pattern.match(name.strip())
        if m:
            # Replicate the sibling's zero-padding style exactly.
            padded = (
                str(season).zfill(len(m.group("num")))
                if m.group("num").startswith("0")
                else str(season)
            )
            return f"{m.group('prefix')}{padded}"
    return f"Season {season:02d}"


# --- Public entry point --------------------------------------------------

def plan_route(torrent_name: str, media_type: str, *, request_title: str | None = None) -> RoutePlan:
    """Plan destination + optional rename for a torrent, before or after download.

    request_title, when provided (the resolved title from the request queue),
    is preferred over the parsed torrent name for show matching — it's the
    canonical name the user actually asked for.
    """
    staging = str(Path(config.TORRENT_DOWNLOAD_DIR))
    parsed = parse_torrent_name(torrent_name)

    if media_type in ("tv", "anime", "xanime"):
        search_title = (request_title or "").strip() or parsed.show_title
        if not search_title:
            return RoutePlan(
                confident=False, dest_dir=staging, parsed=parsed,
                reason="couldn't extract a show title from the torrent name",
            )

        folder, score, root = find_show_folder(search_title, media_type)
        if folder is None or score < _SHOW_MATCH_THRESHOLD:
            # Completely new show: when the title is CANONICAL (came from the
            # request queue / tracker, not parsed out of a torrent name),
            # create "<type root>/<Show Name>/Season NN" on the type's root
            # with the most free space — anime under the anime root, hentai
            # under xanime, etc. Torrent-parsed titles still go to staging;
            # guessing a folder name from release junk is how messes happen.
            if request_title and request_title.strip():
                type_roots = [p for p in config.media_paths_for_types(media_type)
                              if Path(p).is_dir()]
                new_root = pick_root_by_free_space(type_roots)
                if new_root is not None:
                    show_name = sanitize_for_filesystem(request_title.strip())
                    show_dir = Path(new_root) / show_name
                    season = parsed.season if parsed.season is not None else (
                        1 if parsed.episode is not None else None)
                    dest = show_dir / f"Season {season:02d}" if season is not None else show_dir
                    new_filename = (
                        f"{show_name} - S{season:02d}E{parsed.episode:02d}"
                        if season is not None and parsed.episode is not None else None
                    )
                    return RoutePlan(
                        confident=True, dest_dir=str(dest), parsed=parsed,
                        show_folder=str(show_dir),
                        season_folder=dest.name if season is not None else None,
                        new_filename=new_filename, library_root=new_root,
                        reason=f"new show — creating '{show_name}' under {Path(new_root).name}",
                    )
            hint = f"best guess '{folder.name}' scored {score:.0%}" if folder is not None else "no show folders found"
            return RoutePlan(
                confident=False, dest_dir=staging, parsed=parsed,
                reason=f"no confident show-folder match for '{search_title}' ({hint})",
            )

        if parsed.season is None:
            return RoutePlan(
                confident=False, dest_dir=staging, parsed=parsed,
                show_folder=str(folder), library_root=root,
                reason=f"matched show '{folder.name}' but couldn't parse a season number",
            )

        season_name = _season_folder_name(folder, parsed.season)
        dest = folder / season_name
        new_filename = None
        if parsed.episode is not None:
            # Canonical rename uses the *matched folder's* name — never the
            # torrent's own spelling.
            new_filename = (
                f"{folder.name} - S{parsed.season:02d}E{parsed.episode:02d}"
            )
        return RoutePlan(
            confident=True, dest_dir=str(dest),
            show_folder=str(folder), season_folder=season_name,
            new_filename=new_filename, parsed=parsed, library_root=root,
            reason=f"show '{folder.name}' matched at {score:.0%}",
        )

    # Movies (and other/unknown): route to the movie root on the drive with
    # the most free space (or the Settings override).
    roots = [p for p in config.media_paths_for_types("movie") if Path(p).is_dir()] \
        if media_type == "movie" else []
    if media_type == "movie" and roots:
        chosen = pick_root_by_free_space(roots) or roots[0]
        return RoutePlan(
            confident=True, dest_dir=chosen, parsed=parsed,
            library_root=chosen,
            reason=f"movie root ({Path(chosen).name}, most free space)",
            new_filename=None,  # movies keep their original filename
        )
    return RoutePlan(
        confident=False, dest_dir=staging, parsed=parsed,
        reason="no library root configured for this media type",
    )
