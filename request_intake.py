# =============================================================================
# request_intake.py
# =============================================================================
# The single resolve-or-needs_identity path shared by every intake surface:
# the structured Telegram flow, the /request command, the desktop Requests tab,
# and the Plex watchlist queue.
#
# The rule (Task A item 1): no movie/tv/anime request row becomes auto-grabbable
# without identity_source + external_id. Anything unresolved lands as
# needs_identity and stays visible. 'other' is a deliberate human choice, exempt
# from the identity requirement, and is never coerced into a typed grab.
#
# This module is deliberately thin and import-cheap: it depends on queue_store
# and media_identity (both pure-ish) and takes already-fetched MediaResult
# objects from its callers, so it does NOT drag the network into surfaces that
# already did their own lookup.
# =============================================================================

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import media_identity
import queue_store

# Media types that MUST carry a provider-qualified identity to be grabbed.
_TYPED_MEDIA = ("movie", "tv", "anime", "xanime")


def _maybe_track_show(created: "queue_store.QueueRequest") -> None:
    """On TV confirmation, upsert the identity-backed tracked_shows row so
    routing goes by show_id, not title strings (Task D item 1). Best-effort:
    intake must never break because the shows DB hiccuped. Anime keeps its own
    provider flow (its season picker is frozen), so this is TV-only here — the
    grab path still upserts for any episodic identity as a safety net."""
    try:
        if (created is None or created.media_type != "tv"
                or not created.is_qualified):
            return
        import shows_store
        shows_store.upsert_show(
            title=created.resolved_title or created.content, media_type="tv",
            source=created.identity_source, external_id=str(created.external_id),
            external_url=created.external_url, year=created.canonical_year)
    except Exception:  # never let show tracking break request intake
        pass


@dataclass(frozen=True)
class IntakeResult:
    """Outcome of one intake attempt, for surfaces that want to report it."""
    request_ids: tuple[int, ...]
    status: str
    batch_id: str | None = None
    # Subset of request_ids that were reused from an existing row (a dedupe
    # hit) rather than newly inserted — lets a caller tell a user "nothing
    # new was queued, you already have that" instead of claiming it added
    # something it didn't (Task F item 1).
    reused_ids: tuple[int, ...] = ()


# ---------------------------------------------------------------------------
# Alias construction
# ---------------------------------------------------------------------------

def build_aliases(
    resolved_title: str | None,
    content: str | None,
    *,
    origin_countries: list[str] | tuple[str, ...] = (),
    candidate_titles: list[str] | None = None,
) -> list[str]:
    """Compute the aliases stored on the request row.

    aliases[0] is always the SEARCH alias the query builders use (ASCII
    preferring, per media_identity.search_alias — this is what avoids querying
    indexers with a native-script canonical title).

    For a same-title country edition (another candidate shares the base title
    but a different edition — the MAFS AU vs US case) the disambiguating alias
    "<Base> <CC>" becomes the search alias so auto-grab queries pin the right
    edition. A lone country-tagged show (no same-title sibling) is NOT
    country-appended, so ordinary shows keep clean queries.
    """
    base = media_identity.search_alias(resolved_title, content)
    aliases = [base] if base else []

    countries = [str(c).strip().upper() for c in (origin_countries or []) if str(c).strip()]
    if not (base and countries):
        return aliases

    # Ambiguity signal: a sibling candidate with the same normalised title.
    base_norm = media_identity.normalize_title(base)
    siblings = [
        t for t in (candidate_titles or [])
        if media_identity.normalize_title(t) == base_norm
    ]
    if len(siblings) >= 2:  # the pick itself + at least one other edition
        cc = countries[0]
        disamb = f"{base} {cc}"
        if media_identity.normalize_title(disamb) != base_norm:
            # Disambiguating alias leads; the plain base stays available too.
            return [disamb, base]
    return aliases


# ---------------------------------------------------------------------------
# Identity dedupe (intake layer only — queue_store.add_request stays
# insert-always so the duplicate-detection path can still be exercised)
# ---------------------------------------------------------------------------

# States where an existing request still "covers" a new identical request, so a
# second intake should reference it rather than insert a duplicate row.
_DEDUPE_ACTIVE_STATES = (
    queue_store.STATUS_OPEN, queue_store.STATUS_GRABBING,
    queue_store.STATUS_DEFERRED, queue_store.STATUS_NEEDS_ATTENTION,
)


def find_existing_request(*, media_type: str, identity_source: str | None,
                          external_id: str | None, season: int | None):
    """An existing request to reuse instead of inserting a duplicate, matched on
    the durable identity key (media_type, identity_source, external_id, season).

    A request still being worked (open/grabbing/deferred/needs_attention) always
    wins. A fulfilled request wins only when its item is STILL in the library —
    re-requesting something we already have must not reopen work; but a fulfilled
    row whose file has since vanished does NOT block a fresh request. Season is
    part of the key, so S01 and S02 of the same show stay distinct. Returns None
    for an unqualified identity (nothing durable to match on)."""
    if not (identity_source and external_id):
        return None
    key_ext = str(external_id)
    for req in queue_store.list_requests(status="all", limit=1000):
        if (req.media_type != media_type
                or req.identity_source != identity_source
                or str(req.external_id) != key_ext
                or req.season != season):
            continue
        if req.status in _DEDUPE_ACTIVE_STATES:
            return req
        if req.status == queue_store.STATUS_FULFILLED:
            try:
                import maintenance
                if maintenance.request_present_in_library(req):
                    return req
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Adding rows
# ---------------------------------------------------------------------------

def add_needs_identity(
    content: str,
    requester: str,
    *,
    media_type: str = "unknown",
) -> queue_store.QueueRequest:
    """Record an unresolved request as needs_identity: visible, never grabbed.

    Used by surfaces that could not (or did not) resolve a provider identity —
    a bare /request, an 'Other' item the LLM typed as media but couldn't id, a
    desktop add the user didn't disambiguate. 'other' rows never come through
    here; they are added directly as an exempt deliberate choice.
    """
    return queue_store.add_request(
        content,
        requester,
        media_type=media_type,
        status=queue_store.STATUS_NEEDS_IDENTITY,
    )


def add_matched_request(
    content: str,
    requester: str,
    *,
    media_type: str,
    match,
    season: int | None = None,
    batch_id: str | None = None,
    candidate_titles: list[str] | None = None,
) -> queue_store.QueueRequest:
    """Add one request carrying a full provider-qualified identity.

    `match` is a media_lookup.MediaResult. If it lacks a source or external id
    the row is stored as needs_identity instead of open (the identity rule holds
    even here — a MediaResult with an empty external_id is not an identity).
    """
    row, _reused = add_matched_request_reporting(
        content, requester, media_type=media_type, match=match, season=season,
        batch_id=batch_id, candidate_titles=candidate_titles,
    )
    return row


def add_matched_request_reporting(
    content: str,
    requester: str,
    *,
    media_type: str,
    match,
    season: int | None = None,
    batch_id: str | None = None,
    candidate_titles: list[str] | None = None,
) -> tuple[queue_store.QueueRequest, bool]:
    """Same as add_matched_request, plus whether the dedupe path reused an
    existing row instead of inserting a new one.

    Callers that report outcomes to a user (the Telegram flow) need this: a
    dedupe hit means nothing new was actually queued, and saying "added"
    anyway is a lie by omission (Task F item 1) — e.g. the season picker's
    force-grab escape hatch (typing a season number that's already owned)
    reaches this exact path.
    """
    source = (getattr(match, "source", None) or "").strip() or None
    external_id = (str(getattr(match, "external_id", "") or "")).strip() or None
    if not (source and external_id):
        return add_needs_identity(content, requester, media_type=media_type), False

    # Dedupe: reference an existing request with the same identity instead of
    # piling up duplicate rows (the 2073 pileup). queue_store.add_request itself
    # stays insert-always; the guard lives here at the intake layer.
    existing = find_existing_request(
        media_type=media_type, identity_source=source,
        external_id=external_id, season=season)
    if existing is not None:
        return existing, True

    countries = list(getattr(match, "origin_countries", ()) or [])
    aliases = build_aliases(
        getattr(match, "title", None), content,
        origin_countries=countries, candidate_titles=candidate_titles,
    )
    created = queue_store.add_request(
        content,
        requester,
        media_type=media_type,
        resolved_title=getattr(match, "title", None),
        external_id=external_id,
        external_url=getattr(match, "external_url", None) or None,
        status=queue_store.STATUS_OPEN,
        identity_source=source,
        canonical_year=getattr(match, "year", None),
        origin_countries=countries or None,
        aliases=aliases or None,
        season=season,
        batch_id=batch_id,
    )
    _maybe_track_show(created)
    return created, False


def add_from_identity(
    content: str,
    requester: str,
    *,
    media_type: str,
    identity_source: str | None,
    external_id: str | None,
    resolved_title: str | None = None,
    external_url: str | None = None,
    canonical_year: int | None = None,
    origin_countries: list[str] | None = None,
    season: int | None = None,
    batch_id: str | None = None,
) -> queue_store.QueueRequest:
    """Add a row from an already-known provider identity (no MediaResult).

    Used by the Plex watchlist path, which parses a tmdb/tvdb GUID straight off
    the item instead of re-searching by title. Falls to needs_identity when the
    identity is not actually qualified.
    """
    source = (identity_source or "").strip() or None
    ext = (str(external_id or "")).strip() or None
    if not (source and ext):
        return add_needs_identity(content, requester, media_type=media_type)
    # Dedupe against an existing request for the same identity (watchlist path).
    existing = find_existing_request(
        media_type=media_type, identity_source=source,
        external_id=ext, season=season)
    if existing is not None:
        return existing
    aliases = build_aliases(
        resolved_title or content, content, origin_countries=origin_countries or [])
    created = queue_store.add_request(
        content,
        requester,
        media_type=media_type,
        resolved_title=resolved_title,
        external_id=ext,
        external_url=external_url,
        status=queue_store.STATUS_OPEN,
        identity_source=source,
        canonical_year=canonical_year,
        origin_countries=origin_countries or None,
        aliases=aliases or None,
        season=season,
        batch_id=batch_id,
    )
    _maybe_track_show(created)
    return created


def _mark_if_already_in_library(created) -> None:
    """Best-effort library gate for a freshly-queued qualified request: if the
    identity is already on disk, set found_in_library so auto-grab skips it. The
    watchlist path had NO library check, so titles the user already owned were
    re-queued and re-grabbed. Never raises — a library hiccup must not break
    intake."""
    try:
        if created is None or not created.is_qualified:
            return
        import maintenance
        if maintenance.request_present_in_library(created):
            queue_store.update_library_status(created.request_id, found=True)
    except Exception:
        pass


def queue_watchlist_item(item, requester, *, get_seasons=None) -> IntakeResult:
    """Queue one Plex watchlist item, honouring its parsed GUID identity.

    Movies with a GUID become qualified open rows. Shows with a GUID expand to
    one row per aired regular season (via get_seasons) under a shared batch_id;
    when the season list cannot be resolved the show is queued as a visible
    needs_identity row instead of a season=NULL whole-show TV row. Items with no
    usable GUID become needs_identity. `get_seasons` is injectable for tests.
    """
    media_type = "tv" if getattr(item, "item_type", "") == "show" else "movie"
    identity = getattr(item, "identity", None)
    title = getattr(item, "title", "") or ""
    year = getattr(item, "year", None)

    if not identity:
        created = add_needs_identity(title, requester, media_type=media_type)
        return IntakeResult((created.request_id,), created.status)

    source, ext = identity
    if media_type == "movie":
        created = add_from_identity(
            title, requester, media_type="movie",
            identity_source=source, external_id=ext,
            resolved_title=title, canonical_year=year)
        _mark_if_already_in_library(created)
        return IntakeResult((created.request_id,), created.status)

    # TV: enumerate aired seasons so we never store a season=NULL whole-show row.
    if get_seasons is None:
        import media_lookup
        get_seasons = media_lookup.get_show_seasons
    tmdb_id = getattr(item, "tmdb_id", None)
    seasons_data = get_seasons("tmdb", tmdb_id) if tmdb_id else get_seasons(source, ext)
    regular = list(getattr(seasons_data, "regular_seasons", ()) or [])
    if not (getattr(seasons_data, "resolved", False) and regular):
        # Can't enumerate seasons here (no interactive picker) — keep it visible
        # for the user to season it, rather than guess a whole-show grab.
        created = add_needs_identity(title, requester, media_type="tv")
        return IntakeResult((created.request_id,), created.status)

    batch_id = str(uuid.uuid4()) if len(regular) > 1 else None
    ids: list[int] = []
    status = queue_store.STATUS_OPEN
    for season in sorted(set(regular)):
        created = add_from_identity(
            title, requester, media_type="tv",
            identity_source=source, external_id=ext,
            resolved_title=title, canonical_year=year,
            season=season, batch_id=batch_id)
        _mark_if_already_in_library(created)
        ids.append(created.request_id)
        status = created.status
    return IntakeResult(tuple(ids), status, batch_id)


def resolve_request(request_id: int, match, *, season: int | None = None):
    """Resolve an existing needs_identity row to a picked MediaResult identity.

    The resolve path (Task A / Phase 2 gate) for legacy needs_identity rows: the
    user picks the exact title and the row gains identity_source + external_id +
    aliases and flips to 'open'. Returns the updated QueueRequest (or None).
    """
    row = queue_store.get_request(request_id)
    if row is None:
        return None
    source = (getattr(match, "source", None) or "").strip() or None
    ext = (str(getattr(match, "external_id", "") or "")).strip() or None
    countries = list(getattr(match, "origin_countries", ()) or [])
    aliases = build_aliases(getattr(match, "title", None), row.content,
                            origin_countries=countries)
    return queue_store.update_identity(
        request_id,
        media_type=getattr(match, "media_type", None) or row.media_type,
        resolved_title=getattr(match, "title", None),
        external_id=ext,
        external_url=getattr(match, "external_url", None) or None,
        identity_source=source,
        canonical_year=getattr(match, "year", None),
        origin_countries=countries or None,
        aliases=aliases or None,
        season=season,
    )


def search_candidates(content: str, media_type: str | None = None) -> list:
    """Look up candidate identities for a free-text request (network call).

    Used by the desktop Requests tab and the /request path to offer a picker
    instead of storing an untyped, unresolvable row. Searches movies and TV by
    default (the two things auto-grab actually fetches); a caller that already
    knows the type narrows the search. Import of media_lookup is deferred so
    surfaces that never resolve do not pay for it.
    """
    import media_lookup

    parsed = media_lookup.parse_request_list(content)
    if not parsed:
        return []
    req = parsed[0]
    out: list = []
    types = [media_type] if media_type else ["movie", "tv"]
    for mt in types:
        try:
            if mt == "movie":
                out.extend(media_lookup.search_tmdb_movies(req.title, req.year, limit=5))
            elif mt == "tv":
                tv = media_lookup.search_tvdb_shows(req.title, req.year, limit=5)
                out.extend(tv or media_lookup.search_tmdb_shows(req.title, req.year, limit=5))
            elif mt in ("anime", "xanime"):
                out.extend(media_lookup.search_jikan_anime(
                    req.title, explicit=(mt == "xanime"), limit=5))
        except Exception:  # network / parsing failure must not crash intake
            continue
    return out


def add_season_selection(
    content: str,
    requester: str,
    *,
    match,
    seasons: list[int],
    candidate_titles: list[str] | None = None,
) -> IntakeResult:
    """Expand a TV season choice into one request row per season.

    "All currently available" passes every aired regular season here; a single
    pick passes a one-element list. Two or more rows are tied together by one
    batch_id (a uuid) so each season can find/grab/retry independently while
    still being recognisable as one expansion. There is deliberately no
    season=NULL whole-show TV row.
    """
    ordered = sorted({int(s) for s in seasons})
    if not ordered:
        return IntakeResult((), queue_store.STATUS_NEEDS_IDENTITY)

    batch_id = str(uuid.uuid4()) if len(ordered) > 1 else None
    ids: list[int] = []
    reused: list[int] = []
    status = queue_store.STATUS_OPEN
    for season in ordered:
        created, was_reused = add_matched_request_reporting(
            content, requester, media_type="tv", match=match,
            season=season, batch_id=batch_id, candidate_titles=candidate_titles,
        )
        ids.append(created.request_id)
        if was_reused:
            reused.append(created.request_id)
        status = created.status
    return IntakeResult(tuple(ids), status, batch_id, tuple(reused))


# ---------------------------------------------------------------------------
# Desktop picker decision logic (desktop_app.py holds only Tk glue over these;
# desktop_app itself is not importable in CI, so the decisions live here)
# ---------------------------------------------------------------------------

def format_candidate_label(match) -> str:
    """One picker row: 'Title (Year) [CC] - SOURCE'."""
    title = getattr(match, "title", "") or "Unknown"
    year = getattr(match, "year", None)
    year_str = f" ({year})" if year else ""
    countries = list(getattr(match, "origin_countries", ()) or [])
    cc = "/".join(str(c).upper() for c in countries[:2])
    cc_str = f" [{cc}]" if cc else ""
    source = (getattr(match, "source", "") or "").upper()
    return f"{title}{year_str}{cc_str} - {source}"


def seasons_for_answer(answer, match=None, *, get_seasons=None) -> list[int] | None:
    """Parse a season-prompt answer into the season list to request.

    None (dialog cancelled) -> None (add nothing). 'all' / 'everything' / blank
    -> every aired regular season (falling back to [1] when the provider list
    is unavailable). Text containing a number -> that single season. Anything
    else -> [1]. `get_seasons` is injectable for tests; the default fetches
    live provider data (network).
    """
    if answer is None:
        return None
    text = str(answer).strip().lower()
    if text in ("all", "everything", ""):
        if get_seasons is None:
            import media_lookup
            get_seasons = media_lookup.get_show_seasons
        data = get_seasons(getattr(match, "source", None),
                           getattr(match, "external_id", None))
        regular = list(getattr(data, "regular_seasons", ()) or [])
        return sorted(set(int(s) for s in regular)) or [1]
    m = re.search(r"\d+", text)
    return [int(m.group())] if m else [1]


def parse_single_season(answer, default: int = 1) -> int:
    """First number in a free-text season answer, or `default`."""
    m = re.search(r"\d+", str(answer or ""))
    return int(m.group()) if m else default


def add_picked_candidate(
    content: str,
    requester: str,
    candidates: list,
    choice_index: int | None,
    *,
    seasons: list[int] | None = None,
) -> IntakeResult:
    """The desktop picker's decision table, UI-free.

    - choice_index out of range / None ("none of these"): needs_identity row.
    - tv pick: explicit season rows via add_season_selection (seasons required;
      an empty list means the season step was skipped, so the row stays
      needs_identity rather than becoming a season=NULL whole-show row).
    - movie/anime/xanime pick: one qualified row via add_matched_request.
    """
    if choice_index is None or not (0 <= choice_index < len(candidates)):
        created = add_needs_identity(content, requester)
        return IntakeResult((created.request_id,), created.status)

    match = candidates[choice_index]
    titles = [getattr(m, "title", "") for m in candidates]
    media_type = getattr(match, "media_type", None) or "unknown"

    if media_type == "tv":
        if not seasons:
            created = add_needs_identity(content, requester, media_type="tv")
            return IntakeResult((created.request_id,), created.status)
        return add_season_selection(
            content, requester, match=match, seasons=seasons,
            candidate_titles=titles)

    created, was_reused = add_matched_request_reporting(
        content, requester, media_type=media_type, match=match,
        candidate_titles=titles)
    reused = (created.request_id,) if was_reused else ()
    return IntakeResult((created.request_id,), created.status, reused_ids=reused)
