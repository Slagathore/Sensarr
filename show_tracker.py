# =============================================================================
# show_tracker.py
# =============================================================================
# Radarr/Sonarr-style tracking logic, built on the same per-type tracker stack
# the request pipeline uses (Cole's requirement — TVDB→TMDB for TV, Jikan/MAL
# for anime and xanime; AniDB is identification-only, it has no cheap episode
# API):
#
#   scan_library_folders()  — walk typed tv/anime/xanime roots, identify each
#                             show folder against its tracker, map folders to
#                             shows (multiple folders per show = seasons split
#                             across drives, fully supported).
#   sync_show() / sync_all() — pull authoritative episode lists + air dates,
#                             then re-scan mapped folders to mark which
#                             episodes exist on disk.
#
# Missing = aired episodes without a file. Upcoming = air dates in the next
# N days. Both are plain queries in shows_store.
# =============================================================================

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import config
import shows_store
import torrent_routing
from media_lookup import (
    EpisodeInfo, MediaResult,
    best_title_similarity,
    get_anime_airing,
    get_jikan_episodes, get_jikan_status,
    get_tmdb_next_air, get_tmdb_tv_episodes, get_tmdb_tv_status,
    get_tvdb_episodes, get_tvdb_series_status,
    jikan_circuit_open, resolve_tmdb_tv_id,
    search_anidb, search_anilist, search_jikan_anime,
    search_tmdb_anime, search_tmdb_shows, search_tvdb_shows,
)
from torrent_routing import VIDEO_EXTENSIONS, parse_torrent_name

logger = logging.getLogger(__name__)

# --- Concurrency guard -------------------------------------------------------
# Scan and Sync each fan out many external API calls (Jikan especially is
# rate-limited). Clicking "Scan Folders" 4 times used to launch 4 concurrent
# scans that quadrupled the request rate and tripped rate limits; the 6-hour
# auto-grab scheduler could also overlap a manual sync. This RLock lets only
# ONE of these operations run at a time across the whole app, while its
# reentrancy still allows sync_all()/auto-grab to call sync_show() internally.
_OPERATION_LOCK = threading.RLock()


class ShowsBusyError(RuntimeError):
    """Raised when a Shows scan/sync is already running elsewhere."""


def run_exclusive(name: str, fn):
    """Run fn() only if no other Shows operation holds the lock.

    Same-thread nested calls (sync_all → sync_show) are allowed via the RLock;
    a different thread trying to start gets ShowsBusyError immediately instead
    of piling on more concurrent API calls.
    """
    if not _OPERATION_LOCK.acquire(blocking=False):
        raise ShowsBusyError(f"A Shows operation is already running — '{name}' skipped.")
    try:
        return fn()
    finally:
        _OPERATION_LOCK.release()


_IDENTIFY_THRESHOLD = 0.72

# Folder names like "Show Name (2023)" — year improves tracker matching.
_FOLDER_YEAR_RE = re.compile(r"\((?P<year>(19|20)\d{2})\)")

# Release-group / quality / codec noise stripped from folder names before a
# tracker lookup. Anime and hentai folders are especially junk-heavy
# ("[bonkai77] Title [WEB-DL][1080p][x265]"), which otherwise poisons search.
_JUNK_WORD_RE = re.compile(
    r"\b(?:480p|720p|1080p|2160p|4k|x26[45]|h\.?26[45]|hevc|avc|10bit|8bit|"
    r"web-?dl|web-?rip|bd(?:rip)?|blu-?ray|br-?rip|hdtv|dvd(?:-?rip)?|remux|batch|"
    r"complete|dual|audio|multi|eng(?:lish)?|subs?(?:bed)?|dub(?:bed)?|"
    r"aac\d?|ac3|flac|opus|ddp?\d?(?:\.\d)?|hi10p?|uncensored|"
    r"censored|repack|proper|extended|remastered)\b",
    re.IGNORECASE,
)
# Codec tokens that get concatenated to other text ("HEVCx265", "10bitx265").
_JUNK_CODEC_TOKEN_RE = re.compile(r"\b\w*(?:hevc|x26[45]|h26[45]|10bit)\w*\b", re.IGNORECASE)
# A leading "[date - date]" or "[group]" bracket prefix (hentai folders).
_LEADING_BRACKET_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)+")

# Anime fallback: files numbered without SxxEyy ("Show - 12 [1080p].mkv").
# Take the last standalone 1-4 digit number that isn't a year/resolution.
_ABS_EP_RE = re.compile(r"(?:^|[\s._-])(\d{1,4})(?=[\s._-]|$)")
_NOT_EPISODE = re.compile(r"^(?:19|20)\d{2}$|^(?:480|720|1080|2160)$")

# "Episode 01 - Title.mp4" / "Ep 5.mkv" / "_Ep01_" / "Show - E01 [BD].mkv" —
# episode markers with no season token (the season lives in the parent folder
# name). \b can't be used before Ep: underscores are word chars, so
# "_Ep01_" (Exiled-Destiny style) would never match.
_EP_WORD_RE = re.compile(
    r"(?:^|[\s._\-(\[])(?:Episode|Ep)\.?[\s._-]*(\d{1,4})(?:v\d+)?(?=[\s._\-)\]]|$)",
    re.IGNORECASE)
_BARE_E_RE = re.compile(r"(?:^|[\s._-])E(\d{1,3})(?:v\d+)?(?=[\s._-]|$)", re.IGNORECASE)
# "01) Great Sword.mkv" — leading track-style numbering.
_LEADING_NUM_RE = re.compile(r"^\s*(\d{1,3})[)\].\s_-]")
# OVA/OAD/creditless markers → season 0, so "Show OVA - 01" can't collide
# with the real S01E01.
_SPECIAL_MARKER_RE = re.compile(r"\b(?:OVA|OAD|NC(?:OP|ED))\b", re.IGNORECASE)

# Parent folder names that carry the season: "Season 02", "Season 2", "S02",
# and suffixed forms like "Noragami Season_1 L@mBerT" (matched anywhere).
_SEASON_DIR_RE = re.compile(r"(?:^|[\s._-])(?:Season|S)[\s._-]*(\d{1,3})(?=[\s._-]|$)",
                            re.IGNORECASE)
_SPECIALS_DIR_RE = re.compile(r"^specials?$", re.IGNORECASE)
# Bonus-content folders that must NEVER be scanned as episodes: trailers,
# creditless openings/endings, extras — a "Trailer 1" would otherwise be
# counted as episode 1 of something.
_EXTRAS_DIR_RE = re.compile(
    r"^(?:extras?|featurettes?|behind the scenes|deleted scenes|interviews|"
    r"scenes|shorts|trailers?|other|nc(?:op|ed)s?|creditless|menus?|"
    r"bonus(?:es)?|pv|cm)$", re.IGNORECASE)


@dataclass(frozen=True)
class ScanResult:
    identified: int
    already_tracked: int
    unidentified: list[str]


def _typed_roots(media_type: str) -> list[Path]:
    """Roots explicitly tagged with this type (mixed roots are skipped — a
    mixed folder can contain movies, and misidentifying those as shows would
    poison the inventory)."""
    return [
        Path(p.path) for p in config.MEDIA_LIBRARY_PATHS
        if p.media_type == media_type and Path(p.path).is_dir()
    ]


def clean_show_folder_name(folder_name: str) -> tuple[str, int | None]:
    """Reduce a messy folder name to a searchable title + optional year.

    Handles the real-world junk seen in the library: leading [group]/[date]
    brackets, embedded [quality] tags, underscores/dots as separators, and
    trailing codec/release noise. Returns ("", None) for non-show folders
    like "[Unsorted]".
    """
    name = folder_name
    year: int | None = None
    ym = _FOLDER_YEAR_RE.search(name)
    if ym:
        year = int(ym.group("year"))

    name = _LEADING_BRACKET_RE.sub("", name)      # drop leading [group]/[dates]
    name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)  # drop remaining bracket groups
    name = name.replace("_", " ").replace(".", " ")
    name = _JUNK_CODEC_TOKEN_RE.sub(" ", name)    # "HEVCx265"-style concatenations
    name = _JUNK_WORD_RE.sub(" ", name)
    # A season/part marker ends the title portion ("Title S2", "Title Season 3").
    name = re.split(r"\b(?:S\d{1,2}\b|Season\b|Part\b|Cour\b)", name, maxsplit=1,
                    flags=re.IGNORECASE)[0]
    name = re.sub(r"[\s\-–~]+$", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -–_~")
    return name, year


def _score_candidates(query: str, results: list[MediaResult], year: int | None) -> tuple[MediaResult | None, float]:
    best: MediaResult | None = None
    best_score = 0.0
    for r in results:
        score = best_title_similarity(query, r)  # matches romaji OR english
        if year and r.year and year == r.year:
            score = min(1.0, score + 0.1)
        if score > best_score:
            best, best_score = r, score
    return best, best_score


def _identify_folder(folder_name: str, media_type: str) -> MediaResult | None:
    name, year = clean_show_folder_name(folder_name)
    if len(name) < 2:
        return None  # nothing searchable survived (e.g. "[Unsorted]")

    if media_type == "tv":
        # Gather from BOTH sources and pick the best — a wrong-but-nonempty
        # TVDB result must not block the correct TMDB one (old `or` bug).
        candidates = search_tvdb_shows(name, year) + search_tmdb_shows(name, year)
        best, score = _score_candidates(name, candidates, year)
    elif media_type in ("anime", "xanime"):
        best, score = _best_anime_match(name, year, explicit=(media_type == "xanime"))
    else:
        return None

    return best if best is not None and score >= _IDENTIFY_THRESHOLD else None


# Above this, take a source's match without consulting slower/less-reliable
# sources — most folders resolve on the offline AniDB dump alone.
_HIGH_CONFIDENCE = 0.90


def _best_anime_match(name: str, year: int | None, *, explicit: bool
                      ) -> tuple[MediaResult | None, float]:
    """Cascade anime identification across sources, cheapest/most-reliable
    first, stopping as soon as a confident match appears.

    Order: AniDB offline dump (instant, no network, best romaji coverage) →
    AniList (reliable live API) → Jikan (only if its breaker isn't open) →
    TMDB (English-biased, regular anime only). This makes a full scan fast —
    most folders never hit the network — and resilient to any one API being
    down.
    """
    best: MediaResult | None = None
    best_score = 0.0

    def consider(candidates: list[MediaResult]) -> bool:
        nonlocal best, best_score
        b, s = _score_candidates(name, candidates, year)
        if s > best_score:
            best, best_score = b, s
        return best_score >= _HIGH_CONFIDENCE

    # 1. Offline AniDB dump — the workhorse. Covers most folders with no API call.
    if consider(search_anidb(name, media_type="xanime" if explicit else "anime")):
        return best, best_score
    # 2. AniList — reliable live source, rich romaji/synonym titles.
    if consider(search_anilist(name, explicit=explicit)):
        return best, best_score
    # 3. Jikan — skip entirely when its circuit breaker says it's down.
    if not jikan_circuit_open() and consider(search_jikan_anime(name, explicit=explicit)):
        return best, best_score
    # 4. TMDB — last resort; it carries almost no hentai, so regular anime only.
    if not explicit:
        consider(search_tmdb_anime(name, year))
    return best, best_score


def scan_library_folders(media_types: tuple[str, ...] = ("tv", "anime", "xanime")) -> ScanResult:
    """Identify and track every unmapped show folder under the typed roots.

    Guarded: only one scan/sync runs at a time (see run_exclusive)."""
    return run_exclusive("Scan Folders", lambda: _scan_library_folders_impl(media_types))


def _scan_library_folders_impl(media_types: tuple[str, ...]) -> ScanResult:
    shows_store.initialize_shows_db()
    identified = already = 0
    unidentified: list[str] = []

    for media_type in media_types:
        for root in _typed_roots(media_type):
            try:
                subdirs = sorted(d for d in root.iterdir() if d.is_dir())
            except OSError as exc:
                logger.warning("Cannot scan %s: %s", root, exc)
                continue
            for folder in subdirs:
                if shows_store.folder_mapped(str(folder)):
                    already += 1
                    continue
                match = _identify_folder(folder.name, media_type)
                if match is None:
                    unidentified.append(str(folder))
                    logger.info("Could not identify show folder: %s", folder)
                    continue
                show_id = shows_store.upsert_show(
                    title=match.title, media_type=media_type,
                    source=match.source, external_id=match.external_id,
                    external_url=match.external_url or None, year=match.year,
                )
                shows_store.add_show_folder(show_id, str(folder))
                identified += 1
                logger.info(
                    "Tracked '%s' (%s:%s) ← %s",
                    match.title, match.source, match.external_id, folder,
                )

    _save_unidentified(unidentified)
    return ScanResult(identified=identified, already_tracked=already,
                      unidentified=unidentified)


# Folders the scanner couldn't identify — persisted so the Shows tab can
# offer them for manual identification (they used to hide in the log).
_UNIDENTIFIED_FILE = "unidentified_folders.json"


def _save_unidentified(folders: list[str]) -> None:
    import json
    try:
        (Path(config.APP_DIR) / _UNIDENTIFIED_FILE).write_text(
            json.dumps(folders, indent=1), encoding="utf-8")
    except OSError:
        logger.debug("Could not persist unidentified-folder list.", exc_info=True)


def load_unidentified() -> list[str]:
    """Unidentified folders from the most recent scan (folders that have
    since been mapped — e.g. identified by hand — are filtered out)."""
    import json
    try:
        raw = json.loads((Path(config.APP_DIR) / _UNIDENTIFIED_FILE)
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [f for f in raw if isinstance(f, str)
            and Path(f).is_dir() and not shows_store.folder_mapped(f)]


# ---------------------------------------------------------------------------
# Episode sync
# ---------------------------------------------------------------------------

# source → (episode fetcher, status fetcher). Tests monkeypatch this dict.
EPISODE_FETCHERS: dict[str, tuple] = {
    "tvdb": (get_tvdb_episodes, get_tvdb_series_status),
    "tmdb": (get_tmdb_tv_episodes, get_tmdb_tv_status),
    "jikan": (get_jikan_episodes, get_jikan_status),
}


def _parse_episode_from_file(name: str) -> tuple[int | None, int] | None:
    """(season_or_None, episode) from a filename.

    season is None when the filename itself carries no season token — the
    caller then derives it from the parent "Season NN" folder (files like
    "Episode 05 - Title.mp4" live inside per-season folders; assuming season
    1 for them was the bug that marked whole shows as missing when their
    seasons were organised in folders)."""
    stem = Path(name).stem
    # OVA/OAD/creditless files are specials — pin them to season 0 so
    # "Show OVA - 01" can never shadow the real S01E01.
    special = _SPECIAL_MARKER_RE.search(stem) is not None
    default_season = 0 if special else None

    parsed = parse_torrent_name(name)
    if parsed.episode is not None:
        return (parsed.season if parsed.season is not None else default_season,
                parsed.episode)

    m = _EP_WORD_RE.search(stem)
    if m:
        return (default_season, int(m.group(1)))
    # Bare "E01" marker ("[Ranger] Medaka Box - E01 [BD].mkv").
    m = _BARE_E_RE.search(re.sub(r"\[[^\]]*\]", " ", stem))
    if m:
        return (default_season, int(m.group(1)))
    # Leading track-style numbering ("01) Great Sword.mkv").
    m = _LEADING_NUM_RE.match(stem)
    if m and not _NOT_EPISODE.match(m.group(1)):
        return (default_season, int(m.group(1)))

    # Fallback: strip bracket groups, then take the LAST plausible number.
    cleaned = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
    candidates = [
        tok for tok in _ABS_EP_RE.findall(cleaned)
        if not _NOT_EPISODE.match(tok)
    ]
    if candidates:
        return (default_season, int(candidates[-1]))
    if special:
        return (0, 1)  # lone unnumbered OVA
    return None


def _season_from_parents(file_path: Path, root: Path) -> int | None:
    """Season number from the nearest ancestor "Season NN"/"SNN"/"Specials"
    folder between the file and the show root (root itself excluded)."""
    for parent in file_path.parents:
        if parent == root:
            break
        name = parent.name.strip()
        m = _SEASON_DIR_RE.search(name)
        if m:
            return int(m.group(1))
        if _SPECIALS_DIR_RE.match(name):
            return 0
    return None


def _first_video_file(folders: tuple[str, ...]) -> str | None:
    """First non-extras video under the mapped folders (largest first, so a
    sample never wins over the actual episode)."""
    candidates: list[tuple[int, str]] = []
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if (f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
                    and not _in_extras_dir(f, root)):
                try:
                    candidates.append((f.stat().st_size, str(f)))
                except OSError:
                    continue
    return max(candidates)[1] if candidates else None


def _in_extras_dir(file_path: Path, root: Path) -> bool:
    """True when any ancestor between the file and the show root is a bonus
    folder (Trailers, NCOP/NCED, Other, …) — those aren't episodes."""
    for parent in file_path.parents:
        if parent == root:
            return False
        if _EXTRAS_DIR_RE.match(parent.name.strip()):
            return True
    return False


def _scan_folders_for_episodes(folders: tuple[str, ...]) -> dict[tuple[int, int], str]:
    found: dict[tuple[int, int], str] = {}
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if _in_extras_dir(f, root):
                continue
            key = _parse_episode_from_file(f.name)
            if key is None:
                continue
            season, episode = key
            if season is None:
                season = _season_from_parents(f, root)
            if season is None:
                season = 1  # flat folder, absolute numbering
            found.setdefault((season, episode), str(f))
    return found


def _remap_disk_seasons_to_tracker(
    found: dict[tuple[int, int], str], episodes: list,
) -> tuple[dict[tuple[int, int], str], bool]:
    """Bridge the OTHER ordering mismatch: the tracker numbers everything as
    one continuous season ("S1E13-24") while the disk splits it into Season
    folders ("Season 02\\...E01-12") — common when TMDB merges anime cours.

    Only fires for disk seasons the tracker doesn't know AT ALL, and only
    maps onto tracker rows that actually exist and aren't already matched —
    so shows with genuine tracker seasons are untouched. Returns the
    (possibly) remapped dict and whether anything moved.
    """
    tracker_keys = {(e.season, e.episode) for e in episodes}
    tracker_seasons = {s for s, _e in tracker_keys}
    disk_seasons = sorted({s for s, _e in found if s > 0})
    orphan_seasons = [s for s in disk_seasons if s >= 2 and s not in tracker_seasons]
    if not orphan_seasons:
        return found, False

    disk_max = {s: max(e for (ss, e) in found if ss == s) for s in disk_seasons}
    remapped = dict(found)
    moved = False
    for season in orphan_seasons:
        offset = sum(disk_max[s] for s in disk_seasons if s < season)
        for (ss, ep), path in found.items():
            if ss != season or ep < 1:
                continue
            target = (1, offset + ep)
            if target in tracker_keys and target not in remapped:
                remapped[target] = path
                remapped.pop((ss, ep), None)
                moved = True
    return remapped, moved


def _ensure_tmdb_id(show: shows_store.TrackedShow) -> str | None:
    """Return (and persist) a TMDB id for a show — its own id if TMDB-sourced,
    else an anime-aware resolution by title. Used for the episode-list and
    airing fallbacks that non-TMDB sources (anidb/anilist/jikan/tvdb) rely on."""
    if show.tmdb_id:
        return show.tmdb_id
    tmdb_id = show.external_id if show.source == "tmdb" else resolve_tmdb_tv_id(
        show.title, show.year, prefer_anime=show.media_type in ("anime", "xanime"),
    )
    if tmdb_id:
        shows_store.set_show_tmdb_id(show.show_id, tmdb_id)
    return tmdb_id


def sync_show(show_id: int) -> str:
    """Refresh one show's episode list, on-disk state, and airing schedule.

    Guarded: reentrant, so sync_all()/auto-grab may call it while holding the
    lock, but a fresh concurrent click is rejected with ShowsBusyError."""
    return run_exclusive("Sync show", lambda: _sync_show_impl(show_id))


def _sync_show_impl(show_id: int) -> str:
    show = shows_store.get_show(show_id)
    if show is None:
        return f"show #{show_id} not found"

    fetchers = EPISODE_FETCHERS.get(show.source)
    episodes: list[EpisodeInfo] = []
    status = ""
    if fetchers is not None:
        fetch_episodes, fetch_status = fetchers
        episodes = fetch_episodes(show.external_id)
        status = fetch_status(show.external_id)

    # Airing + status for anime come from AniList by title (the most accurate
    # anime airing source), covering every anime regardless of how it was
    # identified — most are AniDB-identified, which has no airing of its own.
    # xanime is skipped (hentai has no meaningful airing schedule and it's the
    # bulk of the extra API calls). Shows already known to be finished are
    # skipped on a re-sync (nothing new airs) so re-syncs stay fast; the first
    # sync checks everything (status still blank).
    already_finished = show.status in ("Ended", "Cancelled") and show.last_synced
    nxt: EpisodeInfo | None = None
    if show.media_type == "anime" and not already_finished:
        nxt, anime_status = get_anime_airing(show.title)
        if anime_status and not status:
            status = anime_status
    if status:
        shows_store.set_show_status(show_id, status)

    # Episode-list fallback: anidb/anilist have no native episode API, and
    # Jikan may be down — fall back to TMDB's episode list so missing-episode
    # detection still works. TMDB is also where TV airing comes from.
    refreshed = shows_store.get_show(show_id) or show
    tmdb_id = _ensure_tmdb_id(refreshed)
    if not episodes and tmdb_id:
        episodes = get_tmdb_tv_episodes(tmdb_id)

    # Ordering mismatch: many anime trackers number episodes absolutely
    # (one long "season 1"), but Plex libraries are organised in Season NN
    # folders. If the fetched list is single-season while the disk clearly
    # uses multiple seasons, swap to TMDB's season-based list — otherwise
    # every episode outside "Season 1" reads as missing.
    found = _scan_folders_for_episodes(show.folders)
    ordering_note = ""
    disk_max_season = max((season for season, _ep in found), default=0)
    fetched_max_season = max((e.season for e in episodes), default=0)
    if (episodes and fetched_max_season <= 1 and disk_max_season >= 2
            and show.media_type in ("anime", "xanime") and tmdb_id):
        seasoned = get_tmdb_tv_episodes(tmdb_id)
        if seasoned and max(e.season for e in seasoned) >= 2:
            logger.info(
                "'%s': tracker list is absolute-ordered but disk has %d seasons — "
                "switching to TMDB season ordering.", show.title, disk_max_season,
            )
            shows_store.clear_episodes(show_id)
            episodes = seasoned
            ordering_note = " (switched to season ordering via TMDB)"

    if episodes:
        shows_store.replace_episodes(show_id, episodes)

    # TV airing (and anime not found on AniList) via TMDB's next_episode_to_air.
    if nxt is None and tmdb_id:
        nxt = get_tmdb_next_air(tmdb_id)
    shows_store.set_show_airing(
        show_id,
        next_air_date=nxt.air_date if nxt else None,
        next_season=nxt.season if nxt else None,
        next_episode=nxt.episode if nxt else None,
    )

    # Reverse mismatch: tracker numbers continuously, disk uses Season
    # folders — map "Season 02/E05" onto the tracker's S1E17 etc.
    if episodes:
        found, remapped = _remap_disk_seasons_to_tracker(found, episodes)
        if remapped:
            ordering_note += " (Season folders matched to the tracker's continuous numbering)"

    # One-episode shows (most hentai, many OVAs) often ship a single file
    # with no number at all — a lone video IS episode 1.
    if not found and sum(1 for e in episodes if e.season > 0) == 1:
        lone = _first_video_file(show.folders)
        if lone is not None:
            found[(1, 1)] = lone

    shows_store.update_file_state(show_id, found)

    # Report the SAME numbers the tracked-shows table shows (regular seasons
    # only) — quoting the raw fetch/scan counts here used to disagree with
    # the row whenever specials (season 0) were involved.
    final = shows_store.get_show(show_id)
    have = final.have_count if final else 0
    known = final.episode_count if final else len(episodes)
    missing = final.missing_count if final else 0
    specials_on_disk = sum(1 for season, _ep in found if season == 0)
    specials_note = f" (+{specials_on_disk} specials on disk)" if specials_on_disk else ""

    air_note = f", next airs {nxt.air_date} (S{nxt.season:02d}E{nxt.episode:02d})" if nxt else ""
    return (f"{show.title}: {have}/{known} on disk, {missing} missing"
            f"{specials_note}{air_note}{ordering_note}")


def sync_all(progress=None) -> list[str]:
    """Sync every tracked show. Guarded; runs shows serially (holds the lock
    the whole time) so it never overlaps another scan/sync.

    progress(done, total, title), when given, is called before each show —
    the Shows tab uses it for its status line + time-remaining estimate."""
    def impl() -> list[str]:
        shows = shows_store.list_shows()
        results: list[str] = []
        for i, s in enumerate(shows):
            if progress is not None:
                try:
                    progress(i, len(shows), s.title)
                except Exception:
                    pass
            results.append(_sync_show_impl(s.show_id))
        return results

    return run_exclusive("Sync all", impl)


def backfill_english_titles() -> int:
    """Rename AniDB-identified shows to their official English title.

    The AniDB dump stores a romaji "main" title (why 'Witch Hat Atelier'
    was tracked as 'Tongari Boushi no Atelier'); this swaps in the official
    English title where the dump has one. Returns the number renamed."""
    from media_lookup import anidb_english_title
    renamed = 0
    for show in shows_store.list_shows():
        if show.source != "anidb":
            continue
        english = anidb_english_title(show.external_id)
        if english and english.strip().casefold() != show.title.strip().casefold():
            shows_store.rename_show(show.show_id, english.strip())
            logger.info("Renamed '%s' → '%s' (AniDB %s)", show.title, english, show.external_id)
            renamed += 1
    return renamed


# ---------------------------------------------------------------------------
# Deterministic routing for tracked episodes (feeds the download pipeline)
# ---------------------------------------------------------------------------

def _folder_containing_season(show: shows_store.TrackedShow, season: int) -> str | None:
    """The mapped folder that already holds this season's subfolder, if any."""
    season_re = re.compile(rf"^(?:Season[\s._-]*0*{season}|S0*{season})$", re.IGNORECASE)
    for folder in show.folders:
        root = Path(folder)
        if not root.is_dir():
            continue
        try:
            if any(d.is_dir() and season_re.match(d.name.strip()) for d in root.iterdir()):
                return folder
        except OSError:
            continue
    return None


def plan_for_episode(
    show: shows_store.TrackedShow, season: int, episode: int,
) -> torrent_routing.RoutePlan:
    """Route plan for a KNOWN episode of a tracked show — no fuzzy matching.

    Precedence for the destination:
      1. An explicit per-season target folder (season_targets) — the file
         lands directly in that folder.
      2. The mapped folder that already contains this season, keeping its
         season-subfolder naming style.
      3. The show's first mapped folder, creating "Season NN" per the
         sibling style.
    Only a show with no mapped folders at all falls back to staging.
    """
    new_filename = torrent_routing.sanitize_for_filesystem(
        f"{show.title} - S{season:02d}E{episode:02d}"
    )

    target = shows_store.get_season_target(show.show_id, season)
    if target:
        return torrent_routing.RoutePlan(
            confident=True, dest_dir=target, new_filename=new_filename,
            season_folder=None, show_folder=None,
            reason=f"season target rule for '{show.title}' S{season}",
        )

    # No folder holds this season yet (e.g. a brand-new season): create it
    # in the mapped folder on the drive with the most free space.
    folder = _folder_containing_season(show, season) or (
        torrent_routing.pick_root_by_free_space(list(show.folders))
        or (show.folders[0] if show.folders else None)
    )
    if folder is None:
        return torrent_routing.RoutePlan(
            confident=False, dest_dir=str(Path(config.TORRENT_DOWNLOAD_DIR)),
            reason=f"'{show.title}' has no mapped folders — staying in staging",
        )

    season_name = torrent_routing._season_folder_name(Path(folder), season)
    return torrent_routing.RoutePlan(
        confident=True,
        dest_dir=str(Path(folder) / season_name),
        new_filename=new_filename,
        show_folder=folder, season_folder=season_name,
        reason=f"tracked show '{show.title}' → {Path(folder).name}/{season_name}",
    )
