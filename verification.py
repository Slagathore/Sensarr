# =============================================================================
# verification.py  —  pre-move verification (Task C item 1)
# =============================================================================
# Verification happens BEFORE movement (Design stance 5: a wrong move is worse
# than no move). This module is the pure, testable core of the new _post_process
# loop: it enumerates the actual files, classifies their roles, parses their
# real names, and compares each against the immutable want_json via
# media_identity — the SINGLE comparator (Design stance 6). No I/O beyond
# reading file sizes/paths that the caller already handed in; no config, no DB.
#
# Honesty (Design stance 11): re-parsing the downloaded path catches DISAGREEMENT
# between the wanted identity and the actual file names (a sequel payload, a
# contradictory season). It does NOT identify deliberately mislabeled
# audiovisual content — that stays a manual/future problem, and nothing here
# claims otherwise.
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import media_identity
import torrent_routing
from media_identity import MediaIdentity

# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
ROLE_PRIMARY_VIDEO = "primary_video"
ROLE_EPISODE = "episode"
ROLE_SUBTITLE = "subtitle"
ROLE_SAMPLE = "sample"
ROLE_EXTRA = "extra"
ROLE_UNKNOWN = "unknown"

# Roles whose identity must match the want before ANY move (a contradictory one
# of these blocks the whole download). Samples/extras/unknowns are never moved
# to a library root regardless, so they don't gate identity.
_IDENTITY_GATING_ROLES = frozenset({ROLE_PRIMARY_VIDEO, ROLE_EPISODE})

_SAMPLE_RE = re.compile(r"(?:^|[\W_])sample(?:[\W_]|$)", re.IGNORECASE)
_EXTRA_DIR_RE = re.compile(
    r"^(?:extras?|featurettes?|behind[ ._-]the[ ._-]scenes|deleted[ ._-]scenes|"
    r"interviews?|scenes|shorts|trailers?|other|nc(?:op|ed)s?|creditless|"
    r"menus?|bonus(?:es)?|specials?|special[ ._-]features?)$", re.IGNORECASE)
_EXTRA_NAME_RE = re.compile(
    r"(?:^|[\W_])(?:trailer|featurette|deleted|behind[ ._-]the[ ._-]scenes|"
    r"extra|nc(?:op|ed)|creditless|preview)(?:[\W_]|$)", re.IGNORECASE)

_YEAR_RE = re.compile(r"(?<![\d])(19\d{2}|20\d{2})(?![\d])")
# A standalone country marker sitting between separators (".AU.", " US ", "-UK-").
_COUNTRY_MARKER_RE = re.compile(
    r"(?:^|[. _\-\[(])(US|USA|UK|GB|GBR|AU|AUS|CA|CAN|NZ|NZL)(?=[. _\-\])]|$)")


# ---------------------------------------------------------------------------
# Parsed-file adapter (duck-typed for media_identity.compare_media_identity)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParsedFile:
    """The RTN-ParsedData-shaped view of an on-disk file name that
    media_identity.compare_media_identity consumes. parsed_title is kept as the
    RAW name portion so the sequel/numeric guards tokenize it themselves (they
    stop at the year/release-junk boundary)."""
    parsed_title: str
    year: int | None = None
    seasons: tuple[int, ...] = ()
    episodes: tuple[int, ...] = ()
    country: str | None = None

    @property
    def title(self) -> str:      # compat with the .title fallback
        return self.parsed_title


def _detect_country(name: str) -> str | None:
    m = _COUNTRY_MARKER_RE.search(name)
    return media_identity.normalize_country(m.group(1)) if m else None


def parse_file(path: Path) -> ParsedFile:
    """Parse an on-disk file/folder name into the identity-comparison view."""
    name = path.name
    parsed = torrent_routing.parse_torrent_name(name)
    stem = path.stem if path.suffix else name
    ym = _YEAR_RE.search(stem)
    year = int(ym.group(1)) if ym else None
    seasons = (parsed.season,) if parsed.season is not None else ()
    episodes = (parsed.episode,) if parsed.episode is not None else ()
    # parsed_title stays the raw stem so the sequel guard sees "<name> 2 <year>".
    return ParsedFile(parsed_title=stem, year=year, seasons=seasons,
                      episodes=episodes, country=_detect_country(name))


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

def classify_role(path: Path, *, is_video: bool, is_subtitle: bool,
                  single_video: bool, want: MediaIdentity) -> str:
    name = path.name
    if is_subtitle:
        return ROLE_SUBTITLE
    if not is_video:
        return ROLE_UNKNOWN
    if _SAMPLE_RE.search(name):
        return ROLE_SAMPLE
    if any(_EXTRA_DIR_RE.match(p) for p in path.parts[:-1]) or _EXTRA_NAME_RE.search(name):
        # A too-small file inside an extras dir is an extra; but never
        # misclassify the single main video as an extra.
        if not single_video:
            return ROLE_EXTRA
    parsed = torrent_routing.parse_torrent_name(name)
    if parsed.episode is not None:
        return ROLE_EPISODE
    # TV/anime wants without an episode marker are still episodic payloads.
    if want.media_type in ("tv", "anime", "xanime"):
        return ROLE_EPISODE
    return ROLE_PRIMARY_VIDEO


# ---------------------------------------------------------------------------
# Per-file + aggregate verdicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileVerdict:
    path: Path
    role: str
    ok: bool
    reason_code: str
    detail: str = ""
    parsed: ParsedFile | None = None


@dataclass
class VerificationResult:
    """Aggregate outcome for one download's staging contents."""
    ok: bool
    reason_code: str
    detail: str
    file_verdicts: list[FileVerdict] = field(default_factory=list)
    # The first identity-gating file that contradicted the want (drives the
    # blocklist parsed_title/size for the wrong-grab entry).
    offending: FileVerdict | None = None

    @property
    def gating_files(self) -> list[FileVerdict]:
        return [v for v in self.file_verdicts if v.role in _IDENTITY_GATING_ROLES]


# A trailing standalone number on an EPISODIC file is an episode/absolute number
# ("Bare Show - 05"), NOT a sequel number — stripping it before the sequel guard
# keeps such files (which carry no season evidence) from being read as sequels.
# Movies keep the full sequel logic; only tv/anime episodic wants strip.
_EPISODIC_TRAILING_NUM_RE = re.compile(r"[\s._\-]+\d{1,4}\s*$")
_BRACKET_BLOCK_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def _episodic_compare_title(raw: str) -> str:
    t = _BRACKET_BLOCK_RE.sub(" ", raw)
    t = re.sub(r"\s{2,}", " ", t).strip()
    prev = None
    while prev != t:
        prev = t
        t = _EPISODIC_TRAILING_NUM_RE.sub("", t).strip()
    return t or raw


def verify_file(path: Path, want: MediaIdentity, role: str) -> FileVerdict:
    parsed = parse_file(path)
    if role not in _IDENTITY_GATING_ROLES:
        return FileVerdict(path, role, True, "ok", "", parsed)
    # Episodic wants: compare on the SHOW-TITLE portion, not the trailing episode
    # number (Task D item 2 composes with the season contradiction gate — the
    # parsed seasons tuple is untouched, so an S03-vs-S02 contradiction still
    # fails; only the bare-number false sequel is defused).
    if want.media_type in ("tv", "anime", "xanime"):
        parsed = replace(
            parsed, parsed_title=_episodic_compare_title(parsed.parsed_title))
    verdict = media_identity.compare_media_identity(want, parsed)
    return FileVerdict(path, role, verdict.ok, verdict.reason_code,
                       verdict.detail, parsed)


def verify_staging(files: list[Path], want: MediaIdentity, *,
                   video_exts: set[str], subtitle_exts: set[str]
                   ) -> VerificationResult:
    """Enumerate -> classify -> parse -> compare. Returns the aggregate identity
    verdict for the whole download. A single contradictory identity-gating file
    fails the download (NO move, quarantine)."""
    videos = [f for f in files if f.suffix.lower() in video_exts]
    single_video = len(videos) == 1
    verdicts: list[FileVerdict] = []
    offending: FileVerdict | None = None
    for f in files:
        suffix = f.suffix.lower()
        is_video = suffix in video_exts
        is_subtitle = suffix in subtitle_exts
        role = classify_role(f, is_video=is_video, is_subtitle=is_subtitle,
                             single_video=single_video, want=want)
        fv = verify_file(f, want, role)
        verdicts.append(fv)
        if not fv.ok and offending is None:
            offending = fv

    gating = [v for v in verdicts if v.role in _IDENTITY_GATING_ROLES]
    if not gating:
        return VerificationResult(
            False, "no_media", "no primary/episode video in staging", verdicts)
    if offending is not None:
        return VerificationResult(
            False, offending.reason_code,
            f"{offending.path.name}: {offending.detail}", verdicts, offending)
    return VerificationResult(True, "ok", "", verdicts)


def identity_from_want(want: dict) -> MediaIdentity:
    """Reconstruct the frozen MediaIdentity from a downloads.want_json dict."""
    return MediaIdentity(
        media_type=want.get("media_type") or "unknown",
        identity_source=want.get("identity_source"),
        external_id=(str(want["external_id"])
                     if want.get("external_id") is not None else None),
        canonical_title=want.get("canonical_title"),
        canonical_year=want.get("canonical_year"),
        origin_countries=tuple(want.get("origin_countries") or ()),
        aliases=tuple(want.get("aliases") or ()),
        season=want.get("season"),
        episode=want.get("episode"),
    )
