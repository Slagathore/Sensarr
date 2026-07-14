# =============================================================================
# torrent_select.py  —  the deterministic torrent selection engine (Task B)
# =============================================================================
# PURE module: no I/O, no config import, no database, no network. Everything a
# decision needs is passed in. This is the single place that answers "given a
# pool of candidate releases and an immutable want, which one do we grab, and
# WHY did every other one lose?".
#
# The LLM is deliberately absent (Design stance 1). Selection is a fixed
# pipeline: eight hard gates in order, then a versioned score over the
# survivors, then deterministic tie-breaks. Every rejection is reason-coded and
# every survivor carries a component-by-component score breakdown, so a
# SelectionDecision can be persisted and shown to Cole in the grab queue.
#
# Phase 1 scope: this engine and the pool collector exist and are fully tested,
# but NOTHING automatic calls them yet. download_manager's auto-grab paths are
# rewired in Phase 3; the legacy pick_best_result / filter_viable_results stay
# alive until then. note: deferred to Phase 3 (wiring).
#
# API fact (Phase 0 spike, tests/test_rtn_spike.py): RTN 1.11.1 has NO
# check_trash function. The trash contract is ParsedData.trash + check_fetch +
# GarbageTorrent via rank(remove_trash=True). We read ParsedData.trash directly
# for the CAM/TS gate and rank(remove_trash=False) for the quality score (which
# never raises, so an allow_cam release still scores instead of exploding).
# =============================================================================

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import re
from dataclasses import dataclass
from importlib import metadata
from typing import Callable, Iterable, Optional

import RTN
from RTN import DefaultRanking, SettingsModel, parse, title_match

import media_identity
from media_identity import MediaIdentity

# ---------------------------------------------------------------------------
# Versioned score profile — plexxarr-v1
# ---------------------------------------------------------------------------
# The COMPONENT SCALE RELATIONSHIPS are binding (section 4 item 3): no component
# may silently dwarf the others. Exact point values are tunable against the
# golden corpus; these are the shipped v1 values.
PROFILE = "plexxarr-v1"

# rtn_quality: RTN integer rank clamped to this window, linearly mapped to 0-120.
_RANK_FLOOR = -2000.0
_RANK_CEIL = 4000.0
_RTN_QUALITY_MAX = 120.0

_SIZE_CLOSENESS_MAX = 35.0     # 0-35, peaks when size == target
_SEED_HEALTH_MAX = 8.0         # 0-8, log-scaled
_SEASON_PACK_BONUS = 15.0      # +15 for a pack on a whole-season want
_COUNTRY_ALIAS_BONUS = 20.0    # +20 on a POSITIVE country/alias match
_RECENT_FAILURE_PENALTY = -25.0  # replaces _prefer_unfailed's pre-sort reorder


def rtn_version() -> str:
    """The pinned RTN release, recorded on every decision for provenance."""
    try:
        return metadata.version("rank-torrent-name")
    except Exception:  # pragma: no cover - only if RTN metadata is missing
        return "unknown"


# ---------------------------------------------------------------------------
# Quality labels (Task F) — cam/telesync/webrip/bluray/... plus resolution,
# derived from the SAME RTN parse the gates already ran. The label written to
# downloads.quality_label / media_quality is this string, so CAM knowledge
# lives in the database, not in filenames (Design stance 10).
# ---------------------------------------------------------------------------

def _scalar(value):
    """RTN fields can be LISTS (spike item) — take the first scalar."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "unknown" else text


def quality_label_from_parsed(parsed) -> Optional[str]:
    """'webrip-720p' / 'bluray-1080p' / 'cam' / '2160p' / None, from an RTN
    ParsedData (or any object exposing quality/resolution)."""
    if parsed is None:
        return None
    quality = _scalar(getattr(parsed, "quality", None)).lower()
    resolution = _scalar(getattr(parsed, "resolution", None)).lower()
    if quality and resolution:
        return f"{quality}-{resolution}"
    return quality or resolution or None


def parse_quality_label(title: str) -> Optional[str]:
    """Quality label for a release title (one RTN parse). The automatic grab
    paths reuse the label the selection engine already derived on the chosen
    candidate (ScoreBreakdown.quality_label); this exists for manual/legacy
    entry points that never went through a scored decision."""
    if not (title or "").strip():
        return None
    try:
        return quality_label_from_parsed(parse(title))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Selection modes (section 4 item 5) — declared on every decision.
# ---------------------------------------------------------------------------
MODE_AUTOMATIC_SINGLE = "automatic-single"
MODE_AUTOMATIC_SEASON_PACK = "automatic-season-pack"
MODE_AUTOMATIC_EPISODE = "automatic-episode"
MODE_AUTOMATIC_REPLACEMENT = "automatic-replacement"
MODE_ZERO_SEEDER_RACE = "zero-seeder-race"
MODE_MANUAL_USER_PICK = "manual-user-pick"
# Task C item 8: a compatible quarantined payload adopted for a NEW request
# after fresh verification, without a second download.
MODE_REUSED_QUARANTINE = "reused_quarantine"

_SELECTION_MODES = frozenset({
    MODE_AUTOMATIC_SINGLE, MODE_AUTOMATIC_SEASON_PACK, MODE_AUTOMATIC_EPISODE,
    MODE_AUTOMATIC_REPLACEMENT, MODE_ZERO_SEEDER_RACE, MODE_MANUAL_USER_PICK,
    MODE_REUSED_QUARANTINE,
})

# ---------------------------------------------------------------------------
# Blocklist interface (Task C provides the real table; Phase 1 defines the shape)
# ---------------------------------------------------------------------------
# A release wrong for one identity can be right for another, so the block is an
# INPUT to the pure selector, never a config read. Reason-aware: race losers and
# transient failures NEVER block (Design stance 7). These reason codes match
# Task C section 5 item 2.
NON_BLOCKING_REASONS = frozenset({
    "race_loser",
    "user_cancel_no_block",
    "download_stalled",
    "tracker_timeout",
    "client_error",
})


@dataclass(frozen=True)
class BlocklistEntry:
    """One subject-scoped blocklist row, reason-coded. Matched on infohash OR
    on (normalized title, size within 2%, release group)."""
    reason_code: str
    infohash: Optional[str] = None
    parsed_title: Optional[str] = None
    size_bytes: Optional[int] = None
    release_group: Optional[str] = None


# A blocklist lookup returns the matching reason code (a string) when a candidate
# is blocked, or None when it is not. `blocklist=` on select_torrent accepts this
# callable, an iterable of BlocklistEntry, or an iterable of bare infohash
# strings (treated as permanent identity_mismatch blocks).
BlocklistLookup = Callable[["Candidate", object], Optional[str]]

# recent_failures: same shape, but a soft SCORE penalty rather than a hard gate.
RecentFailureLookup = Callable[["Candidate"], bool]


# ---------------------------------------------------------------------------
# Candidate + want inputs
# ---------------------------------------------------------------------------

_INFOHASH_RE = re.compile(r"btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_BASE32_RE = re.compile(r"^[a-z2-7]{32}$")


def infohash_from_magnet(magnet: str) -> str:
    """Lowercased btih info-hash from a magnet URI, or '' if absent."""
    m = _INFOHASH_RE.search(magnet or "")
    return m.group(1).lower() if m else ""


@dataclass(frozen=True)
class Candidate:
    """One release under consideration. Normalized, source-agnostic, hashable."""
    title: str
    infohash: str
    size_bytes: int
    seeders: int
    source: str = ""

    @property
    def norm_infohash(self) -> str:
        return (self.infohash or "").lower()


def to_candidate(result) -> Candidate:
    """Adapt a torrent_search.TorrentResult (or any object exposing
    title/magnet/size_bytes/seeders/source) into a Candidate. Duck-typed so the
    pure selector never imports torrent_search."""
    infohash = getattr(result, "infohash", "") or infohash_from_magnet(
        getattr(result, "magnet", ""))
    return Candidate(
        title=getattr(result, "title", "") or "",
        infohash=(infohash or "").lower(),
        size_bytes=int(getattr(result, "size_bytes", 0) or 0),
        seeders=int(getattr(result, "seeders", 0) or 0),
        source=getattr(result, "source", "") or "",
    )


@dataclass(frozen=True)
class SelectWant:
    """The immutable target a selection is judged against: a MediaIdentity plus
    the size prefs, runtime, CAM policy and selection mode. Built from the
    downloads want_json snapshot in Phase 3; built directly in tests."""
    identity: MediaIdentity
    size_pref_mb_min: float = 0.0
    size_max_rate: float = 0.0          # max MB/min; 0 disables the size gate
    runtime_minutes: Optional[float] = None
    fallback_minutes: float = 24.0      # used when runtime is unknown
    allow_cam: bool = False
    mode: str = MODE_AUTOMATIC_SINGLE

    @property
    def effective_minutes(self) -> float:
        rt = self.runtime_minutes
        if rt and rt > 0:
            return float(rt)
        return float(self.fallback_minutes)

    @property
    def target_bytes(self) -> Optional[float]:
        if self.size_pref_mb_min and self.size_pref_mb_min > 0:
            return self.size_pref_mb_min * self.effective_minutes * 1024 * 1024
        return None

    @property
    def wants_season_pack(self) -> bool:
        """A whole-season want: TV, a specific season, no specific episode."""
        return (self.identity.media_type not in ("movie",)
                and self.identity.season is not None
                and self.identity.episode is None)


def want_from_snapshot(snapshot: dict, *, mode: str = MODE_AUTOMATIC_SINGLE,
                       allow_cam: Optional[bool] = None,
                       fallback_minutes: Optional[float] = None) -> SelectWant:
    """Build a SelectWant from a downloads.want_json dict (the Phase 0 snapshot
    schema in download_manager._build_want_snapshot). Convenience for Phase 3."""
    identity = MediaIdentity(
        media_type=snapshot.get("media_type") or "unknown",
        identity_source=snapshot.get("identity_source"),
        external_id=(str(snapshot["external_id"])
                     if snapshot.get("external_id") is not None else None),
        canonical_title=snapshot.get("canonical_title"),
        canonical_year=snapshot.get("canonical_year"),
        origin_countries=tuple(snapshot.get("origin_countries") or ()),
        aliases=tuple(snapshot.get("aliases") or ()),
        season=snapshot.get("season"),
        episode=snapshot.get("episode"),
    )
    fb = fallback_minutes
    if fb is None:
        fb = 120.0 if identity.media_type == "movie" else 24.0
    return SelectWant(
        identity=identity,
        size_pref_mb_min=float(snapshot.get("size_pref_mb_min") or 0.0),
        size_max_rate=float(snapshot.get("size_max_rate") or 0.0),
        runtime_minutes=snapshot.get("runtime_minutes"),
        fallback_minutes=fb,
        allow_cam=(bool(allow_cam) if allow_cam is not None else False),
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Decision records — immutable and complete (section 4 item 4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateVerdict:
    """One candidate's pass/fail through the eight gates."""
    infohash: str
    title: str
    passed: bool
    reason_code: str          # "ok" when passed, else the rejecting gate reason
    detail: str = ""


@dataclass(frozen=True)
class ScoreBreakdown:
    """A survivor's per-component score, ready to render in the grab queue."""
    infohash: str
    title: str
    total: float
    components: dict
    seeders: int
    size_bytes: int
    size_distance: float      # abs(log2(size/target)); tie-break key 3
    # Task F: the quality label from the SAME parse the gates ran, so grab
    # time reuses it instead of re-parsing the chosen candidate.
    quality_label: Optional[str] = None


@dataclass(frozen=True)
class SelectionDecision:
    """The complete, immutable record of one selection pass."""
    chosen_infohash: Optional[str]
    chosen_title: Optional[str]
    mode: str
    profile: str
    rtn_version: str
    verdicts: tuple           # tuple[GateVerdict, ...]  — EVERY candidate
    scores: tuple             # tuple[ScoreBreakdown, ...] — top 5 survivors
    pool_stats: dict
    created_at: str
    reason: str = ""          # why nothing was chosen, when chosen_* is None

    @property
    def chosen(self) -> bool:
        return self.chosen_infohash is not None

    def verdict_histogram(self) -> dict:
        """reason_code -> count across every candidate. The forever-retained
        rejection aggregate (Task E retention)."""
        hist: dict = {}
        for v in self.verdicts:
            hist[v.reason_code] = hist.get(v.reason_code, 0) + 1
        return hist


# ---------------------------------------------------------------------------
# Internal: a lazily-built default ranker (RTN construction is not free)
# ---------------------------------------------------------------------------
_RANKER = None
_SETTINGS = None


def _ranker():
    global _RANKER, _SETTINGS
    if _RANKER is None:
        _SETTINGS = SettingsModel()
        _RANKER = RTN.RTN(settings=_SETTINGS, ranking_model=DefaultRanking())
    return _RANKER


def _valid_infohash(ih: str) -> bool:
    ih = (ih or "").lower()
    return bool(_HEX40_RE.match(ih) or _BASE32_RE.match(ih))


def _rtn_rank(title: str, infohash: str) -> Optional[int]:
    """RTN integer rank for a title (remove_trash=False so CAM survivors under
    allow_cam still score instead of raising). The rank is independent of the
    info-hash value, so an invalid/missing hash is replaced with a synthetic one
    purely to satisfy RTN's validation. Returns None if RTN cannot rank it."""
    ih = infohash if _valid_infohash(infohash) else hashlib.sha1(
        (title or "").encode("utf-8", "replace")).hexdigest()
    try:
        torrent = _ranker().rank(title, ih, remove_trash=False)
        return int(torrent.rank)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Blocklist adaptation
# ---------------------------------------------------------------------------

def make_blocklist(entries: Iterable) -> BlocklistLookup:
    """Turn an iterable of BlocklistEntry (or bare infohash strings) into a
    reason-aware lookup. Non-blocking reasons (race_loser, transient) are dropped
    up front so they can never block. Matches on infohash OR on
    (normalized title, size within 2%, release group)."""
    norm_entries: list[BlocklistEntry] = []
    for e in entries or ():
        if isinstance(e, str):
            norm_entries.append(BlocklistEntry(reason_code="identity_mismatch",
                                               infohash=e.lower()))
        elif isinstance(e, BlocklistEntry):
            if e.reason_code in NON_BLOCKING_REASONS:
                continue
            norm_entries.append(e)
    blocked_hashes = {e.infohash.lower() for e in norm_entries if e.infohash}

    def lookup(candidate: Candidate, parsed) -> Optional[str]:
        if candidate.norm_infohash and candidate.norm_infohash in blocked_hashes:
            for e in norm_entries:
                if e.infohash and e.infohash.lower() == candidate.norm_infohash:
                    return e.reason_code
        p_title = media_identity.normalize_title(
            getattr(parsed, "parsed_title", None) or getattr(parsed, "title", ""))
        p_group = (getattr(parsed, "group", "") or "").lower()
        for e in norm_entries:
            if e.parsed_title is None or e.size_bytes is None:
                continue
            if media_identity.normalize_title(e.parsed_title) != p_title:
                continue
            if e.size_bytes <= 0 or candidate.size_bytes <= 0:
                continue
            if abs(candidate.size_bytes - e.size_bytes) > 0.02 * e.size_bytes:
                continue
            if e.release_group and e.release_group.lower() != p_group:
                continue
            return e.reason_code
        return None

    return lookup


def _as_blocklist_lookup(blocklist) -> BlocklistLookup:
    if blocklist is None:
        return lambda c, p: None
    if callable(blocklist):
        return blocklist
    return make_blocklist(blocklist)


def _as_recent_failure_lookup(recent_failures) -> RecentFailureLookup:
    if recent_failures is None:
        return lambda c: False
    if callable(recent_failures):
        return recent_failures
    hashes = {str(h).lower() for h in recent_failures}
    return lambda c: c.norm_infohash in hashes


# ---------------------------------------------------------------------------
# The eight hard gates, in order (section 4 item 2)
# ---------------------------------------------------------------------------

def _run_gates(candidate: Candidate, want: SelectWant, parsed,
               blocklist: BlocklistLookup,
               cam_check: Optional[Callable[[str], bool]]):
    """Return (passed, reason_code, detail). `parsed` is the RTN ParsedData (or
    None if gate 1 already failed to parse)."""
    identity = want.identity

    # Gate 1: parse. Handled by the caller (parsed is None -> unparseable).
    if parsed is None or not (getattr(parsed, "parsed_title", "") or "").strip():
        return False, "unparseable", f"RTN could not parse {candidate.title!r}"

    # Gate 2: blocklist (subject-scoped; reason-aware).
    reason = blocklist(candidate, parsed)
    if reason and reason not in NON_BLOCKING_REASONS:
        return False, "blocklisted", f"blocked: {reason}"

    # Gate 3: title identity. RTN.title_match AND no sequel/numeric mismatch.
    # title_match alone accepts "Angry Birds Movie 2" for "Angry Birds Movie"
    # (spike-confirmed True), so the sequel/numeric guards are what actually
    # kill it.
    canonical = identity.canonical_title or ""
    parsed_title = getattr(parsed, "parsed_title", "") or ""
    if canonical and parsed_title:
        try:
            matched = title_match(canonical, parsed_title)
        except Exception:
            matched = False
        if not matched:
            return False, "title_mismatch", (
                f"'{parsed_title}' does not match wanted '{canonical}'")
        if media_identity.sequel_mismatch(canonical, parsed_title):
            return False, "sequel_mismatch", (
                f"wanted '{canonical}', parsed '{parsed_title}'")
        if media_identity.numeric_title_mismatch(canonical, parsed_title):
            return False, "numeric_title_mismatch", (
                f"candidate carries a sequel number '{canonical}' does not")

    # Gate 4: country edition. Contradiction rejects; absence never does.
    parsed_country = getattr(parsed, "country", None)
    if media_identity.country_edition_mismatch(identity.origin_countries,
                                               parsed_country):
        return False, "country_edition_contradiction", (
            f"wanted {list(identity.origin_countries)}, parsed '{parsed_country}'")

    # Gate 5: year (movies only). TV premiere year is scoring, never a gate.
    parsed_year = getattr(parsed, "year", None)
    if (identity.media_type == "movie" and identity.canonical_year
            and parsed_year):
        try:
            if abs(int(parsed_year) - int(identity.canonical_year)) > 1:
                return False, "year_mismatch", (
                    f"wanted {identity.canonical_year}, parsed {parsed_year}")
        except (TypeError, ValueError):
            pass

    # Gate 6: season/episode (tv). Also vetoes episode markers on a MOVIE grab
    # (preserving _looks_like_episode_release: "Movie Title S01E03" is a show).
    seasons = _int_list(getattr(parsed, "seasons", None))
    episodes = _int_list(getattr(parsed, "episodes", None))
    if identity.media_type == "movie":
        if seasons or episodes:
            return False, "episode_marker_on_movie", (
                f"movie want, parsed seasons {seasons} episodes {episodes}")
    elif identity.season is not None:
        if seasons and int(identity.season) not in seasons:
            return False, "season_contradiction", (
                f"wanted S{identity.season}, parsed seasons {seasons}")

    # Gate 7: CAM/TS. RTN ParsedData.trash is the trash contract (the spike
    # proved it agrees with CAM_RE on the corpus). An optional cam_check hook
    # lets the wiring layer pass video_quality.is_cam_release without this pure
    # module importing config. allow_cam bypasses the gate.
    if not want.allow_cam:
        is_trash = bool(getattr(parsed, "trash", False))
        if cam_check is not None:
            try:
                is_trash = is_trash or bool(cam_check(candidate.title))
            except Exception:
                pass
        if is_trash:
            return False, "cam_or_trash", f"CAM/TS/trash: {candidate.title!r}"

    # Gate 8: size. Zero size is unverifiable garbage; oversize exceeds the max
    # MB/min cap (preserving _oversize_gate's max behavior).
    if candidate.size_bytes <= 0:
        return False, "zero_size", "size 0 is never downloaded"
    if want.size_max_rate and want.size_max_rate > 0:
        cap = want.size_max_rate * want.effective_minutes * 1024 * 1024
        if candidate.size_bytes > cap:
            return False, "oversize", (
                f"{candidate.size_bytes} > cap {int(cap)} "
                f"({want.size_max_rate} MB/min x {want.effective_minutes:.0f}m)")

    return True, "ok", ""


def _int_list(value) -> list[int]:
    """RTN season/episode fields are LISTS (spike item). Coerce defensively."""
    if value is None:
        return []
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return [int(value)]
    out: list[int] = []
    try:
        for v in value:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    except TypeError:
        return []
    return out


# ---------------------------------------------------------------------------
# The versioned score (survivors only) — plexxarr-v1 (section 4 item 3)
# ---------------------------------------------------------------------------

def _score(candidate: Candidate, want: SelectWant, parsed,
           recent_failure: bool) -> ScoreBreakdown:
    identity = want.identity
    components: dict = {}

    # rtn_quality: 0-120.
    rank = _rtn_rank(candidate.title, candidate.infohash)
    if rank is None:
        rtn_quality = 0.0
    else:
        clamped = max(_RANK_FLOOR, min(_RANK_CEIL, float(rank)))
        rtn_quality = ((clamped - _RANK_FLOOR) / (_RANK_CEIL - _RANK_FLOOR)
                       * _RTN_QUALITY_MAX)
    components["rtn_quality"] = round(rtn_quality, 3)

    # size_closeness: 0-35, peaks when size == target. 2^-|log2(size/target)|
    # == min(ratio, 1/ratio) — a clean inverted-log form.
    target = want.target_bytes
    if target and candidate.size_bytes > 0:
        ratio = candidate.size_bytes / target
        size_distance = abs(math.log2(ratio))
        closeness = _SIZE_CLOSENESS_MAX * min(ratio, 1.0 / ratio)
    else:
        size_distance = 0.0
        closeness = 0.0
    components["size_closeness"] = round(closeness, 3)

    # seed_health: 0-8, log-scaled.
    seed_health = min(_SEED_HEALTH_MAX,
                      math.log2(candidate.seeders + 1)) if candidate.seeders > 0 else 0.0
    components["seed_health"] = round(seed_health, 3)

    # season_pack: +15 for a pack on a whole-season want.
    season_pack = 0.0
    if want.wants_season_pack:
        seasons = _int_list(getattr(parsed, "seasons", None))
        episodes = _int_list(getattr(parsed, "episodes", None))
        if seasons and int(identity.season) in seasons and not episodes:
            season_pack = _SEASON_PACK_BONUS
    components["season_pack"] = season_pack

    # country_alias: +20 on a POSITIVE country OR alias match.
    country_alias = 0.0
    parsed_country = getattr(parsed, "country", None)
    if identity.origin_countries and parsed_country:
        want_c = media_identity._country_set(identity.origin_countries)
        parsed_c = media_identity._country_set(parsed_country)
        if want_c & parsed_c:
            country_alias = _COUNTRY_ALIAS_BONUS
    if country_alias == 0.0 and identity.aliases:
        norm_release = media_identity.normalize_title(candidate.title)
        canonical_norm = media_identity.normalize_title(identity.canonical_title)
        for alias in identity.aliases:
            an = media_identity.normalize_title(alias)
            if an and an != canonical_norm and an in norm_release:
                country_alias = _COUNTRY_ALIAS_BONUS
                break
    components["country_alias"] = country_alias

    # recent_failure: -25 when this release failed for this context in-window.
    components["recent_failure"] = _RECENT_FAILURE_PENALTY if recent_failure else 0.0

    total = sum(components.values())
    return ScoreBreakdown(
        infohash=candidate.norm_infohash,
        title=candidate.title,
        total=round(total, 3),
        components=components,
        seeders=candidate.seeders,
        size_bytes=candidate.size_bytes,
        size_distance=round(size_distance, 6),
        quality_label=quality_label_from_parsed(parsed),
    )


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

def select_torrent(candidates: Iterable, want: SelectWant, *,
                   blocklist=None, recent_failures=None,
                   cam_check: Optional[Callable[[str], bool]] = None,
                   pool_stats: Optional[dict] = None,
                   now: Optional[str] = None) -> SelectionDecision:
    """Run the gates, score the survivors, and return the complete decision.

    candidates : iterable of Candidate (use to_candidate() to adapt
                 torrent_search.TorrentResult).
    want       : the SelectWant target.
    blocklist  : None | reason-aware lookup callable | iterable[BlocklistEntry]
                 | iterable[str infohash]. Non-blocking reasons never block.
    recent_failures : None | callable(Candidate)->bool | iterable[str infohash];
                 drives the -25 recent_failure score component (replacing the
                 order-dependent _prefer_unfailed pre-sort).
    cam_check  : optional callable(title)->bool for the CAM gate (pass
                 video_quality.is_cam_release from the wiring layer).
    pool_stats : optional per-source pool sizes to record on the decision.

    Manual picks (mode == manual-user-pick) still run every hard gate as
    preflight — an identity/sequel mismatch on a manual grab is surfaced, not
    silently accepted; the caller decides whether to record a typed override.
    """
    cands = [c if isinstance(c, Candidate) else to_candidate(c)
             for c in candidates]
    block_lookup = _as_blocklist_lookup(blocklist)
    fail_lookup = _as_recent_failure_lookup(recent_failures)

    verdicts: list[GateVerdict] = []
    survivors: list[tuple[Candidate, object]] = []

    for cand in cands:
        try:
            parsed = parse(cand.title) if (cand.title or "").strip() else None
        except Exception:
            parsed = None
        passed, reason, detail = _run_gates(cand, want, parsed, block_lookup,
                                            cam_check)
        verdicts.append(GateVerdict(
            infohash=cand.norm_infohash, title=cand.title,
            passed=passed, reason_code=reason, detail=detail))
        if passed:
            survivors.append((cand, parsed))

    scored: list[ScoreBreakdown] = [
        _score(cand, want, parsed, fail_lookup(cand))
        for cand, parsed in survivors
    ]

    # Deterministic ordering: total desc, seeders desc, size-distance asc,
    # normalized infohash asc.
    scored.sort(key=lambda s: (
        -s.total, -s.seeders, s.size_distance, s.infohash))

    stats = dict(pool_stats or {})
    stats.setdefault("candidates", len(cands))
    stats.setdefault("survivors", len(survivors))
    stats.setdefault("rejected", len(cands) - len(survivors))

    created = now if now is not None else _dt.datetime.now(_dt.timezone.utc).isoformat()
    mode = want.mode if want.mode in _SELECTION_MODES else MODE_AUTOMATIC_SINGLE

    if not scored:
        return SelectionDecision(
            chosen_infohash=None, chosen_title=None, mode=mode,
            profile=PROFILE, rtn_version=rtn_version(),
            verdicts=tuple(verdicts), scores=tuple(), pool_stats=stats,
            created_at=created,
            reason=("no candidates" if not cands else "all candidates rejected"))

    best = scored[0]
    return SelectionDecision(
        chosen_infohash=best.infohash, chosen_title=best.title, mode=mode,
        profile=PROFILE, rtn_version=rtn_version(),
        verdicts=tuple(verdicts), scores=tuple(scored[:5]), pool_stats=stats,
        created_at=created, reason="")
