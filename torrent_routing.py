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
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import config

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = set(config.LIBRARY_INDEX_EXTENSIONS)
# .smi (SAMI) added in Task D2 item 4. .sub/.idx are a PAIRED VobSub asset —
# both listed here, but the move loop moves them together or not at all (a lone
# .sub VobSub track is broken without its .idx index; see VOBSUB_EXTENSIONS).
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".smi", ".idx"}
# The VobSub pair: neither half is usable alone.
VOBSUB_EXTENSIONS = {".sub", ".idx"}

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')


def sanitize_for_filesystem(name: str) -> str:
    """Strip characters Windows refuses in file/folder names. AniDB titles
    use a backtick for an apostrophe ("JoJo`s"); write a real one."""
    return _INVALID_FS_CHARS.sub("", name.replace("`", "'")).strip()


# A release name, not a show name: an episode marker or release-quality junk.
_RELEASE_NAME_RE = re.compile(
    r"(?i)(?<![a-z0-9])(s\d{1,2}e\d{1,3}|\d{3,4}p|web-?dl|webrip|bluray|"
    r"blu-ray|bdrip|hdtv|x26[45]|h\.?26[45]|hevc|av1|aac\d?|ddp?\d)(?![a-z0-9])")


# A leading "[Group] " tag followed by more text is a release prefix; a title
# that IS a bracketed name ("[Oshi no Ko]") is not.
_GROUP_PREFIX_RE = re.compile(r"^\s*\[[^\]]+\]\s*\S")


def looks_like_release_name(title: str | None) -> bool:
    """True for a raw torrent/file name ("[ToonsHub] Show S06E03 1080p ...").
    Such a string must never become a show folder name."""
    text = title or ""
    return bool(_RELEASE_NAME_RE.search(text) or _GROUP_PREFIX_RE.match(text))


def _clean_folder_title(name: str) -> str:
    """A folder name usable as a file-name base: as is when clean, else the
    show scanner's cleanup ("World Trigger [BD]" -> "World Trigger")."""
    if not looks_like_release_name(name) and "[" not in name:
        return name
    try:
        import show_tracker
        cleaned, _year = show_tracker.clean_show_folder_name(name)
    except Exception:
        cleaned = ""
    return cleaned


def _same_show(base: str, *names: str) -> bool:
    """Does an existing file-name base belong to this show? (Guards against a
    shared season-target folder full of another show's files.)"""
    nb = _norm(base)
    cb = re.sub(r"[^a-z0-9]", "", nb)
    for name in names:
        nn = _norm(name or "")
        cn = re.sub(r"[^a-z0-9]", "", nn)
        if not cb or not cn:
            continue
        # Spaces and punctuation ignored: "JoJos Bizarre Adventure" belongs to
        # "JoJo`s Bizarre Adventure (2012)". Short names must match exactly so
        # "Show A" never takes "Show B"'s files.
        shorter = min(len(cb), len(cn))
        if cb == cn or (shorter >= 5 and (cb in cn or cn in cb)):
            return True
        try:
            from rapidfuzz import fuzz
            if fuzz.token_set_ratio(nb, nn) >= 90 and shorter >= 5:
                return True
        except ImportError:
            pass
    return False


_EPISODE_FILE_RE = re.compile(r"^(?P<base>.+?) - S\d{2,}E\d{2,}", re.IGNORECASE)


def episode_name_base(show_title: str, show_folder: str | None = None,
                      season_dir: str | None = None) -> str:
    """The "<base>" in "<base> - S06E04": whatever the episodes already in
    this season folder use, else the show folder's own (clean) name, else the
    tracked title. Files land next to "JoJos Bizarre Adventure - S06E03.mkv",
    so the new one is "JoJos Bizarre Adventure - S06E04.mkv", not AniDB's
    "JoJo`s Bizarre Adventure (2012) - S06E04.mkv"."""
    folder_title = _clean_folder_title(Path(show_folder).name.strip()) if show_folder else ""
    if season_dir:
        bases: dict[str, int] = {}
        try:
            for entry in Path(season_dir).iterdir():
                if entry.is_file() and entry.suffix.lower() in VIDEO_EXTENSIONS:
                    m = _EPISODE_FILE_RE.match(entry.stem)
                    if m:
                        base = m.group("base").strip()
                        # Never copy junk or another show's name.
                        if (not looks_like_release_name(base)
                                and _same_show(base, show_title, folder_title)):
                            bases[base] = bases.get(base, 0) + 1
        except OSError:
            pass
        if bases:
            return sanitize_for_filesystem(max(bases, key=lambda b: bases[b]))
    if folder_title:
        return sanitize_for_filesystem(folder_title)
    cleaned_title = _clean_folder_title(show_title)
    return sanitize_for_filesystem(cleaned_title or show_title)


# --- Movie folder + stem naming (Task D2) -----------------------------------

def movie_display_base(title: str, year: int | None) -> str:
    """The `<Canonical Title (Year)>` base both the per-movie folder and the
    primary video stem share. Only filesystem-hostile characters are stripped;
    the year parens are preserved (Plex reads them)."""
    base = f"{title} ({year})" if year else title
    return sanitize_for_filesystem(base)


def movie_folder_name(title: str, year: int | None,
                      tmdb_id: str | int | None = None) -> str:
    """Per-movie folder name.  RESOLVED DECISION 4: a VERIFIED TMDB identity is
    tagged as `<Canonical Title (Year)> {tmdb-12345}`; without one the folder is
    just `<Canonical Title (Year)>` (never a fabricated/translated id). The brace
    tag is appended AFTER sanitising the base so the braces survive."""
    base = movie_display_base(title, year)
    tmdb = str(tmdb_id).strip() if tmdb_id not in (None, "") else ""
    if tmdb:
        return f"{base} {{tmdb-{tmdb}}}"
    return base

# Confidence needed before we'll route into an existing show folder.
_SHOW_MATCH_THRESHOLD = 0.85


def _same_volume(a: Path, b: Path) -> bool:
    """Do two paths live on the same disk/volume?

    Windows keeps the exact drive-letter comparison it always had. POSIX has
    no useful Path.drive (it is "" for every path — comparing it would call
    EVERYTHING the same drive), so volumes compare by the st_dev of the
    deepest existing ancestor: /mnt/disk1 vs /media/disk2 vs an ordinary
    path all resolve honestly (Task H item 7).
    """
    if sys.platform == "win32":
        return a.drive.upper() == b.drive.upper()

    def dev_of(path: Path) -> int | None:
        p = path
        while True:
            try:
                return os.stat(p).st_dev
            except OSError:
                if p == p.parent:
                    return None
                p = p.parent

    dev_a, dev_b = dev_of(a), dev_of(b)
    return dev_a is not None and dev_a == dev_b


def pick_root_by_free_space(candidates: list[str]) -> str | None:
    """Where NEW content should land, among the given candidate folders.

    DOWNLOAD_ROOT_OVERRIDE (Settings) hard-pins the choice: an exact folder
    wins outright; a volume-style override ("I:\\" on Windows, a mount point
    like "/mnt/disk1" on Linux) narrows candidates to that volume. Otherwise
    the folder on the volume with the most free space wins — drive letters
    and mount names can shuffle, free space follows the disk.
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
        on_override_volume = [
            c for c in dirs if _same_volume(Path(c), override_path)
        ]
        if on_override_volume:
            dirs = on_override_volume
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
    # "[Raze] Dandadan S2 - 11 (...)" — season token, separator, episode.
    re.compile(r"\bS(?P<season>\d{1,2})\s*[-._]\s*(?P<episode>\d{1,3})(?:v\d+)?\b",
               re.IGNORECASE),
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
    # new_filename is a STEM, not a full filename: `_post_process` appends the
    # source extension per file, and multi-file payloads (season packs,
    # multipart movies) derive one stem PER FILE from it. None = keep original.
    new_filename: str | None = None
    show_folder: str | None = None
    season_folder: str | None = None
    parsed: ParsedName | None = None
    library_root: str | None = None
    # True when a per-movie folder was created WITHOUT a verified TMDB tag —
    # the folder is valid but flagged for later metadata repair (Task D2 item 1).
    missing_folder_id: bool = False

    @property
    def new_stem(self) -> str | None:
        """Explicit alias — new_filename has always been a stem (Task D2 item 2)."""
        return self.new_filename

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
    # Match the identity layer's treatment of ampersands.  Release names often
    # spell ``&`` as ``and`` (or vice versa); that is not a different show.
    return re.sub(
        r"[^a-z0-9]+", " ", text.casefold().replace("&", " and ")).strip()


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

def plan_route(torrent_name: str, media_type: str, *, request_title: str | None = None,
               movie_title: str | None = None, movie_year: int | None = None,
               movie_tmdb_id: str | int | None = None) -> RoutePlan:
    """Plan destination + optional rename for a torrent, before or after download.

    request_title, when provided (the resolved title from the request queue),
    is preferred over the parsed torrent name for show matching — it's the
    canonical name the user actually asked for.

    movie_title/movie_year/movie_tmdb_id (Task D2), when a qualified movie
    identity is known, drive the per-movie folder + `{tmdb-ID}` tag. Without a
    TMDB id the folder is created untagged and missing_folder_id is set.
    """
    staging = str(Path(config.TORRENT_DOWNLOAD_DIR))
    parsed = parse_torrent_name(torrent_name)

    if media_type in ("tv", "anime", "xanime"):
        if looks_like_release_name(request_title):
            # A raw release name is not a canonical title (the JoJo S06 mess of
            # 2026-10-04 made a show folder out of one): match by the parsed
            # show name instead, and never create a folder from it.
            request_title = None
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
        # Task D2 item 1: per-movie folder, always. A qualified canonical title
        # (from want_json) drives `<Title (Year) {tmdb-ID}>/` + a readable
        # `<Title (Year)>` stem; an unlinked/manual movie folders under its
        # parsed title with NO fabricated tag (missing_folder_id flagged).
        title = (movie_title or "").strip() or parsed.show_title.strip()
        if title:
            year = movie_year if movie_year is not None else _detect_year(torrent_name)
            tmdb = str(movie_tmdb_id).strip() if movie_tmdb_id not in (None, "") else ""
            folder = movie_folder_name(title, year, tmdb or None)
            dest = str(Path(chosen) / folder)
            # NB: show_folder stays None — it means a TV SHOW folder to _post_
            # process (it derives the rename base from it). A movie's rename base
            # is new_filename, not the folder name.
            return RoutePlan(
                confident=True, dest_dir=dest, parsed=parsed,
                library_root=chosen,
                new_filename=movie_display_base(title, year),
                missing_folder_id=not tmdb,
                reason=(f"movie folder '{folder}' under {Path(chosen).name}"
                        + ("" if tmdb else " (no verified TMDB id)")),
            )
        # No usable title at all — keep the file flat rather than invent a folder
        # name from release junk.
        return RoutePlan(
            confident=True, dest_dir=chosen, parsed=parsed,
            library_root=chosen,
            reason=f"movie root ({Path(chosen).name}, most free space)",
            new_filename=None,
        )
    return RoutePlan(
        confident=False, dest_dir=staging, parsed=parsed,
        reason="no library root configured for this media type",
    )


_YEAR_IN_NAME_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")


def _detect_year(name: str) -> int | None:
    m = _YEAR_IN_NAME_RE.search(name)
    return int(m.group(1)) if m else None
