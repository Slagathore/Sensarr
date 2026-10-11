# =============================================================================
# presence_ledger.py — what we actually HAVE, decided at the moment it lands
# =============================================================================
# The tracker's episode grid is the master list of what is on disk. Everything
# downstream reads it: the grabber decides what to search for, requests decide
# when they are finished, and the user asks it what state their request is in.
# So the moment a download's files are placed, three things must happen, and
# until now only the first happened, and only sometimes:
#
#   1. REGISTER  every episode the placement supplied, into the grid. A season
#      pack registers every episode inside it, one row per episode.
#   2. REAP      every other download still in flight that this placement just
#      made pointless — never itself, never anything already finished.
#   3. SETTLE    the request: a movie is done on one file; a season is done
#      when its aired episode set is complete.
#
# Why this module exists: the old code did (1) only when the download row
# carried a show_id, and (3) only when it carried a request_id. A pack grabbed
# by hand has neither. Widow's Bay S01 landed complete on disk on 3 Sep and
# nothing recorded it, so the grid still read 0/10, the request never settled,
# and the grabber re-queued the same pack every five minutes for two hours and
# then queued all ten episodes individually. Registration now depends on the
# FILES, not on how the download happened to be started.
#
# Pure-ish: reads and writes the stores, never the network, never config.
# =============================================================================

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import downloads_store
import shows_store

logger = logging.getLogger(__name__)

# A placement only counts as evidence when verification agreed. 'duplicate'
# counts too: the file is on disk, it just arrived twice.
PLACED_STATES = ("verified", "duplicate")

# Download states whose bytes are still being fetched, i.e. rows a placement
# can make redundant. Anything else is already finished or already dead.
IN_FLIGHT = ("queued", "downloading", "seeding", "verifying", "downloaded")

_SXXEYY_RE = re.compile(r"(?<![0-9a-z])s(\d{1,3})[\s._-]*e(\d{1,4})",
                        re.IGNORECASE)


@dataclass(frozen=True)
class Registration:
    """What one placement put on the master list."""
    download_id: int
    show_id: Optional[int] = None
    season: Optional[int] = None
    episodes: tuple = ()             # tuple[int, ...] episode numbers recorded
    movie_path: Optional[str] = None
    detail: str = ""

    @property
    def recorded_anything(self) -> bool:
        return bool(self.episodes) or self.movie_path is not None


@dataclass
class RequestFile:
    """One concrete file (or expected file) behind a request."""
    season: Optional[int]
    episode: Optional[int]
    title: str
    state: str                # on-disk | downloading | queued | failed | missing
    detail: str = ""
    path: Optional[str] = None
    progress: float = 0.0


@dataclass
class RequestReport:
    """Exactly where a request stands, down to the last file."""
    request_id: int
    title: str
    media_type: str
    status: str
    season: Optional[int]
    files: list = field(default_factory=list)   # list[RequestFile]
    summary: str = ""

    @property
    def counts(self) -> dict:
        out: dict = {}
        for entry in self.files:
            out[entry.state] = out.get(entry.state, 0) + 1
        return out


# ---------------------------------------------------------------------------
# 1. REGISTER
# ---------------------------------------------------------------------------

def _season_episode_from_path(path: str) -> tuple[Optional[int], Optional[int]]:
    """(season, episode) parsed from a placed file's name, or (None, None)."""
    match = _SXXEYY_RE.search(Path(path or "").name)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None, None


def placed_episodes(download_id: int) -> list[tuple[int, int, str]]:
    """(season, episode, final_path) for every verified file of a download.

    Season/episode come from the recorded per-file parse where it exists and
    are re-derived from the placed filename when it does not, so a pack whose
    files were named at move time still yields its episode list.
    """
    out: list[tuple[int, int, str]] = []
    for entry in downloads_store.list_download_files(download_id):
        if entry.verification_state not in PLACED_STATES:
            continue
        if not entry.final_path:
            continue
        season, episode = entry.parsed_season, entry.parsed_episode
        if episode is None:
            season, episode = _season_episode_from_path(entry.final_path)
        if episode is None:
            continue
        season = int(season) if season is not None else 1
        out.append((season, int(episode), entry.final_path))
        # A multi-episode file ("Max & Ruby - S01E04-E06") covers every
        # episode in its range, not just the first.
        import torrent_routing
        parsed = torrent_routing.parse_torrent_name(Path(entry.final_path).name)
        if (parsed.last_episode is not None and parsed.episode == int(episode)
                and parsed.last_episode - int(episode) <= 10):
            out.extend((season, e, entry.final_path)
                       for e in range(int(episode) + 1, parsed.last_episode + 1))
    return out


def resolve_show_id(row, *, resolve_show: Optional[Callable] = None
                    ) -> Optional[int]:
    """The tracked show a placement belongs to.

    row.show_id when the grab carried show context; otherwise the caller's
    resolver gets a chance (identity first, then fuzzy title). A manual grab
    has no context at all, which is exactly the case that used to register
    nothing.
    """
    if getattr(row, "show_id", None) is not None:
        return int(row.show_id)
    if resolve_show is None:
        return None
    try:
        show = resolve_show(row)
    except Exception:
        logger.exception("Show resolver failed for download #%s",
                         getattr(row, "download_id", "?"))
        return None
    return int(show.show_id) if show is not None else None


def register_download(row, *, resolve_show: Optional[Callable] = None
                      ) -> Registration:
    """Record everything a placed download supplied, on the master list.

    Works with no request_id and no show_id — the placed FILES are the
    evidence. Never raises: a bookkeeping failure must not fail a move that
    already happened.
    """
    download_id = int(getattr(row, "download_id", 0) or 0)
    media_type = getattr(row, "media_type", "") or ""

    if media_type == "movie":
        path = next((f.final_path for f in
                     downloads_store.list_download_files(download_id)
                     if f.verification_state in PLACED_STATES and f.final_path),
                    None)
        return Registration(download_id=download_id, movie_path=path,
                            detail="movie placement" if path else "no placed file")

    episodes = placed_episodes(download_id)
    if not episodes:
        return Registration(download_id=download_id,
                            detail="no episode files to register")

    show_id = resolve_show_id(row, resolve_show=resolve_show)
    if show_id is None:
        return Registration(
            download_id=download_id, episodes=tuple(e for _s, e, _p in episodes),
            detail=(f"{len(episodes)} episode file(s) placed but no tracked "
                    f"show to record them against"))

    recorded: list[int] = []
    seasons: set[int] = set()
    for season, episode, path in episodes:
        try:
            shows_store.set_episode_file(show_id, season, episode, path)
            recorded.append(episode)
            seasons.add(season)
        except Exception:
            logger.exception("Could not record S%02dE%02d for show %s",
                             season, episode, show_id)
    season = next(iter(seasons)) if len(seasons) == 1 else None
    if recorded:
        logger.info("Presence: download #%s put %d episode(s) on the master "
                    "list for show %s (%s).", download_id, len(recorded),
                    show_id, ", ".join(f"S{s:02d}E{e:02d}"
                                       for s, e, _p in episodes[:6]))
    return Registration(download_id=download_id, show_id=show_id, season=season,
                        episodes=tuple(recorded),
                        detail=f"{len(recorded)} episode(s) recorded")


# ---------------------------------------------------------------------------
# 2. REAP
# ---------------------------------------------------------------------------

def redundant_downloads(row, registration: Registration) -> list:
    """In-flight downloads this placement just made pointless.

    A row qualifies when it is fetching an episode that is now on disk, or a
    season pack for a season that is now complete. The placed download itself
    is excluded explicitly and unconditionally — a reaper that can target its
    own placement would delete the thing it just finished.
    """
    placed_id = int(getattr(row, "download_id", 0) or 0)
    if registration.show_id is None or not registration.episodes:
        return []

    have = {int(e) for e in registration.episodes}
    season = registration.season
    grid = [e for e in shows_store.list_episodes(registration.show_id)
            if season is None or e.season == season]
    season_complete = bool(grid) and all(e.has_file for e in grid)

    victims = []
    for candidate in downloads_store.list_downloads_by_status(IN_FLIGHT):
        if candidate.download_id == placed_id:
            continue                      # never itself
        if candidate.removed_at is not None:
            continue
        if candidate.show_id != registration.show_id:
            continue
        if season is not None and candidate.season != season:
            continue
        if candidate.episode is not None:
            if int(candidate.episode) in have:
                victims.append(candidate)
        elif season_complete:
            # A pack for a season we now hold in full.
            victims.append(candidate)
    return victims


def reap_redundant(row, registration: Registration) -> list[int]:
    """Cancel and archive the downloads redundant_downloads() identifies.

    Returns the ids reaped. Safe to call from a worker thread; never raises.
    """
    reaped: list[int] = []
    try:
        victims = redundant_downloads(row, registration)
    except Exception:
        logger.exception("Redundant-download scan failed after download #%s",
                         getattr(row, "download_id", "?"))
        return reaped

    placed_id = int(getattr(row, "download_id", 0) or 0)
    for victim in victims:
        if victim.download_id == placed_id:      # belt and braces
            continue
        try:
            downloads_store.set_status(victim.download_id, "cancelled")
            downloads_store.tombstone_download(
                victim.download_id,
                reason=(f"redundant: download #{placed_id} already supplied "
                        f"these episodes"))
            downloads_store.add_history(
                victim.download_id, "cancelled", before=None,
                after=f"redundant after download #{placed_id} was placed")
            reaped.append(victim.download_id)
        except Exception:
            logger.exception("Could not reap redundant download #%s",
                             victim.download_id)
    if reaped:
        logger.info("Presence: download #%s made %d in-flight download(s) "
                    "redundant; cancelled %s.", placed_id, len(reaped), reaped)
    return reaped


# ---------------------------------------------------------------------------
# 3. REPORT — the exact state of a request, down to the last file
# ---------------------------------------------------------------------------

def _download_state(status: str) -> str:
    if status in ("moved", "completed"):
        return "on-disk"
    if status in ("downloading", "seeding", "verifying", "downloaded"):
        return "downloading"
    if status == "queued":
        return "queued"
    return "failed"


def request_report(request_id: int, *, get_request: Callable,
                   resolve_show: Optional[Callable] = None) -> Optional[RequestReport]:
    """Everything known about one request, file by file.

    `get_request` is injected so this module never imports the queue store's
    request-lookup chain (and so tests can hand in a stub).
    """
    req = get_request(request_id)
    if req is None:
        return None

    title = getattr(req, "resolved_title", None) or getattr(req, "content", "")
    report = RequestReport(
        request_id=request_id, title=title,
        media_type=getattr(req, "media_type", "unknown"),
        status=getattr(req, "status", "unknown"),
        season=getattr(req, "season", None))

    downloads = downloads_store.downloads_for_request(request_id,
                                                     include_removed=False)
    # --- movies and one-offs: the files of every linked download -------------
    if report.media_type in ("movie", "other", "unknown"):
        for dl in downloads:
            files = [f for f in downloads_store.list_download_files(dl.download_id)
                     if f.final_path or f.verification_state]
            if not files:
                report.files.append(RequestFile(
                    season=None, episode=None, title=dl.title,
                    state=_download_state(dl.status),
                    detail=dl.error or dl.status, progress=dl.progress))
                continue
            for f in files:
                report.files.append(RequestFile(
                    season=None, episode=None,
                    title=Path(f.final_path).name if f.final_path else dl.title,
                    state=("on-disk" if f.verification_state in PLACED_STATES
                           and f.final_path else _download_state(dl.status)),
                    detail=f.verification_reason or f.verification_state or "",
                    path=f.final_path, progress=dl.progress))
        report.summary = _summarize(report)
        return report

    # --- shows: the aired episode set is the yardstick -----------------------
    show = None
    if resolve_show is not None:
        try:
            show = resolve_show(req)
        except Exception:
            logger.exception("Show resolver failed for request #%s", request_id)
    in_flight: dict[tuple, object] = {}
    for dl in downloads:
        if dl.season is None:
            continue
        in_flight[(dl.season, dl.episode)] = dl

    if show is None:
        for dl in downloads:
            report.files.append(RequestFile(
                season=dl.season, episode=dl.episode, title=dl.title,
                state=_download_state(dl.status),
                detail=dl.error or dl.status, progress=dl.progress))
        report.summary = _summarize(report)
        return report

    from datetime import date
    today = date.today().isoformat()
    for ep in shows_store.list_episodes(show.show_id):
        if report.season is not None and ep.season != report.season:
            continue
        if ep.air_date and ep.air_date > today:
            state, detail = "unaired", f"airs {ep.air_date}"
        elif ep.has_file:
            state, detail = "on-disk", ""
        else:
            dl = in_flight.get((ep.season, ep.episode)) or in_flight.get(
                (ep.season, None))
            if dl is not None:
                state = _download_state(dl.status)
                detail = dl.error or dl.title
            else:
                state, detail = "missing", "not grabbed yet"
        report.files.append(RequestFile(
            season=ep.season, episode=ep.episode,
            title=getattr(ep, "title", "") or f"S{ep.season:02d}E{ep.episode:02d}",
            state=state, detail=detail,
            path=getattr(ep, "file_path", None),
            progress=(in_flight[(ep.season, ep.episode)].progress
                      if (ep.season, ep.episode) in in_flight else 0.0)))

    report.summary = _summarize(report)
    return report


def _summarize(report: RequestReport) -> str:
    counts = report.counts
    if not counts:
        return "nothing recorded yet"
    order = ("on-disk", "downloading", "queued", "missing", "unaired", "failed")
    parts = [f"{counts[state]} {state}" for state in order if counts.get(state)]
    total = sum(counts.values())
    return f"{', '.join(parts)} of {total}"


_STATE_MARK = {
    "on-disk": "have",
    "downloading": "getting",
    "queued": "queued",
    "missing": "missing",
    "unaired": "not out",
    "failed": "failed",
}


def format_report(report: Optional[RequestReport], *, limit: int = 60) -> str:
    """The report as plain text for the bot and the desktop detail view.

    Deliberately states every file rather than a rounded-up count: "3 of 8"
    hides which three, and that is exactly what someone asking about their
    request wants to know.
    """
    if report is None:
        return "No such request."

    season = f" season {report.season}" if report.season is not None else ""
    lines = [f"Request #{report.request_id}: {report.title}{season}",
             f"State: {report.status} ({report.summary})", ""]

    for entry in report.files[:limit]:
        mark = _STATE_MARK.get(entry.state, entry.state)
        if entry.season is not None and entry.episode is not None:
            label = f"S{entry.season:02d}E{entry.episode:02d}"
            name = f" {entry.title}" if entry.title else ""
        else:
            label = ""
            name = f" {entry.title}" if entry.title else ""
        progress = ""
        if entry.state == "downloading" and entry.progress:
            progress = f" {entry.progress:.0f}%"
        detail = f" ({entry.detail})" if entry.detail else ""
        lines.append(f"  [{mark}] {label}{name}{progress}{detail}".rstrip())

    if len(report.files) > limit:
        lines.append(f"  ... and {len(report.files) - limit} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 0. GUARD — the same bytes are never fetched twice, whatever the media type
# ---------------------------------------------------------------------------

_BTIH_RE = re.compile(r"btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)


def infohash_of(magnet: str) -> str:
    """Lowercased btih info-hash from a magnet URI, or ''."""
    match = _BTIH_RE.search(magnet or "")
    return match.group(1).lower() if match else ""


def live_downloads_for(magnet: str) -> list:
    """Every live download fetching this info-hash (a pack can be fetched as
    several single-file downloads at once)."""
    wanted = infohash_of(magnet)
    if not wanted:
        return []
    return [c for c in downloads_store.list_downloads_by_status(IN_FLIGHT)
            if c.removed_at is None and infohash_of(c.magnet) == wanted]


def live_download_for(magnet: str, *, exclude: Optional[int] = None):
    """A download already fetching these exact bytes, or None.

    Identity here is the info-hash, not the title and not the media type: the
    same hash IS the same files. The reaper only understands shows, so two
    live grabs of one movie slipped past it — a hand-grabbed Gulliver's
    Travels and an auto-grab for the request both sat at 0.7% for ten hours,
    splitting an already-thin swarm between them.
    """
    wanted = infohash_of(magnet)
    if not wanted:
        return None
    for candidate in downloads_store.list_downloads_by_status(IN_FLIGHT):
        if candidate.removed_at is not None:
            continue
        if exclude is not None and candidate.download_id == exclude:
            continue
        if infohash_of(candidate.magnet) == wanted:
            return candidate
    return None
