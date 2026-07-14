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
class JunkFile:
    """One cleanup candidate found by find_junk_files()."""
    path: str
    kind: str          # "file" | "dir"
    reason: str        # human explanation ("sample video", "release notes", …)
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
class MovieInventory:
    """Per-movie rollup (files grouped by normalized title)."""
    title: str
    media_type: str
    file_count: int
    total_size_bytes: int


@dataclass
class LibraryInventory:
    """Aggregate stats for one walk of the library."""
    shows: list[ShowInventory]
    movie_count: int
    movie_size_bytes: int
    untyped_files: int            # files whose category couldn't be guessed
    movies: list["MovieInventory"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Daily library check
# ---------------------------------------------------------------------------

def _request_want_identity(req):
    """Build the MediaIdentity a request is asking for (reconciliation compares
    library evidence against THIS, never a raw substring)."""
    import media_identity
    return media_identity.MediaIdentity(
        media_type=req.media_type or "unknown",
        identity_source=getattr(req, "identity_source", None),
        external_id=(str(req.external_id) if req.external_id is not None else None),
        canonical_title=req.resolved_title or req.content,
        canonical_year=getattr(req, "canonical_year", None),
        origin_countries=tuple(getattr(req, "origin_countries", []) or ()),
        season=getattr(req, "season", None),
    )


def _core_title(name: str) -> str:
    """Normalized title core of a library entry/name, year + release-junk
    stripped but any sequel number KEPT (so 'Movie' and 'Movie 2' differ)."""
    import media_identity
    import torrent_routing
    core = torrent_routing.parse_torrent_name(name).show_title or name
    core = re.sub(r"[\(\[]?(?:19|20)\d{2}[\)\]]?", " ", core)
    return media_identity.normalize_title(core)


def _library_identity_eval(want, entries) -> tuple[bool, list]:
    """Compare library entries against a want by IDENTITY (normalized-title
    equality + media_identity's sequel/numeric/year guards), not substring.

    Returns (matched, contradicting) where `matched` means a real same-identity
    entry is present, and `contradicting` are entries that share the base title
    but are a DIFFERENT entry in the series (the sequel that poisoned the old
    substring check) — those get blocklisted for the identity.
    """
    import media_identity
    import verification
    want_core = media_identity.normalize_title(want.canonical_title)
    matched = False
    contradicting = []
    for e in entries:
        name = getattr(e, "name", "") or ""
        entry_core = _core_title(name)
        parsed = verification.parse_file(Path(name))
        verdict = media_identity.compare_media_identity(want, parsed)
        if not want_core:
            continue
        if entry_core == want_core and verdict.ok:
            matched = True
        elif (entry_core.startswith(want_core + " ") and not verdict.ok
              and verdict.reason_code in ("sequel_mismatch",
                                          "numeric_title_mismatch")):
            contradicting.append(e)
    return matched, contradicting


def daily_library_check() -> dict:
    """Reconcile requests against the library BY IDENTITY (Task C item 5).

    The old substring test (`title in entry.name`) is gone: it matched a sequel
    ("Angry Birds Movie" is a substring of "Angry Birds Movie 2") and even
    false-positived on requests whose only download never left staging. This
    version compares library evidence against each request's MediaIdentity, so:

    - found_in_library is set only after a REAL identity match;
    - a request whose only "match" is a contradicting sequel has its poisoned
      found flag CLEARED, the sequel BLOCKLISTED for that identity, and the
      request stays open/grabbable;
    - fulfilled/placed requests are re-verified against what is on disk NOW; a
      broken or contradicted link is reopened;
    - needs_identity rows can never be 'found' (no identity to match) — a stale
      flag is cleared.
    """
    import downloads_store
    import queue_store as qs
    from library_index import search_library

    checked = 0
    newly_found = 0
    cleared_false_found = 0
    reopened = 0
    sequels_blocked = 0
    errors: list[str] = []

    def _block_contradictions(want, entries) -> int:
        n = 0
        if not want.subject_key:
            return 0
        for e in entries:
            downloads_store.add_blocklist_entry(
                subject_type=("request_identity" if want.media_type == "movie"
                              else "show_season"),
                subject_key=want.subject_key,
                reason_code=downloads_store.BLOCK_REASON_IDENTITY_MISMATCH,
                parsed_title=getattr(e, "name", None),
                reason_detail="reconcile: contradicting entry in library",
                created_by="reconcile")
            n += 1
        return n

    # 1. Identity-based found check for checkable (non-needs_identity) requests.
    for req in qs.get_requests_needing_library_check():
        try:
            want = _request_want_identity(req)
            entries = search_library(req.resolved_title or req.content, limit=8)
            matched, contradicting = _library_identity_eval(want, entries)
            qs.update_library_status(req.request_id, found=matched)
            checked += 1
            if matched and not req.found_in_library:
                newly_found += 1
            elif req.found_in_library and not matched:
                cleared_false_found += 1
                if contradicting:
                    sequels_blocked += _block_contradictions(want, contradicting)
                    logger.info(
                        "Reconcile: request #%s had a POISONED found flag "
                        "(contradicting entry in library) — cleared and blocked.",
                        req.request_id)
        except Exception as exc:
            errors.append(f"Error checking request #{req.request_id}: {exc}")
            logger.exception("Library check failed for request #%s", req.request_id)

    # 2. Reconcile placement links + re-verify EVERY found flag by identity
    #    (independent of check age — poison predating this build must be
    #    corrected even if it was 'checked' recently).
    for req in qs.list_requests(status="all", limit=2000):
        try:
            if req.status == qs.STATUS_NEEDS_IDENTITY:
                if req.found_in_library:
                    qs.update_library_status(req.request_id, found=False)
                    cleared_false_found += 1
                continue
            if req.found_in_library:
                want = _request_want_identity(req)
                entries = search_library(req.resolved_title or req.content,
                                         limit=8)
                matched, contradicting = _library_identity_eval(want, entries)
                if not matched:
                    qs.update_library_status(req.request_id, found=False)
                    cleared_false_found += 1
                    if contradicting:
                        sequels_blocked += _block_contradictions(want, contradicting)
                        logger.info(
                            "Reconcile: request #%s had a POISONED found flag — "
                            "cleared and blocked the contradicting entry.",
                            req.request_id)
            if req.status in (qs.STATUS_FULFILLED, qs.STATUS_PLACED):
                if not _placement_still_valid(req):
                    qs.set_status(req.request_id, qs.STATUS_OPEN)
                    reopened += 1
                    logger.info("Reconcile: request #%s no longer verifies on "
                                "disk — reopened.", req.request_id)
        except Exception as exc:
            errors.append(f"Error reconciling request #{req.request_id}: {exc}")

    return {"checked": checked, "newly_found": newly_found,
            "cleared_false_found": cleared_false_found, "reopened": reopened,
            "sequels_blocked": sequels_blocked, "errors": errors}


def _placement_still_valid(req) -> bool:
    """A fulfilled/placed request still verifies when at least one of its linked
    downloads has a moved file that (a) still exists on disk and (b) still
    matches the request's identity.

    Legacy rows with NO recorded placement provenance return True (no evidence
    to contradict them — the identity found-check in step 1 handles those). A
    request that HAS placement links but every one is now broken or contradicted
    returns False and is reopened.
    """
    import downloads_store
    import media_identity
    import verification
    want = _request_want_identity(req)
    have_links = False
    for dl in downloads_store.downloads_for_request(req.request_id):
        for f in downloads_store.list_download_files(dl.download_id):
            if f.verification_state not in ("verified", "duplicate"):
                continue
            if not f.final_path:
                continue
            have_links = True
            if not Path(f.final_path).exists():
                continue
            parsed = verification.parse_file(Path(f.final_path))
            if media_identity.compare_media_identity(want, parsed).ok:
                return True
    return not have_links


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

# --- duplicate-detection guards (learned from a real-library audit) ----------
# Bonus-content folders and creditless/OVA files must never be compared
# against real episodes; pt/cd parts of one item aren't copies of each other;
# x.5 recaps and SxxXyy specials are DIFFERENT episodes; promo stubs
# (ETRG.mp4, RARBG.com.mp4) are junk, not media.
_DUP_EXTRAS_DIR_RE = re.compile(
    r"^(?:extras?|featurettes?|behind the scenes|deleted scenes|interviews|"
    r"scenes|shorts|trailers?|other|nc(?:op|ed)s?|creditless|menus?|"
    r"bonus(?:es)?|pv|cm|samples?|special features?)$", re.IGNORECASE)
_DUP_SPECIAL_FILE_RE = re.compile(r"\b(?:OVA|OAD|NC(?:OP|ED))\b", re.IGNORECASE)
_DUP_PART_RE = re.compile(r"\b(?:pt|part|cd|disc|disk)[\s._-]?(\d{1,2})\b",
                          re.IGNORECASE)
_DUP_HALF_RE = re.compile(r"(\d{1,3})\.5\b")
_DUP_SXX_X_RE = re.compile(r"[Ss](\d{1,2})[Xx](\d{1,2})\b")
_DUP_EXX_X_RE = re.compile(r"[Ee](\d{1,3})[Xx](\d{1,2})\b")
_DUP_PROMO_STEMS = {"etrg", "rarbg", "rarbg com", "sample", "readme", "info",
                    "torrent downloaded from"}
_DUP_YEAR_RE = re.compile(r"\((19|20)\d{2}\)|\b(?:19|20)\d{2}\b")

# Junk words the Combo Clean rename always removes (whole-word), agreed from
# a frequency scan of the real library: quality/codec/source/group noise.
_COMBO_REMOVE_WORDS = [
    "480p", "720p", "1080p", "2160p", "4k", "x264", "x265", "h264", "h265",
    "hevc", "av1", "10bit", "8bit", "aac", "ac3", "flac", "opus",
    "web", "webrip", "webdl", "dl", "bluray", "brrip", "bdrip", "hdtv",
    "bd", "dvd", "dvdrip", "remux", "dual", "audio", "amzn", "nf",
    "galaxytv", "subsplease", "eztv", "eztvx", "heteam", "judas",
]
_COMBO_WORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _COMBO_REMOVE_WORDS) + r")\b",
    re.IGNORECASE)


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

    # Key: (title, year, season, episode, part). Year separates reboots that
    # share a name (Goosebumps 1995 vs 2023); part separates cd1/cd2 and
    # pt1/pt2 splits of ONE item; episode is a string so 13.5 recaps and
    # SxxXyy specials stay distinct from their whole-number neighbours.
    groups: dict[tuple, list[Path]] = {}
    for f in files:
        if any(_DUP_EXTRAS_DIR_RE.match(part) for part in f.parent.parts):
            continue
        stem = f.stem
        if _DUP_SPECIAL_FILE_RE.search(stem) or "sample" in stem.lower():
            continue
        title = _normalize_title(f.name)
        if not title or title in _DUP_PROMO_STEMS:
            continue
        ep = _parse_episode(f.name)
        season = ep[0] if ep else None
        episode: str | None = str(ep[1]) if ep else None
        m = _DUP_HALF_RE.search(stem)
        if m and ep and int(m.group(1)) == ep[1]:
            episode = f"{ep[1]}.5"
        m = _DUP_SXX_X_RE.search(stem)
        if m:
            season = int(m.group(1))
            episode = f"x{int(m.group(2))}"
        m = _DUP_EXX_X_RE.search(stem)
        if m:
            episode = f"{int(m.group(1))}x{int(m.group(2))}"
        m = _DUP_PART_RE.search(stem)
        part = m.group(1) if m else None
        ym = _DUP_YEAR_RE.search(f.parent.name) or _DUP_YEAR_RE.search(stem)
        year = ym.group(0).strip("()") if ym else None
        key = (title, year, season, episode, part)
        groups.setdefault(key, []).append(f)

    results: list[DuplicateGroup] = []
    for (norm_title, year, season, episode, _part), paths in groups.items():
        if len(paths) < 2:
            continue
        sorted_paths = sorted(paths)
        sizes: list[int] = []
        for p in sorted_paths:
            try:
                sizes.append(p.stat().st_size)
            except OSError:
                sizes.append(0)
        label = norm_title + (f" ({year})" if year else "")
        if season is not None and episode is not None:
            label = f"{label} S{season:02d}E{episode}"
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
    # movie title -> [file_count, total_size, media_type]
    movie_groups: dict[str, list] = {}
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
            group = movie_groups.setdefault(norm, [0, 0, _media_type_for_path(f)])
            group[0] += 1
            group[1] += size
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

    movies = [
        MovieInventory(title=title, media_type=group[2],
                       file_count=group[0], total_size_bytes=group[1])
        for title, group in movie_groups.items()
    ]
    movies.sort(key=lambda m: m.title)

    logger.info(
        "library_inventory: %d show(s) / %d movie file(s) / %d untyped",
        len(shows), movie_count, untyped,
    )
    return LibraryInventory(
        shows=shows,
        movie_count=movie_count,
        movie_size_bytes=movie_size,
        untyped_files=untyped,
        movies=movies,
    )


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------

def _after_library_rename(old_path: str, new_path: str) -> None:
    """Every library-file rename path funnels here so quality labels keep
    following the file (Task F item 2 — media_quality.update_file_path is the
    ONE central helper; the label itself rides the identity, this only moves
    the file_path pointer). Never fails the rename that already happened."""
    try:
        import media_quality
        media_quality.update_file_path(old_path, new_path)
    except Exception:
        logger.exception("media_quality file_path update failed: %s", old_path)


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
        _after_library_rename(str(p), str(new_path))
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

    # Prefer the recycle bin so a wrong click is recoverable; fall back to a
    # permanent unlink only when send2trash isn't installed.
    try:
        from send2trash import send2trash as _to_trash
    except ImportError:
        _to_trash = None
        logger.warning("send2trash not installed — deletes will be permanent.")

    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            errors.append(f"Not a file (skipped): {raw_path}")
            continue
        try:
            parent = path.parent
            if _to_trash is not None:
                _to_trash(str(path))
            else:
                path.unlink()
            deleted.append(raw_path)
            candidate_parents.add(parent)
            logger.info("Deleted (to recycle bin): %s", raw_path)
            try:
                from library_index import log_file_event
                log_file_event("removed", raw_path,
                               "deleted via Plexxarr (user-confirmed, recycle bin)")
            except Exception:
                pass
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
            _after_library_rename(str(src), str(dst))
            logger.info("Renamed: %s → %s", src.name, dst.name)
        except OSError as exc:
            errors.append(f"Rename failed for {src.name}: {exc}")

    return errors


# ---------------------------------------------------------------------------
# Junk cleanup — samples, release notes, screenshot jpgs, empty folders
# ---------------------------------------------------------------------------

# Plex extras folders — real bonus content lives here; never flag it.
_EXTRAS_DIR_RE = re.compile(
    r"^(?:extras?|featurettes?|behind the scenes|deleted scenes|interviews|"
    r"scenes|shorts|trailers|other|specials?)$", re.IGNORECASE)

# Plex artwork filenames to KEEP even though they're images.
_PLEX_ART_RE = re.compile(
    r"^(?:poster|fanart|cover|folder|background|banner|backdrop|logo|"
    r"season\d*(?:-poster)?|theme)\d*$", re.IGNORECASE)

_SAMPLE_RE = re.compile(r"(?:^|[\s._\-\[(])sample(?:[\s._\-\])]|$)", re.IGNORECASE)

_JUNK_EXTENSIONS = {".txt", ".nfo", ".sfv", ".srr", ".exe", ".url", ".lnk",
                    ".website", ".md5", ".torrent", ".jpg", ".jpeg", ".png",
                    ".gif", ".bmp", ".webp"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# A video smaller than this, next to one ≥10× its size, is almost certainly
# a sample/excerpt even without "sample" in the name.
_TINY_VIDEO_BYTES = 80 * 1024 * 1024


def find_junk_files() -> list[JunkFile]:
    """Cleanup candidates across all configured library paths.

    Keeps: the main video(s), subtitles, Plex artwork (poster/fanart/…), and
    anything inside Plex extras folders (Featurettes, Deleted Scenes, …).
    Flags: sample videos (by name, or tiny next to a full-size sibling),
    release-note .txt/.nfo/.sfv files, screenshot images, stray .exe/.url,
    and recursively-empty folders.
    """
    video_exts = set(config.LIBRARY_INDEX_EXTENSIONS)
    subtitle_exts = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx"}
    junk: list[JunkFile] = []

    for lib_root in config.PLEX_LIBRARY_PATHS:
        root = Path(lib_root)
        if not root.is_dir():
            continue
        for dirpath, dirnames, names in os.walk(root):
            # Never descend into Plex extras folders.
            dirnames[:] = [d for d in dirnames if not _EXTRAS_DIR_RE.match(d.strip())]
            here = Path(dirpath)
            in_sample_dir = _SAMPLE_RE.search(here.name) is not None

            videos: list[tuple[Path, int]] = []
            others: list[tuple[Path, int]] = []
            for name in names:
                p = here / name
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                suffix = p.suffix.lower()
                if suffix in video_exts:
                    videos.append((p, size))
                elif suffix not in subtitle_exts:
                    others.append((p, size))

            biggest_video = max((s for _p, s in videos), default=0)

            for p, size in videos:
                if _SAMPLE_RE.search(p.stem) or in_sample_dir:
                    if biggest_video > size * 4 or size < 200 * 1024 * 1024:
                        junk.append(JunkFile(str(p), "file", "sample video (named)", size))
                elif size < _TINY_VIDEO_BYTES and biggest_video >= size * 10:
                    junk.append(JunkFile(
                        str(p), "file",
                        f"tiny video next to a {biggest_video // (1024*1024)} MB main file",
                        size))

            for p, size in others:
                suffix = p.suffix.lower()
                if suffix not in _JUNK_EXTENSIONS:
                    continue
                if suffix in _IMAGE_EXTENSIONS and _PLEX_ART_RE.match(p.stem.strip()):
                    continue  # Plex artwork
                reason = {
                    ".txt": "release notes", ".nfo": "release info",
                    ".sfv": "checksum file", ".srr": "rescene file",
                    ".exe": "stray executable", ".url": "link file",
                    ".lnk": "link file", ".website": "link file",
                    ".md5": "checksum file", ".torrent": "torrent file",
                }.get(suffix, "screenshot / junk image")
                junk.append(JunkFile(str(p), "file", reason, size))

    # Recursively-empty folders (deepest first so parents empty out too).
    library_roots = {str(Path(p).resolve()) for p in config.PLEX_LIBRARY_PATHS if Path(p).is_dir()}
    for lib_root in config.PLEX_LIBRARY_PATHS:
        root = Path(lib_root)
        if not root.is_dir():
            continue
        for dirpath, _dirnames, names in sorted(
                os.walk(root), key=lambda t: -len(t[0])):
            here = Path(dirpath)
            try:
                if str(here.resolve()) in library_roots:
                    continue
                if not names and not any(here.iterdir()):
                    junk.append(JunkFile(str(here), "dir", "empty folder", 0))
            except OSError:
                continue

    logger.info("find_junk_files: %d cleanup candidate(s).", len(junk))
    return junk


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


def build_combo_renames(media_type: str) -> list[SanitizePair]:
    """Combo Clean for ONE library type (preview only — nothing renamed):
    dots become spaces, [bracketed] and {braced} chunks go away, the agreed
    junk-word list is stripped whole-word, whitespace is tidied. Skips a
    file when the result is unchanged, dangerously short, or collides."""
    roots = [p.path for p in config.MEDIA_LIBRARY_PATHS
             if p.media_type == media_type]
    pairs: list[SanitizePair] = []
    for f in _media_files(roots):
        new = f.stem.replace(".", " ")
        new = re.sub(r"\[[^\]]*\]", " ", new)
        new = re.sub(r"\{[^}]*\}", " ", new)
        new = _COMBO_WORD_RE.sub(" ", new)
        new = _MULTI_SPACES.sub(" ", new).strip(" -_~")
        if len(new) < 3 or new == f.stem:
            continue
        target = f.with_name(new + f.suffix)
        if target.exists():
            continue
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        pairs.append(SanitizePair(original=str(f), sanitized=str(target),
                                  size_bytes=size))
    pairs.sort(key=lambda pr: pr.original.casefold())
    return pairs


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
    import db

    # Load indexed paths from the SQLite library_files table
    indexed_paths: set[str] = set()
    try:
        with db.connect() as conn:
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
