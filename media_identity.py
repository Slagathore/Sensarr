# =============================================================================
# media_identity.py
# =============================================================================
# The single source of truth for comparing "what we wanted" against "what a
# release / file actually is". Pure module: no I/O, no config, no network, no
# heavy deps. Everything here is safe to import in CI with only the pytest
# subset installed.
#
# The sequel-number logic was MOVED here out of media_lookup._sequel_mismatch
# (which is where it grew up guarding the library check). media_lookup keeps
# thin delegating aliases so its existing callers and tests keep working.
#
# Selection, pre-move verification, reconciliation and intake dedupe all route
# through these functions so there is exactly one definition of "different
# entry in a series", "wrong country edition", and "different year".
# =============================================================================

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Title normalisation
# ---------------------------------------------------------------------------

def normalize_title(title: str | None) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace.

    Deliberately dependency-free (does not lean on RTN.normalize_title) so this
    module stays importable with the minimal CI dep set. RTN's own normaliser
    is used by the selection layer on top of this, not instead of it.
    """
    text = (title or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Sequel-number guard (MOVED verbatim from media_lookup)
# ---------------------------------------------------------------------------
# "Dune Part Two" must never match a library entry of just "Dune", and
# "<movie> 2" must not match "<movie>". The guard extracts the sequel numbers
# each title carries and treats titles with different numbers as different
# entries in a series.

_ROMAN_NUMERALS = {
    "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_PART_MARKERS = {"part", "pt", "chapter", "vol", "volume", "book", "season"}

# Tokens that mark the start of release-group junk in a filename. Sequel
# numbers sit NEXT TO the title ("John Wick 3"); digits after these markers
# are codec/audio noise ("DDP5.1", "x265", "1080p") and must not pollute the
# signature — otherwise junk digits block legitimate matches.
_JUNK_TOKEN_RE = re.compile(
    r"^(?:\d{3,4}p|x26[45]|h26[45]|hevc|avc|blu-?ray|b[dr]rip|web-?(?:dl|rip)?"
    r"|hdtv|dvdrip|remux|proper|repack|extended|uncut|imax|hdr(?:10)?\+?"
    r"|dolby|vision|dv|atmos|ddp?\d?|dts(?:hd)?|aac\d?|ac3|truehd|opus"
    r"|\d+bit|multi|dual|sub(?:bed|s)?|dub(?:bed)?|remaster(?:ed)?)$",
    re.IGNORECASE,
)


def _signature_portion(title: str) -> list[str]:
    """Tokens of the title up to the first year or release-junk marker.

    "john wick 3 2019 1080p ddp5 1 x265 group" -> ["john", "wick", "3"]
    """
    tokens = re.findall(r"[a-z0-9]+", title.casefold())
    portion: list[str] = []
    for tok in tokens:
        if tok.isdigit() and len(tok) == 4 and 1880 <= int(tok) <= 2159:
            break  # year — title (and any sequel number) ends here
        if _JUNK_TOKEN_RE.match(tok):
            break
        portion.append(tok)
    return portion or tokens  # a title that IS a year/junk word survives


def sequel_signature(title: str) -> frozenset[int]:
    """Return the set of sequel/part numbers a title carries.

    Captures: standalone digit tokens ("Movie 2", "Movie 10"), roman numerals
    ("Rocky III"), and number words directly after a part marker
    ("Dune Part Two"). Only the portion of the name before the first
    year/release-junk marker is considered, so "DDP5.1"/"x265"-style noise
    from release groups can't inject phantom numbers. Four-digit numbers in
    the plausible-year range are ignored so "Blade Runner 2049" isn't treated
    as sequel #2049.
    """
    numbers: set[int] = set()
    prev = ""
    for tok in _signature_portion(title):
        if tok.isdigit():
            value = int(tok)
            if not (1880 <= value <= 2159 and len(tok) == 4):  # skip years
                numbers.add(value)
        elif tok in _ROMAN_NUMERALS:
            numbers.add(_ROMAN_NUMERALS[tok])
        elif tok in _NUMBER_WORDS and prev in _PART_MARKERS:
            numbers.add(_NUMBER_WORDS[tok])
        prev = tok
    return frozenset(numbers)


def sequel_mismatch(a: str, b: str) -> bool:
    """True when the two titles clearly refer to different entries in a series."""
    return sequel_signature(a) != sequel_signature(b)


def numeric_title_mismatch(canonical: str, candidate: str) -> bool:
    """True when the candidate carries a trailing sequel number the canonical
    does not. Directional on purpose: wanting "Angry Birds Movie" and getting
    "Angry Birds Movie 2" is a mismatch; the reverse (wanting the sequel,
    getting a candidate with no number) is left for the fuzzy/season gates to
    judge, not rejected here.
    """
    extra = sequel_signature(candidate) - sequel_signature(canonical)
    return bool(extra)


# ---------------------------------------------------------------------------
# Country-edition guard
# ---------------------------------------------------------------------------
# TVDB/TMDB give AU/US variants of the same show DISTINCT ids. A parsed release
# marker that CONTRADICTS the wanted origin is a mismatch; an ABSENT marker is
# never a mismatch (most valid releases carry no country tag at all).

_COUNTRY_SYNONYMS = {
    "USA": "US", "US": "US", "U.S.": "US", "U.S.A.": "US",
    "UK": "UK", "GB": "UK", "GBR": "UK", "ENGLAND": "UK",
    "AU": "AU", "AUS": "AU", "AUSTRALIA": "AU",
    "CA": "CA", "CAN": "CA", "CANADA": "CA",
    "NZ": "NZ", "NZL": "NZ",
}

# ISO 3166-1 alpha-3 -> alpha-2 for the realistic TV-origin codes. TVDB v4
# returns alpha-3 ("usa"); TMDB returns alpha-2 ("US"); both must land on the
# same canonical form or the display renders [USA] and comparisons drift. An
# unknown code passes through unchanged (uppercased) rather than being guessed.
_ALPHA3_TO_ALPHA2 = {
    "USA": "US", "GBR": "UK", "AUS": "AU", "CAN": "CA", "NZL": "NZ",
    "JPN": "JP", "KOR": "KR", "CHN": "CN", "IND": "IN", "FRA": "FR",
    "DEU": "DE", "ESP": "ES", "ITA": "IT", "NLD": "NL", "BEL": "BE",
    "SWE": "SE", "NOR": "NO", "DNK": "DK", "FIN": "FI", "IRL": "IE",
    "POL": "PL", "TUR": "TR", "RUS": "RU", "BRA": "BR", "MEX": "MX",
    "ARG": "AR", "COL": "CO", "ZAF": "ZA", "CHE": "CH", "AUT": "AT",
    "PRT": "PT", "GRC": "GR", "CZE": "CZ", "ISL": "IS", "ISR": "IL",
    "THA": "TH", "PHL": "PH", "IDN": "ID", "MYS": "MY", "SGP": "SG",
    "TWN": "TW", "HKG": "HK", "UKR": "UA", "HUN": "HU", "ROU": "RO",
}


def normalize_country(code: str | None) -> str:
    """Canonical short country code: alpha-2 style (UK for GB, matching the
    comparator's existing convention), accepting alpha-3 codes and common
    names. Unknown codes pass through uppercased rather than guessed.
    Returns '' for empty input."""
    if not code:
        return ""
    key = str(code).strip().upper()
    if not key:
        return ""
    return _COUNTRY_SYNONYMS.get(key) or _ALPHA3_TO_ALPHA2.get(key, key)


def _canon_country(code: str) -> str:
    return normalize_country(code)


def _country_set(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [str(value)]
    return {_canon_country(str(c)) for c in items if str(c).strip()}


def country_edition_mismatch(want_countries, parsed_countries) -> bool:
    """True only when BOTH sides carry a country marker and they are disjoint.

    want US + parsed AU -> True (contradiction).
    want US + parsed US -> False (agreement).
    want US + parsed <none> -> False (absence is never a contradiction).
    """
    want = _country_set(want_countries)
    parsed = _country_set(parsed_countries)
    if not want or not parsed:
        return False
    return not (want & parsed)


# ---------------------------------------------------------------------------
# Search alias (ASCII-preferring) — mirrors download_manager._ascii_preferring_title
# ---------------------------------------------------------------------------

def search_alias(resolved: str | None, content: str | None) -> str:
    """Prefer an ASCII title for indexer queries. TVDB/TMDB can resolve to a
    native-script primary title (the "Pursuit of jade" kanji incident);
    querying indexers with that returns garbage. Same logic as
    download_manager._ascii_preferring_title, kept pure so the intake path and
    the query builders can compute the stored alias without importing the
    download manager.
    """
    def ratio(s: str) -> float:
        letters = [c for c in s if not c.isspace()]
        return (sum(1 for c in letters if ord(c) < 128) / len(letters)) if letters else 0.0

    resolved = (resolved or "").strip()
    content = (content or "").strip()
    if resolved and ratio(resolved) >= 0.7:
        return resolved
    if content and ratio(content) >= 0.7:
        return content
    return resolved or content


# ---------------------------------------------------------------------------
# Identity dataclasses + comparator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MediaIdentity:
    """What a request is asking for, provider-qualified. The durable key is
    (media_type, identity_source, external_id) plus season for a TV season
    target. A bare external_id is NOT an identity — 12345 means nothing without
    knowing tmdb vs tvdb vs mal vs anidb.
    """
    media_type: str
    identity_source: str | None = None
    external_id: str | None = None
    canonical_title: str | None = None
    canonical_year: int | None = None
    origin_countries: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    season: int | None = None
    episode: int | None = None

    @property
    def is_qualified(self) -> bool:
        """True when this identity is auto-grabbable: a provider AND an id."""
        return bool(self.identity_source) and bool(self.external_id)

    @property
    def subject_key(self) -> str | None:
        """Blocklist / provenance subject key, e.g. 'tmdb:153518' or
        'tvdb:75692:s19'. None when the identity is not provider-qualified.
        """
        if not self.is_qualified:
            return None
        key = f"{self.identity_source}:{self.external_id}"
        if self.media_type != "movie" and self.season is not None:
            key = f"{key}:s{self.season}"
        return key


@dataclass(frozen=True)
class IdentityVerdict:
    """Outcome of comparing a want against a parsed candidate/file."""
    ok: bool
    reason_code: str
    detail: str = ""


def compare_media_identity(want: MediaIdentity, parsed) -> IdentityVerdict:
    """Compare a wanted identity against a parsed candidate/file.

    `parsed` is duck-typed: any object exposing the RTN ParsedData attributes
    (parsed_title/title, year, seasons, episodes, country). Kept RTN-free so
    this stays importable without the selection dependency; the selection layer
    (Task B) adds RTN.title_match on top of this, it does not replace it.

    Returns the FIRST failing verdict, or an ok verdict with reason 'ok'.
    """
    want_title = want.canonical_title or ""
    parsed_title = (
        getattr(parsed, "parsed_title", None)
        or getattr(parsed, "title", None)
        or ""
    )

    if want_title and parsed_title:
        if sequel_mismatch(want_title, parsed_title):
            return IdentityVerdict(
                False, "sequel_mismatch",
                f"wanted '{want_title}', parsed '{parsed_title}'")
        if numeric_title_mismatch(want_title, parsed_title):
            return IdentityVerdict(
                False, "numeric_title_mismatch",
                f"candidate carries a sequel number '{want_title}' does not")

    parsed_country = getattr(parsed, "country", None)
    if country_edition_mismatch(want.origin_countries, parsed_country):
        return IdentityVerdict(
            False, "country_edition_contradiction",
            f"wanted {list(want.origin_countries)}, parsed '{parsed_country}'")

    parsed_year = getattr(parsed, "year", None)
    if want.media_type == "movie" and want.canonical_year and parsed_year:
        try:
            if abs(int(parsed_year) - int(want.canonical_year)) > 1:
                return IdentityVerdict(
                    False, "year_mismatch",
                    f"wanted {want.canonical_year}, parsed {parsed_year}")
        except (TypeError, ValueError):
            pass

    if want.season is not None:
        seasons = getattr(parsed, "seasons", None) or []
        try:
            season_list = [int(s) for s in seasons]
        except (TypeError, ValueError):
            season_list = []
        if season_list and int(want.season) not in season_list:
            return IdentityVerdict(
                False, "season_contradiction",
                f"wanted S{want.season}, parsed seasons {season_list}")

    return IdentityVerdict(True, "ok", "")
