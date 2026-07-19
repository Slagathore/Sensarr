# =============================================================================
# request_flow.py
# =============================================================================
# Telegram ConversationHandler for the smart media request flow.
#
# Flow:
#   Entry ("📝 Requests" button)
#     ↓
#   SELECT_TYPE  — type-picker inline keyboard
#     ↓  (user picks Movie / TV / Anime / xAnime / Other / Queue)
#   AWAITING_CONTENT  — per-type instruction message; user types request(s)
#     ↓  (background: library check + external DB lookup)
#   CONFIRMING  — shows found/not-found results; user confirms or restarts
#     ↓
#   Requests added to DB  →  END
#
# "Other" type bypasses the lookup pipeline and goes straight to the LLM
# for categorisation, then ends the conversation immediately.
#
# Exposes:
#   REQUEST_CONV_HANDLER  — the ConversationHandler to register in telegram_service
# =============================================================================

import asyncio
import logging
import re
from typing import Any, cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from llm_service import categorize_other_request, fuzzy_correct_title, llm_available
from media_lookup import (
    LookupResult, MediaResult, ParsedRequest, ShowSeasons,
    check_library_for_title, clean_library_name, get_show_seasons, lookup_media,
    parse_request_list, title_similarity,
    search_tmdb_movies, search_tmdb_shows, search_tvdb_shows,
    search_jikan_anime, search_anidb, search_omdb_movies,
    get_tmdb_tv_status, get_tvdb_series_status, get_tmdb_next_air,
    get_anime_airing, get_jikan_status,
)
import request_intake
import shows_store
from queue_store import add_request, format_requests_message_user, get_request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

SELECT_TYPE = 0
AWAITING_CONTENT = 1
CONFIRMING = 2
CORRECTING = 3
SELECTING_SEASONS = 4

# user_data keys
_UD_MEDIA_TYPE = "req_media_type"           # str: "movie" | "tv" | "anime" | "xanime"
_UD_RESULTS = "req_lookup_results"          # list[LookupResult]
_UD_CORRECTING_IDX = "req_correcting_idx"   # int: 0-based index of item being corrected
_UD_CORRECTION_OPTS = "req_correction_opts" # list[MediaResult]: candidates for correction
_UD_CORRECTION_QUEUE = "req_correction_queue"  # list[int]: 0-based indices still to fix
_UD_CORRECTION_PAGE  = "req_correction_page"   # int: current result page (0-based, 5 per page)
_UD_REMOVED          = "req_removed"           # set[int]: 0-based indices dropped by the user
_UD_SEASON_QUEUE     = "req_season_queue"      # list[int]: 0-based TV indices still needing a season pick
_UD_SEASON_ADDED     = "req_season_added"      # list[str]: human summary of rows added so far
_UD_SEASON_ALREADY_HAVE = "req_season_already_have"  # list[str]: summaries of dedupe-skipped items

_CORRECTION_PAGE_SIZE = 5
_CORRECTION_FETCH_LIMIT = 15  # fetch this many results upfront so paging works without extra calls

# ---------------------------------------------------------------------------
# Static keyboards
# ---------------------------------------------------------------------------

def _type_keyboard() -> InlineKeyboardMarkup:
    anime_row = [InlineKeyboardButton("🍜 Anime", callback_data="req_type_anime")]
    if config.XANIME_ENABLED:
        anime_row.append(
            InlineKeyboardButton("🔞 xAnime", callback_data="req_type_xanime"))
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Movie(s)",   callback_data="req_type_movie"),
            InlineKeyboardButton("📺 TV Show(s)", callback_data="req_type_tv"),
        ],
        anime_row,
        [
            InlineKeyboardButton("❓ Other",       callback_data="req_type_other"),
            InlineKeyboardButton("📋 View Queue",  callback_data="req_type_queue"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="req_cancel"),
        ],
    ])

_CONFIRM_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Submit Requests", callback_data="req_confirm_yes"),
        InlineKeyboardButton("✏️ Start Over",       callback_data="req_confirm_restart"),
    ],
    [
        InlineKeyboardButton("❌ Cancel", callback_data="req_cancel"),
    ],
])

# ---------------------------------------------------------------------------
# Per-type instruction texts
# ---------------------------------------------------------------------------

_INSTRUCTIONS: dict[str, str] = {
    "movie": (
        "🎬 <b>What movie(s) would you like to request?</b>\n\n"
        "Separate multiple titles with a <b>comma</b>. "
        "If the title itself contains a comma, leave it out.\n"
        "You can add the release year in <b>parentheses</b> for better matching — optional.\n\n"
        "<code>Inception (2010), Dune Part Two, The Substance</code>\n\n"
        "Type your request below, or /cancel to quit."
    ),
    "tv": (
        "📺 <b>What TV show(s) would you like to request?</b>\n\n"
        "Separate multiple titles with a <b>comma</b>. "
        "Add the year in parentheses if you know it.\n\n"
        "<code>Severance, Shogun (2024), The Bear</code>\n\n"
        "Type your request below, or /cancel to quit."
    ),
    "anime": (
        "🍜 <b>What anime would you like to request?</b>\n\n"
        "Separate multiple titles with a <b>comma</b>. "
        "If you want a specific season or arc, put it in <b>[brackets]</b> "
        "<i>before</i> the comma.\n\n"
        "<code>Attack on Titan [Final Season], Frieren (2023), Solo Leveling</code>\n\n"
        "Type your request below, or /cancel to quit."
    ),
    "xanime": (
        "🔞 <b>What xAnime (adult anime) would you like to request?</b>\n\n"
        "Separate multiple titles with a <b>comma</b>. "
        "For a specific season or episode, put it in <b>[brackets]</b> "
        "<i>before</i> the comma.\n\n"
        "<code>Title Here [Part 2], Another Title</code>\n\n"
        "Type your request below, or /cancel to quit."
    ),
    "other": (
        "❓ <b>What are you looking for?</b>\n\n"
        "Describe it however you like — movie, show, game, software, music, "
        "something specific, or anything else. I'll do my best to figure it out.\n\n"
        "<code>That animated spider-man multiverse movie</code>\n\n"
        "Type your request below, or /cancel to quit."
    ),
}

_MEDIA_TYPE_LABEL: dict[str, str] = {
    "movie":  "movie",
    "tv":     "TV show",
    "anime":  "anime",
    "xanime": "xAnime",
    "other":  "request",
}

_MEDIA_TYPE_EMOJI: dict[str, str] = {
    "movie":  "🎬",
    "tv":     "📺",
    "anime":  "🍜",
    "xanime": "🔞",
    "other":  "❓",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _requester_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Unknown"
    if user.username:
        return f"@{user.username}"
    return user.full_name or "Unknown"


def _country_tag(m: MediaResult) -> str:
    """' [US]' style origin tag for the confirmation display, or '' when the
    provider gave no country. This is the signal that lets the user pick the
    right AU vs US edition (TVDB gives those distinct ids)."""
    countries = getattr(m, "origin_countries", ()) or ()
    if not countries:
        return ""
    return " [" + "/".join(str(c).upper() for c in countries[:2]) + "]"


def _display_for_in_lib(lr: LookupResult) -> str:
    """
    Pick the best display string for an in-library item.

    Why: when the user corrects a mismatched item and we re-check the library,
    Plex's search can return tangentially related entries (a search for
    "Reacher 2" came back showing "Jack Frost" because library_matches[0] was
    whatever Plex's hub search surfaced). The chosen external-DB title is the
    canonical name; prefer it over the library filename when set.
    """
    if lr.best_match is not None:
        return lr.best_match.title
    raw = lr.library_matches[0] if lr.library_matches else ""
    return clean_library_name(raw) if raw else lr.request.display()


def _build_results_message(
    results: list[LookupResult],
    media_type: str,
    *,
    removed: set[int] | None = None,
) -> str:
    """
    Build the numbered HTML confirmation message.

    Items are numbered 1–N in their original request order and that numbering
    is preserved across all sections so users can reference them by number
    (e.g. "9 is wrong").

    Sections:
        ✅  Already in your library
        ⚠️  In library but qualifier noted (e.g. [english dub])
        🔍  Not in library — found online
        ❓  Uncertain match — please verify
        ❌  Not found in any database
        ⚙️  External search unavailable (API key not configured)
    """
    # Bucket each result WITH its 1-based display number
    in_lib:        list[tuple[int, LookupResult]] = []
    in_lib_qual:   list[tuple[int, LookupResult]] = []
    found_online:  list[tuple[int, LookupResult]] = []
    uncertain:     list[tuple[int, LookupResult]] = []
    nf_searched:   list[tuple[int, LookupResult]] = []
    nf_no_key:     list[tuple[int, LookupResult]] = []

    _removed = removed or set()

    for num, lr in enumerate(results, start=1):
        if num - 1 in _removed:
            continue
        if lr.in_library:
            if lr.request.qualifier:
                in_lib_qual.append((num, lr))
            else:
                in_lib.append((num, lr))
        elif lr.best_match is not None:
            sim = title_similarity(lr.request.title, lr.best_match.title)
            if sim >= 0.55:
                found_online.append((num, lr))
            else:
                uncertain.append((num, lr))
        else:
            if lr.search_attempted:
                nf_searched.append((num, lr))
            else:
                nf_no_key.append((num, lr))

    lines: list[str] = []
    label = _MEDIA_TYPE_LABEL.get(media_type, "request")
    emoji = _MEDIA_TYPE_EMOJI.get(media_type, "📝")
    lines.append(f"{emoji} <b>Here's what I found for your {label} request(s):</b>\n")

    if in_lib:
        lines.append("✅ <b>Already in your library:</b>")
        for num, lr in in_lib:
            display = _display_for_in_lib(lr)
            lines.append(f"  <b>{num}.</b> <i>{display}</i>")
        lines.append("")

    if in_lib_qual:
        lines.append("⚠️ <b>In library — but you added a note (please verify):</b>")
        for num, lr in in_lib_qual:
            display = _display_for_in_lib(lr)
            lines.append(
                f"  <b>{num}.</b> <i>{display}</i>"
                f" — you asked for <code>[{lr.request.qualifier}]</code>"
            )
        lines.append("")

    if found_online:
        lines.append("🔍 <b>Not in library — found online:</b>")
        for num, lr in found_online:
            m = lr.best_match
            assert m is not None
            year_str = f" ({m.year})" if m.year else ""
            qualifier_str = f" [{m.qualifier}]" if m.qualifier else ""
            country_str = _country_tag(m)
            title_link = f'<a href="{m.external_url}">{m.title}</a>' if m.external_url else f"<b>{m.title}</b>"
            lines.append(
                f"  <b>{num}.</b> {title_link}{qualifier_str}{year_str}{country_str}"
                f"  <i>[{m.source.upper()}]</i>")
        lines.append("")

    if uncertain:
        lines.append("❓ <b>Uncertain match — please verify:</b>")
        for num, lr in uncertain:
            m = lr.best_match
            assert m is not None
            title_link = f'<a href="{m.external_url}">{m.title}</a>' if m.external_url else f"<b>{m.title}</b>"
            lines.append(
                f'  <b>{num}.</b> You asked for "<i>{lr.request.display()}</i>"'
                f" → found {title_link}"
            )
        lines.append("")

    if nf_searched:
        lines.append("❌ <b>Not found in any database:</b>")
        for num, lr in nf_searched:
            lines.append(f"  <b>{num}.</b> {lr.request.display()}")
        lines.append("")

    if nf_no_key:
        lines.append("⚙️ <b>External search unavailable (API key not configured):</b>")
        for num, lr in nf_no_key:
            lines.append(f"  <b>{num}.</b> {lr.request.display()}")
        lines.append(
            "<i>Ask your admin to add TMDB_API_KEY / TVDB_API_KEY to the .env file.</i>"
        )
        lines.append("")

    # Correction hint — shown whenever anything is still on the list. This
    # deliberately includes clean ✅ in-library matches: the library check is
    # fuzzy, so "already in your library" can be a false positive, and the
    # user needs to know they can dispute it (the correction handlers already
    # accept any item number).
    fixable = in_lib + in_lib_qual + found_online + uncertain + nf_searched
    if fixable:
        lines.append(
            "💡 <i>Did I match something wrong — even one marked ✅ in-library? "
            "Type its number followed by \"is wrong\" "
            "(e.g. <code>9 is wrong</code>) to search again.\n"
            "To drop an item entirely, type <code>remove 9</code> "
            "(or <code>remove 9 and 11</code>).</i>\n"
        )

    submittable = [lr for i, lr in enumerate(results) if i not in _removed and not lr.in_library]
    if submittable:
        lines.append(
            "Tap <b>Submit Requests</b> to add the un-found title(s) to the queue, "
            "or <b>Start Over</b> to try again."
        )
    else:
        lines.append(
            "Everything you asked for is already in the library! 🎉\n"
            "<i>If that doesn't look right for one of them, type its number "
            "+ \"is wrong\" and I'll take another look.</i>"
        )

    return "\n".join(lines)


def _has_submittable(results: list[LookupResult], removed: set[int] | None = None) -> bool:
    """True if at least one non-removed result is not already in the library."""
    _removed = removed or set()
    return any(not lr.in_library for i, lr in enumerate(results) if i not in _removed)


# ---------------------------------------------------------------------------
# Entry point — show type-picker keyboard
# ---------------------------------------------------------------------------

async def start_request_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: show the media-type picker keyboard."""
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.message.reply_text(  # type: ignore[union-attr]
            "What would you like to request?",
            reply_markup=_type_keyboard(),
        )
    elif update.message is not None:
        await update.message.reply_text(
            "What would you like to request?",
            reply_markup=_type_keyboard(),
        )
    return SELECT_TYPE


# ---------------------------------------------------------------------------
# SELECT_TYPE state
# ---------------------------------------------------------------------------

async def handle_type_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User picked a media type (or Queue/Cancel)."""
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    message = cast(Message, query.message)
    await query.answer()

    data = query.data or ""
    # e.g. "req_type_movie" → media_type = "movie"
    media_type = data.removeprefix("req_type_")

    if media_type == "queue":
        queue_text = format_requests_message_user()
        await message.reply_text(queue_text)
        return ConversationHandler.END

    # Store chosen type and send instructions
    assert context.user_data is not None
    context.user_data[_UD_MEDIA_TYPE] = media_type

    instruction = _INSTRUCTIONS.get(media_type, _INSTRUCTIONS["other"])
    await message.reply_html(instruction)
    return AWAITING_CONTENT


# ---------------------------------------------------------------------------
# AWAITING_CONTENT state
# ---------------------------------------------------------------------------

async def handle_content_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User has typed their request(s). Parse, look up, show results."""
    if update.message is None or update.message.text is None:
        return AWAITING_CONTENT

    assert context.user_data is not None
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "other")
    raw_text = update.message.text.strip()

    # ---- "Other" type: hand off to the LLM immediately ---------------------
    if media_type == "other":
        await update.message.reply_text("Got it! Analysing your request…")
        loop = asyncio.get_running_loop()
        requester = _requester_name(update)

        analysis = await loop.run_in_executor(
            None,
            lambda: categorize_other_request(raw_text),
        )

        category = analysis.get("category", "other")
        guessed_title = analysis.get("title")
        reasoning = analysis.get("reasoning", "")
        flagged = analysis.get("flagged", False)

        # Identity rule holds even here: if the LLM decided this is actually a
        # movie/tv/anime but we have no external id for it, the row is
        # needs_identity (visible, never auto-grabbed as that type) rather than
        # an open typed row with no identity — the exact #85 shape. A genuinely
        # 'other' guess (game/software/music/…) is an exempt deliberate choice
        # and is queued as-is.
        def _add_other(cat=category, title=guessed_title) -> None:
            if cat in ("movie", "tv", "anime", "xanime"):
                request_intake.add_needs_identity(
                    raw_text, requester, media_type=cat)
            else:
                add_request(
                    raw_text, requester,
                    media_type=cat, resolved_title=title,
                )

        await loop.run_in_executor(None, _add_other)

        flag_note = " ⚠️ <b>[Flagged for review]</b>" if flagged else ""
        category_emoji = _MEDIA_TYPE_EMOJI.get(category, "❓")
        reply = (
            f"✅ Got it — queued your request!\n\n"
            f"{category_emoji} Looks like a <b>{category}</b>"
            + (f": <i>{guessed_title}</i>" if guessed_title else "")
            + f"\n<i>{reasoning}</i>{flag_note}"
        )
        await update.message.reply_html(reply)
        return ConversationHandler.END

    # ---- Structured types: parse + look up ---------------------------------
    parsed_requests = parse_request_list(raw_text)
    if not parsed_requests:
        await update.message.reply_text(
            "I couldn't parse any titles from that. "
            "Separate multiple titles with commas and try again."
        )
        return AWAITING_CONTENT

    label = _MEDIA_TYPE_LABEL.get(media_type, "request")
    await update.message.reply_text(
        f"Searching for {len(parsed_requests)} {label}(s)… "
        f"(this may take a few seconds)"
    )

    # Send typing indicator
    await update.message.chat.send_action("typing")

    loop = asyncio.get_running_loop()
    results: list[LookupResult] = await loop.run_in_executor(
        None,
        lambda: [lookup_media(pr, media_type) for pr in parsed_requests],
    )

    # Optional: ask Ollama to correct obviously wrong matches
    if llm_available():
        for lr in results:
            if (
                lr.best_match is not None
                and title_similarity(lr.request.title, lr.best_match.title) < 0.55
                and lr.external_matches
            ):
                candidates = [m.title for m in lr.external_matches[:5]]
                corrected = await loop.run_in_executor(
                    None,
                    lambda c=candidates, q=lr.request.title: fuzzy_correct_title(q, c),
                )
                if corrected:
                    # Swap best_match to the LLM-preferred candidate
                    for m in lr.external_matches:
                        if m.title == corrected:
                            lr.best_match = m
                            break

    context.user_data[_UD_RESULTS] = results

    # Build confirmation message
    results_msg = _build_results_message(results, media_type)
    keyboard = _CONFIRM_KEYBOARD if _has_submittable(results) else InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Make another request", callback_data="req_confirm_restart"),
        InlineKeyboardButton("❌ Done", callback_data="req_cancel"),
    ]])

    await update.message.reply_html(results_msg, reply_markup=keyboard)
    return CONFIRMING


# ---------------------------------------------------------------------------
# CONFIRMING state
# ---------------------------------------------------------------------------

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User confirmed or restarted."""
    query = update.callback_query
    if query is None or query.message is None:
        return ConversationHandler.END
    message = cast(Message, query.message)
    await query.answer()

    data = query.data or ""
    assert context.user_data is not None

    if data == "req_confirm_restart":
        await message.reply_text(
            "No problem — what would you like to request?",
            reply_markup=_type_keyboard(),
        )
        _clear_flow_state(context)
        return SELECT_TYPE

    if data == "req_confirm_yes":
        results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
        media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "unknown")
        removed: set[int] = context.user_data.get(_UD_REMOVED, set())

        submittable = [
            i for i, lr in enumerate(results)
            if i not in removed and not lr.in_library
        ]

        # TV goes through the per-item season picker (Task A item 4, enriched
        # in Task F) so each submitted show becomes explicit season rows,
        # never a whole-show row. Anime/xanime get the Task F picker too
        # (RESOLVED DECISION 11 unfrozen for this sprint): TMDB/TVDB season
        # enumeration doesn't apply to Jikan/AniList/AniDB entries, so they
        # take the simpler add/keep-updated prompt in _prompt_next_anime
        # instead of a season grid.
        if media_type == "tv" and submittable:
            context.user_data[_UD_SEASON_QUEUE] = submittable
            context.user_data[_UD_SEASON_ADDED] = []
            return await _prompt_next_season(update, context)

        if media_type in ("anime", "xanime") and submittable:
            context.user_data[_UD_SEASON_QUEUE] = submittable
            context.user_data[_UD_SEASON_ADDED] = []
            return await _prompt_next_anime(update, context)

        # Everything else (movie, or anime/xanime with nothing submittable)
        # is added now, carrying its full provider-qualified identity.
        requester = _requester_name(update)
        loop = asyncio.get_running_loop()
        added, already_have = await loop.run_in_executor(
            None,
            lambda: _add_non_season_items(results, submittable, media_type, requester),
        )

        reply_parts: list[str] = []
        if added:
            bullet_list = "\n".join(f"• {t}" for t in added)
            reply_parts.append(f"✅ <b>Added {len(added)} request(s) to the queue:</b>\n{bullet_list}")
        if already_have:
            bullet_list = "\n".join(f"• {t}" for t in already_have)
            reply_parts.append(
                f"📚 <b>Already in your library, nothing new queued ({len(already_have)}):</b>\n{bullet_list}"
            )
        if reply_parts:
            reply_parts.append("Use the <b>📝 Requests</b> button anytime to see the full queue.")
            await message.reply_html("\n\n".join(reply_parts))
        else:
            await message.reply_text(
                "Nothing new to add. Everything was already in your library."
            )

        _clear_flow_state(context)
        return ConversationHandler.END

    # Unknown confirm action — just end
    return ConversationHandler.END


def _add_non_season_items(
    results: list[LookupResult],
    submittable: list[int],
    media_type: str,
    requester: str,
) -> tuple[list[str], list[str]]:
    """Add movie/anime/xanime rows with their full identity (blocking).

    Returns (added, already_have): already_have is populated when the intake
    dedupe path reused an existing row instead of inserting one, so the
    caller can say honestly that nothing new was queued for that item
    (Task F item 1).
    """
    added: list[str] = []
    already_have: list[str] = []
    for i in submittable:
        lr = results[i]
        display = lr.request.display()
        match = lr.best_match
        candidate_titles = [m.title for m in lr.external_matches]
        if match is not None:
            _row, reused = request_intake.add_matched_request_reporting(
                display, requester, media_type=media_type, match=match,
                candidate_titles=candidate_titles,
            )
            if reused:
                already_have.append(f"{display} is already in your library.")
                continue
        else:
            # No external match at all — visible needs_identity, not grabbed.
            request_intake.add_needs_identity(
                display, requester, media_type=media_type)
        added.append(display)
    return added, already_have


def _clear_flow_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data:
        return
    for key in (_UD_RESULTS, _UD_MEDIA_TYPE, _UD_REMOVED,
                _UD_SEASON_QUEUE, _UD_SEASON_ADDED, _UD_SEASON_ALREADY_HAVE,
                _UD_SEASON_CURRENT, _UD_SEASON_DATA):
        context.user_data.pop(key, None)


# ---------------------------------------------------------------------------
# SELECTING_SEASONS state — TV season picker (Task A item 4, enriched by
# Task F) + the anime/xanime add-or-follow prompt (Task F item 4)
# ---------------------------------------------------------------------------
# "Grab everything missing" expands into one request row per aired regular
# season that isn't already owned, under one batch_id; a single pick makes
# one row; Specials (S00) is an explicit opt-in button, never folded into the
# missing-seasons grab. Anime/xanime have no per-season provider data (a
# Jikan/AniList/AniDB entry is one cour, not a season list), so they take the
# simpler prompt in _prompt_next_anime instead of a season grid.

_UD_SEASON_CURRENT = "req_season_current"   # int: 0-based index being seasoned
_UD_SEASON_DATA    = "req_season_data"      # dict: {"regular","missing","have","specials","resolved"}

_SEASON_BUTTONS_PER_ROW = 4

# TMDB/TVDB status strings -> a short friendly label for the context line.
_STATUS_LABELS: dict[str, str] = {
    "Returning Series": "airing",
    "Continuing":       "airing",
    "Ended":            "ended",
    "Canceled":         "cancelled",
    "Cancelled":        "cancelled",
    "In Production":    "upcoming",
    "Planned":          "upcoming",
    "Pilot":            "upcoming",
    "Upcoming":         "upcoming",
}

_JIKAN_STATUS_LABELS: dict[str, str] = {
    "Currently Airing": "airing",
    "Finished Airing":  "ended",
    "Not yet aired":    "upcoming",
}


async def _safe_fetch(loop, fn, default):
    """Run a blocking metadata fetch in the executor; any exception (not just
    the ones the fetcher already catches internally) degrades to `default`
    rather than blocking the season/anime picker. Defense in depth on top of
    the try/except already inside each _tv_status/_owned_seasons/etc.
    helper — Task F requires the picker to survive a metadata failure no
    matter which layer it happens at."""
    try:
        return await loop.run_in_executor(None, fn)
    except Exception:
        logger.debug("Metadata fetch failed — degrading to default.", exc_info=True)
        return default


async def _reply_html(update: Update, text: str,
                      keyboard: InlineKeyboardMarkup | None = None) -> None:
    """Reply with HTML from either a callback-query or a text-message update."""
    query = update.callback_query
    if query is not None and query.message is not None:
        await cast(Message, query.message).reply_html(text, reply_markup=keyboard)
    elif update.message is not None:
        await update.message.reply_html(text, reply_markup=keyboard)


def _format_season_ranges(seasons: list[int] | set[int]) -> str:
    """[1,2,3,5] -> 'S01-S03, S05' — used for the have/missing context clause."""
    ordered = sorted(set(seasons))
    if not ordered:
        return ""
    ranges: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for s in ordered[1:]:
        if s == prev + 1:
            prev = s
            continue
        ranges.append((start, prev))
        start = prev = s
    ranges.append((start, prev))
    return ", ".join(
        f"S{a:02d}" if a == b else f"S{a:02d}-S{b:02d}" for a, b in ranges)


def _friendly_date(iso: str | None) -> str:
    """'2026-08-02' -> 'Aug 2'. Empty/unparseable input -> ''."""
    if not iso:
        return ""
    try:
        from datetime import date as _date
        y, m, d = (int(x) for x in str(iso)[:10].split("-"))
        return f"{_date(y, m, d).strftime('%b')} {d}"
    except Exception:
        return ""


def _tv_status(match: MediaResult) -> tuple[str, str]:
    """(status_label, next_air_display) for a TV match, best-effort.

    Never raises — a metadata hiccup must not block the season picker. Only
    fetches next-air for TMDB-sourced shows (the only source with a reliable
    next_episode_to_air field); TVDB-sourced shows still get a status label,
    just without a specific next-episode date.
    """
    try:
        source = getattr(match, "source", None)
        ext = str(getattr(match, "external_id", "") or "")
        if not ext:
            return "", ""
        raw = ""
        next_air = ""
        if source == "tmdb":
            raw = get_tmdb_tv_status(ext)
            status_label = _STATUS_LABELS.get(raw, "")
            if status_label == "airing":
                nxt = get_tmdb_next_air(ext)
                if nxt is not None:
                    next_air = _friendly_date(nxt.air_date)
        elif source == "tvdb":
            raw = get_tvdb_series_status(ext)
            status_label = _STATUS_LABELS.get(raw, "")
        else:
            status_label = ""
        return status_label, next_air
    except Exception:
        logger.debug("TV status/airing fetch failed for %r", getattr(match, "title", "?"),
                    exc_info=True)
        return "", ""


def _owned_seasons(match: MediaResult) -> set[int]:
    """Regular seasons already on disk for this show's tracked-show row, or
    an empty set when untracked or on any lookup failure."""
    try:
        source = getattr(match, "source", None)
        ext = str(getattr(match, "external_id", "") or "")
        if not (source and ext):
            return set()
        show = shows_store.get_show_by_identity(source, ext)
        if show is None:
            return set()
        return shows_store.have_seasons(show.show_id)
    except Exception:
        logger.debug("Owned-season lookup failed for %r", getattr(match, "title", "?"),
                    exc_info=True)
        return set()


def _season_context_line(*, total: int | None, status_label: str, next_air: str,
                         have: list[int], missing: list[int]) -> str:
    """One context line: total seasons + airing status always when known,
    plus whichever of have/missing is the shorter list to state."""
    parts: list[str] = []
    if total is not None and total > 0:
        parts.append(f"{total} season{'s' if total != 1 else ''}")
    if status_label:
        if status_label == "airing" and next_air:
            parts.append(f"airing, next episode {next_air}")
        else:
            parts.append(status_label)
    line = ", ".join(parts)

    have_missing = ""
    if have and missing:
        if len(have) <= len(missing):
            have_missing = f"You have {_format_season_ranges(have)}."
        else:
            have_missing = f"Missing {_format_season_ranges(missing)}."
    elif have and not missing:
        have_missing = f"You have all of it ({_format_season_ranges(have)})."

    if line and have_missing:
        line = f"{line}. {have_missing}"
    elif have_missing:
        line = have_missing
    elif line:
        line = f"{line}."
    if line:
        line = line[0].upper() + line[1:]
    return line


def _season_keyboard(regular: list[int], have: list[int], *, show_grab_all: bool,
                     has_specials: bool, airing: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    have_set = set(have)
    if show_grab_all:
        rows.append([InlineKeyboardButton(
            "📥 Grab everything missing", callback_data="req_season_all")])
    btn_row: list[InlineKeyboardButton] = []
    for s in regular:
        if s in have_set:
            # Owned — shown as a marked, non-actionable label, never omitted.
            btn_row.append(InlineKeyboardButton(
                f"✅ S{s:02d}", callback_data=f"req_season_owned_{s}"))
        else:
            btn_row.append(InlineKeyboardButton(
                f"S{s:02d}", callback_data=f"req_season_pick_{s}"))
        if len(btn_row) == _SEASON_BUTTONS_PER_ROW:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)
    if has_specials:
        rows.append([InlineKeyboardButton(
            "🎞️ S00 Specials", callback_data="req_season_specials")])
    if airing:
        rows.append([InlineKeyboardButton(
            "🔔 Keep it updated", callback_data="req_season_keep_updated")])
    rows.append([InlineKeyboardButton(
        "⏭️ Skip this show", callback_data="req_season_skip")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="req_cancel")])
    return InlineKeyboardMarkup(rows)


def _all_owned_keyboard() -> InlineKeyboardMarkup:
    """Nothing missing — only offer to follow new seasons, plus skip/cancel."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Keep it updated", callback_data="req_season_keep_updated")],
        [InlineKeyboardButton("⏭️ Skip this show", callback_data="req_season_skip")],
        [InlineKeyboardButton("❌ Cancel", callback_data="req_cancel")],
    ])


async def _prompt_next_season(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the season keyboard for the next queued TV item, or finish."""
    assert context.user_data is not None
    queue: list[int] = context.user_data.get(_UD_SEASON_QUEUE, [])
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    requester = _requester_name(update)

    while queue:
        idx = queue[0]
        queue = queue[1:]
        context.user_data[_UD_SEASON_QUEUE] = queue
        lr = results[idx]
        match = lr.best_match
        if match is None or not (match.source and str(match.external_id or "").strip()):
            # No usable identity — store needs_identity and move on (visible,
            # not grabbed). Cannot offer a season grid without an identity.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda d=lr.request.display(): request_intake.add_needs_identity(
                    d, requester, media_type="tv"))
            _record_season_added(context, f"{lr.request.display()} (needs identity)")
            continue

        context.user_data[_UD_SEASON_CURRENT] = idx
        loop = asyncio.get_running_loop()
        seasons_data = await _safe_fetch(
            loop, lambda m=match: get_show_seasons(m.source, m.external_id),
            ShowSeasons(resolved=False))

        regular = list(seasons_data.regular_seasons)
        show_grab_all = bool(seasons_data.resolved and regular)
        if seasons_data.resolved and not regular:
            regular = [1]  # resolved but nothing aired yet — offer S01
        if not seasons_data.resolved:
            regular = list(range(1, 11))  # provider unavailable — manual range

        # Status/airing and owned-seasons are independent, best-effort fetches
        # — either failing must never block the picker (Task F).
        status_label, next_air = await _safe_fetch(
            loop, lambda m=match: _tv_status(m), ("", ""))
        if seasons_data.resolved:
            have_raw = await _safe_fetch(loop, lambda m=match: _owned_seasons(m), set())
            have = sorted(s for s in have_raw if s in regular)
        else:
            have = []
        missing = sorted(set(regular) - set(have))

        context.user_data[_UD_SEASON_DATA] = {
            "regular": regular,
            "missing": missing,
            "have": have,
            "specials": seasons_data.has_specials,
            "resolved": seasons_data.resolved,
        }

        note = ("" if seasons_data.resolved else
                "\n<i>(couldn't fetch the season list — pick a season number "
                "or type one, e.g. <code>3</code>)</i>")

        if seasons_data.resolved and regular and not missing:
            # Fully owned — nothing to grab, only the follow-new option.
            await _reply_html(
                update,
                f"📺 <b>{match.title}</b>: you already have all of it "
                f"({_format_season_ranges(regular)}).",
                _all_owned_keyboard())
            return SELECTING_SEASONS

        context_line = _season_context_line(
            total=(len(regular) if seasons_data.resolved else None),
            status_label=status_label, next_air=next_air,
            have=have, missing=missing)
        header = f"📺 <b>{match.title}</b>"
        if context_line:
            header += f": {context_line}"
        keyboard = _season_keyboard(
            regular, have, show_grab_all=show_grab_all,
            has_specials=seasons_data.has_specials, airing=(status_label == "airing"))
        await _reply_html(
            update,
            f"{header}\nTap <b>Grab everything missing</b> or a single season.{note}",
            keyboard)
        return SELECTING_SEASONS

    return await _finish_season_flow(update, context)


def _record_season_added(context: ContextTypes.DEFAULT_TYPE, summary: str) -> None:
    assert context.user_data is not None
    added: list[str] = context.user_data.get(_UD_SEASON_ADDED, [])
    added.append(summary)
    context.user_data[_UD_SEASON_ADDED] = added


def _record_already_have(context: ContextTypes.DEFAULT_TYPE, summary: str) -> None:
    """Record an item the dedupe path reused instead of inserting — shown to
    the user as a separate "already have" bucket, never folded into "added"
    (Task F item 1: a dedupe hit is not a new request)."""
    assert context.user_data is not None
    already_have: list[str] = context.user_data.get(_UD_SEASON_ALREADY_HAVE, [])
    already_have.append(summary)
    context.user_data[_UD_SEASON_ALREADY_HAVE] = already_have


async def _apply_season_choice(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               *, seasons: list[int], label: str) -> None:
    """Add the chosen season rows for the current TV item.

    Some of the requested seasons may already be owned — the season-picker's
    own escape hatch (typing/tapping a specific season number) can reach a
    season that's marked owned, and "Grab everything missing" can still
    dedupe-hit if ownership changed since the prompt was built. Those seasons
    are reported honestly as "already have", never claimed as added
    (Task F item 1).
    """
    assert context.user_data is not None
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    idx: int = context.user_data.get(_UD_SEASON_CURRENT, -1)
    requester = _requester_name(update)
    lr = results[idx]
    match = lr.best_match
    if match is None:  # guarded upstream in _prompt_next_season; defensive
        return
    if not seasons:
        return
    candidate_titles = [m.title for m in lr.external_matches]
    display = lr.request.display()

    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        None,
        lambda: request_intake.add_season_selection(
            display, requester, match=match, seasons=seasons,
            candidate_titles=candidate_titles),
    )
    reused_ids = set(outcome.reused_ids)
    new_ids = [i for i in outcome.request_ids if i not in reused_ids]
    if new_ids:
        tag = "" if outcome.status != "needs_identity" else " (needs identity)"
        _record_season_added(context, f"{match.title}: {label}{tag}")
    if reused_ids:
        reused_seasons = await loop.run_in_executor(
            None, lambda: _seasons_for_ids(reused_ids))
        season_txt = _format_season_ranges(reused_seasons) if reused_seasons else "that season"
        _record_already_have(
            context, f"{match.title} {season_txt} is already in your library.")


def _seasons_for_ids(request_ids: set[int]) -> list[int]:
    """Season numbers for a set of request ids — best-effort, for the
    'already have' message. Never raises."""
    seasons: list[int] = []
    for rid in request_ids:
        try:
            row = get_request(rid)
        except Exception:
            row = None
        if row is not None and row.season is not None:
            seasons.append(row.season)
    return seasons


def _ensure_tracked_show(match: MediaResult, media_type: str) -> int | None:
    """Upsert (idempotent) the tracked_shows row for this identity and return
    its show_id, or None on failure. Used by 'Keep it updated' for both TV
    and anime/xanime — matches what _maybe_track_show does for a TV
    confirmation, just callable ahead of any season row being added."""
    try:
        source = getattr(match, "source", None)
        ext = str(getattr(match, "external_id", "") or "")
        if not (source and ext):
            return None
        return shows_store.upsert_show(
            title=getattr(match, "title", "") or "unknown",
            media_type=media_type, source=source, external_id=ext,
            external_url=getattr(match, "external_url", None) or None,
            year=getattr(match, "year", None))
    except Exception:
        logger.debug("Show tracking failed for %r", getattr(match, "title", "?"), exc_info=True)
        return None


async def _apply_keep_updated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'Keep it updated' for the current TV item: track the show (if not
    already) and turn on auto_grab so newly-aired seasons get picked up
    automatically. Adds no request rows of its own."""
    assert context.user_data is not None
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    idx: int = context.user_data.get(_UD_SEASON_CURRENT, -1)
    if not (0 <= idx < len(results)):
        return
    match = results[idx].best_match
    if match is None:
        return
    loop = asyncio.get_running_loop()
    show_id = await loop.run_in_executor(None, lambda: _ensure_tracked_show(match, "tv"))
    if show_id is None:
        _record_season_added(
            context, f"{match.title}: couldn't set up auto-follow, try again later.")
        return
    await loop.run_in_executor(None, lambda: shows_store.set_show_auto_grab(show_id, True))
    _record_season_added(
        context,
        f"{match.title}: keeping it updated. New seasons will be grabbed "
        "automatically as they air.")


async def handle_season_selection(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback handler for the season keyboard and the anime add/keep-updated
    keyboard (both live in the SELECTING_SEASONS state)."""
    query = update.callback_query
    if query is None:
        return SELECTING_SEASONS
    assert context.user_data is not None
    data = query.data or ""

    if data.startswith("req_season_owned_"):
        # Tapping a marked/owned season is a no-op — nothing to grab there.
        await query.answer("You already have that season.")
        return SELECTING_SEASONS

    await query.answer()
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "tv")

    if media_type in ("anime", "xanime"):
        if data == "req_season_anime_add":
            await _apply_anime_add(update, context)
        elif data == "req_season_anime_keep_updated":
            await _apply_anime_keep_updated(update, context)
        elif data == "req_season_skip":
            pass
        else:
            return SELECTING_SEASONS
        return await _prompt_next_anime(update, context)

    season_data: dict = context.user_data.get(_UD_SEASON_DATA, {})
    regular: list[int] = list(season_data.get("regular", []))
    missing: list[int] = list(season_data.get("missing", regular))

    if data == "req_season_skip":
        pass  # add nothing for this item
    elif data == "req_season_all":
        await _apply_season_choice(
            update, context, seasons=(missing or regular), label="everything missing")
    elif data == "req_season_specials":
        await _apply_season_choice(update, context, seasons=[0], label="Specials")
    elif data.startswith("req_season_pick_"):
        try:
            season = int(data.removeprefix("req_season_pick_"))
        except ValueError:
            return SELECTING_SEASONS
        await _apply_season_choice(
            update, context, seasons=[season], label=f"S{season:02d}")
    elif data == "req_season_keep_updated":
        await _apply_keep_updated(update, context)
    else:
        return SELECTING_SEASONS

    return await _prompt_next_season(update, context)


async def handle_season_text(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    """Text fallback in SELECTING_SEASONS: 'all'/'missing', a season number,
    'specials', 'keep updated', 'skip' for TV; 'add'/'keep updated'/'skip'
    for anime/xanime. Lets the picker work even when the provider list
    couldn't load, or the user prefers typing."""
    if update.message is None or update.message.text is None:
        return SELECTING_SEASONS
    assert context.user_data is not None
    text = update.message.text.strip().lower()
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "tv")

    if media_type in ("anime", "xanime"):
        if text in {"skip", "next"}:
            return await _prompt_next_anime(update, context)
        if text in {"add", "yes", "grab", "missing", "queue"}:
            await _apply_anime_add(update, context)
            return await _prompt_next_anime(update, context)
        if text in {"keep", "keep updated", "keep it updated", "follow", "update"}:
            await _apply_anime_keep_updated(update, context)
            return await _prompt_next_anime(update, context)
        await update.message.reply_text(
            "Type 'add' to queue it, 'keep updated' to follow new episodes, or 'skip'.")
        return SELECTING_SEASONS

    season_data: dict = context.user_data.get(_UD_SEASON_DATA, {})
    regular: list[int] = list(season_data.get("regular", []))
    missing: list[int] = list(season_data.get("missing", regular))

    if text in {"skip", "next"}:
        return await _prompt_next_season(update, context)
    if text in {"all", "everything"}:
        await _apply_season_choice(
            update, context, seasons=regular, label="all currently available")
        return await _prompt_next_season(update, context)
    if text == "missing":
        await _apply_season_choice(
            update, context, seasons=(missing or regular), label="everything missing")
        return await _prompt_next_season(update, context)
    if text in {"specials", "s0", "s00", "0"}:
        await _apply_season_choice(update, context, seasons=[0], label="Specials")
        return await _prompt_next_season(update, context)
    if text in {"keep updated", "keep it updated", "follow"}:
        await _apply_keep_updated(update, context)
        return await _prompt_next_season(update, context)
    m = re.search(r"\d+", text)
    if m:
        season = int(m.group())
        await _apply_season_choice(
            update, context, seasons=[season], label=f"S{season:02d}")
        return await _prompt_next_season(update, context)

    await update.message.reply_text(
        "Type a season number (e.g. 3), 'all', 'missing', 'specials', "
        "'keep updated', or 'skip'.")
    return SELECTING_SEASONS


# ---------------------------------------------------------------------------
# Anime/xAnime add-or-follow prompt (Task F item 4) — no season grid: a
# Jikan/AniList/AniDB entry is one cour, not a season list, so this degrades
# to the simpler prompt the per-provider metadata can actually support.
# ---------------------------------------------------------------------------

def _anime_status(match: MediaResult, *, explicit: bool) -> tuple[str, str]:
    """(status_label, next_air_display) for an anime/xanime match, best-effort.

    AniList (by title) is the primary source; Jikan's own status field is a
    fallback for MAL-identified entries when AniList has nothing. Never
    raises — a metadata hiccup must not block the picker.
    """
    try:
        title = getattr(match, "title", "") or ""
        if not title:
            return "", ""
        nxt, raw_status = get_anime_airing(title, explicit=explicit)
        status_label = raw_status.lower() if raw_status else ""
        next_air = _friendly_date(nxt.air_date) if nxt is not None else ""
        if not status_label and getattr(match, "source", None) == "jikan":
            raw = get_jikan_status(str(getattr(match, "external_id", "") or ""))
            status_label = _JIKAN_STATUS_LABELS.get(raw, "")
        return status_label, next_air
    except Exception:
        logger.debug("Anime status fetch failed for %r", getattr(match, "title", "?"),
                    exc_info=True)
        return "", ""


def _anime_owned_counts(match: MediaResult) -> tuple[int, int]:
    """(have_count, episode_count) from the tracked show, or (0, 0) when
    untracked or on any lookup failure."""
    try:
        source = getattr(match, "source", None)
        ext = str(getattr(match, "external_id", "") or "")
        if not (source and ext):
            return 0, 0
        show = shows_store.get_show_by_identity(source, ext)
        if show is None:
            return 0, 0
        return show.have_count, show.episode_count
    except Exception:
        logger.debug("Anime owned-count lookup failed for %r", getattr(match, "title", "?"),
                    exc_info=True)
        return 0, 0


def _anime_context_line(status_label: str, next_air: str, have: int, total: int) -> str:
    parts: list[str] = []
    if status_label:
        if status_label == "airing" and next_air:
            parts.append(f"airing, next episode {next_air}")
        else:
            parts.append(status_label)
    line = ", ".join(parts)
    if total > 0:
        owned = (f"You have {have} of {total} episodes." if have
                else f"You don't have any of the {total} episodes yet.")
        line = f"{line}. {owned}" if line else owned
    elif line:
        line = f"{line}."
    if line:
        line = line[0].upper() + line[1:]
    return line


def _anime_keyboard(*, airing: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("➕ Add to queue", callback_data="req_season_anime_add")]]
    if airing:
        rows.append([InlineKeyboardButton(
            "🔔 Keep it updated", callback_data="req_season_anime_keep_updated")])
    rows.append([InlineKeyboardButton("⏭️ Skip this title", callback_data="req_season_skip")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="req_cancel")])
    return InlineKeyboardMarkup(rows)


async def _prompt_next_anime(update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the add/keep-updated prompt for the next queued anime/xanime
    item, or finish. Mirrors _prompt_next_season's queue-draining shape."""
    assert context.user_data is not None
    queue: list[int] = context.user_data.get(_UD_SEASON_QUEUE, [])
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "anime")
    requester = _requester_name(update)

    while queue:
        idx = queue[0]
        queue = queue[1:]
        context.user_data[_UD_SEASON_QUEUE] = queue
        lr = results[idx]
        match = lr.best_match
        if match is None or not (match.source and str(match.external_id or "").strip()):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda d=lr.request.display(): request_intake.add_needs_identity(
                    d, requester, media_type=media_type))
            _record_season_added(context, f"{lr.request.display()} (needs identity)")
            continue

        context.user_data[_UD_SEASON_CURRENT] = idx
        loop = asyncio.get_running_loop()
        status_label, next_air = await _safe_fetch(
            loop, lambda m=match: _anime_status(m, explicit=(media_type == "xanime")),
            ("", ""))
        have, total = await _safe_fetch(
            loop, lambda m=match: _anime_owned_counts(m), (0, 0))

        context_line = _anime_context_line(status_label, next_air, have, total)
        emoji = "🔞" if media_type == "xanime" else "🍜"
        header = f"{emoji} <b>{match.title}</b>"
        if context_line:
            header += f": {context_line}"
        keyboard = _anime_keyboard(airing=(status_label == "airing"))
        await _reply_html(update, header, keyboard)
        return SELECTING_SEASONS

    return await _finish_season_flow(update, context)


async def _apply_anime_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add the current anime/xanime item to the queue (its full identity, no
    season split, matches how anime rows have always been added). Reports
    honestly when the item was already in the queue/library instead of
    claiming it was added (Task F item 1)."""
    assert context.user_data is not None
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    idx: int = context.user_data.get(_UD_SEASON_CURRENT, -1)
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "anime")
    requester = _requester_name(update)
    if not (0 <= idx < len(results)):
        return
    lr = results[idx]
    match = lr.best_match
    if match is None:
        return
    candidate_titles = [m.title for m in lr.external_matches]
    display = lr.request.display()
    loop = asyncio.get_running_loop()
    _row, reused = await loop.run_in_executor(
        None,
        lambda: request_intake.add_matched_request_reporting(
            display, requester, media_type=media_type, match=match,
            candidate_titles=candidate_titles),
    )
    if reused:
        _record_already_have(context, f"{display} is already in your library.")
    else:
        _record_season_added(context, display)


async def _apply_anime_keep_updated(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'Keep it updated' for the current anime/xanime item: track the show
    and turn on auto_grab. Adds no request row of its own."""
    assert context.user_data is not None
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    idx: int = context.user_data.get(_UD_SEASON_CURRENT, -1)
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "anime")
    if not (0 <= idx < len(results)):
        return
    match = results[idx].best_match
    if match is None:
        return
    loop = asyncio.get_running_loop()
    show_id = await loop.run_in_executor(
        None, lambda: _ensure_tracked_show(match, media_type))
    if show_id is None:
        _record_season_added(
            context, f"{match.title}: couldn't set up auto-follow, try again later.")
        return
    await loop.run_in_executor(None, lambda: shows_store.set_show_auto_grab(show_id, True))
    _record_season_added(
        context,
        f"{match.title}: keeping it updated. New episodes will be grabbed "
        "automatically as they air.")


async def _finish_season_flow(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> int:
    """Wrap up the season/anime picker: report what was actually queued and,
    separately, what was skipped because it's already owned (Task F items 1
    and 3, plain counted wording instead of a vague 'updated' header)."""
    assert context.user_data is not None
    added: list[str] = context.user_data.get(_UD_SEASON_ADDED, [])
    already_have: list[str] = context.user_data.get(_UD_SEASON_ALREADY_HAVE, [])
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "tv")
    unit = "season" if media_type == "tv" else "item"

    reply_parts: list[str] = []
    if added:
        bullet_list = "\n".join(f"• {t}" for t in added)
        noun = unit + ("s" if len(added) != 1 else "")
        reply_parts.append(f"✅ <b>Queued {len(added)} {noun}:</b>\n{bullet_list}")
    if already_have:
        bullet_list = "\n".join(f"• {t}" for t in already_have)
        noun = unit + ("s" if len(already_have) != 1 else "")
        reply_parts.append(
            f"📚 <b>Already had {len(already_have)} {noun}, nothing new queued:</b>\n{bullet_list}")

    if reply_parts:
        reply_parts.append("Use the <b>📝 Requests</b> button anytime to see the full queue.")
        await _reply_html(update, "\n\n".join(reply_parts))
    else:
        await _reply_html(update, "Nothing added. No items were selected.")
    _clear_flow_state(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Correction flow helpers
# ---------------------------------------------------------------------------

_WRONG_KEYWORDS = frozenset({
    "wrong", "fix", "incorrect", "bad", "off", "redo", "retry", "recheck", "not right",
})


def _parse_remove_numbers(text: str) -> list[int]:
    """
    Extract item numbers from messages like 'remove 9', 'drop 9 and 11'.
    Returns an empty list if no remove keyword is present.
    """
    lower = text.lower().strip()
    if not re.match(r"^(remove|delete|drop|ditch|scratch)\b", lower):
        return []
    return [int(m.group()) for m in re.finditer(r"\b(\d+)\b", lower) if int(m.group()) >= 1]


def _parse_wrong_numbers(text: str) -> list[int]:
    """
    Extract item numbers from messages like '9 is wrong', 'fix 9 and 11'.
    Returns an empty list if no correction keyword is found.
    """
    lower = text.lower()
    if not any(kw in lower for kw in _WRONG_KEYWORDS):
        return []
    return [int(m.group()) for m in re.finditer(r"\b(\d+)\b", lower) if int(m.group()) >= 1]


def _run_external_search(
    request: ParsedRequest,
    media_type: str,
    *,
    limit: int = _CORRECTION_FETCH_LIMIT,
) -> list[MediaResult]:
    """Force an external DB search regardless of library state. Blocking — run in executor."""
    if media_type == "movie":
        return search_tmdb_movies(request.title, request.year, limit=limit)
    if media_type == "tv":
        results = search_tvdb_shows(request.title, request.year, limit=limit)
        return results or search_tmdb_shows(request.title, request.year, limit=limit)
    if media_type == "anime":
        return search_jikan_anime(request.title, explicit=False, limit=limit)
    if media_type == "xanime":
        return search_anidb(request.title) or search_jikan_anime(request.title, explicit=True, limit=limit)
    return []


# ---------------------------------------------------------------------------
# CONFIRMING state — text handler for "X is wrong" / "9 and 11 are wrong"
# ---------------------------------------------------------------------------

def _format_candidates_page(
    label: str,
    candidates: list[MediaResult],
    page: int,
    media_type: str,
) -> str:
    """Format one page of candidates as an HTML string ready to send.

    Numbering is absolute across pages (1..len(candidates)), so the user can
    pick any item from any page without re-paging — e.g. they flip to page 3,
    decide #2 from page 1 was right, and just type `2`.
    """
    start = page * _CORRECTION_PAGE_SIZE
    page_items = candidates[start : start + _CORRECTION_PAGE_SIZE]
    total_pages = (len(candidates) + _CORRECTION_PAGE_SIZE - 1) // _CORRECTION_PAGE_SIZE
    has_more = (start + _CORRECTION_PAGE_SIZE) < len(candidates)

    lines = [f"🔍 Results for <b>{label}</b> (page {page + 1}/{total_pages}):\n"]
    for offset, m in enumerate(page_items):
        abs_num = start + offset + 1
        year_str = f" ({m.year})" if m.year else ""
        title_link = f'<a href="{m.external_url}">{m.title}</a>' if m.external_url else f"<b>{m.title}</b>"
        snippet = f" — <i>{m.overview[:80]}…</i>" if m.overview else ""
        lines.append(f"  <b>{abs_num}.</b> {title_link}{year_str}{snippet}")

    footer_parts = [f"Reply with a number (1–{len(candidates)}) to select"]
    if has_more:
        footer_parts.append("<code>more</code> for next page")
    if page > 0:
        footer_parts.append("<code>back</code> for previous page")
    footer_parts.append("<code>0</code> to leave as-is")
    footer_parts.append("<code>remove</code> to drop this item")
    if media_type in ("anime", "xanime"):
        other_label = "xAnime DBs" if media_type == "anime" else "regular anime DB"
        footer_parts.append(f"<code>other db</code> to search {other_label}")
    elif media_type == "movie" and config.OMDB_API_KEY:
        footer_parts.append("<code>other db</code> to search OMDB / IMDB")
    footer_parts.append("or type a different search term")
    lines.append("\n" + ", ".join(footer_parts) + ".")
    return "\n".join(lines)


async def _search_and_show_candidates(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    item_num: int,          # 1-based display number
    results: list[LookupResult],
    media_type: str,
    *,
    override_title: str | None = None,  # use a different search term if provided
) -> bool:
    """
    Run external search for item_num (or override_title), store all candidates,
    show page 0. Returns True if candidates were found, False otherwise.
    """
    assert context.user_data is not None
    lr = results[item_num - 1]
    search_label = override_title or lr.request.display()

    await update.message.reply_html(  # type: ignore[union-attr]
        f"🔄 Searching for <b>{search_label}</b>…"
    )
    await update.message.chat.send_action("typing")  # type: ignore[union-attr]

    # Build a request with potentially overridden title for the search
    search_req = lr.request
    if override_title:
        search_req = ParsedRequest(
            original=override_title,
            title=override_title,
            year=lr.request.year,
            qualifier=lr.request.qualifier,
        )

    loop = asyncio.get_running_loop()
    candidates: list[MediaResult] = await loop.run_in_executor(
        None,
        lambda: _run_external_search(search_req, media_type),
    )

    if not candidates:
        await update.message.reply_html(  # type: ignore[union-attr]
            f"No results found for <b>{search_label}</b>.\n"
            "Try a different search term, or reply <code>0</code> to leave this item as-is."
        )
        return False

    context.user_data[_UD_CORRECTING_IDX] = item_num - 1
    context.user_data[_UD_CORRECTION_OPTS] = candidates
    context.user_data[_UD_CORRECTION_PAGE] = 0

    await update.message.reply_html(  # type: ignore[union-attr]
        _format_candidates_page(search_label, candidates, 0, media_type)
    )
    return True


async def handle_correction_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed something like '9 is wrong', '9 and 11 are wrong', or 'remove 9'."""
    if update.message is None or update.message.text is None:
        return CONFIRMING

    assert context.user_data is not None
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "other")
    removed: set[int] = context.user_data.get(_UD_REMOVED, set())

    # ── "remove X" — drop items from the list ────────────────────────────────
    remove_numbers = _parse_remove_numbers(update.message.text)
    if remove_numbers:
        newly_removed = []
        for n in remove_numbers:
            if n < 1 or n > len(results):
                await update.message.reply_text(
                    f"Item {n} doesn't exist — there are {len(results)} items."
                )
            elif (n - 1) not in removed:
                removed.add(n - 1)
                newly_removed.append(n)
        if newly_removed:
            context.user_data[_UD_REMOVED] = removed
            removed_list = ", ".join(f"#{n}" for n in newly_removed)
            await update.message.reply_text(f"Removed {removed_list} from your request.")
            results_msg = _build_results_message(results, media_type, removed=removed)
            keyboard = _CONFIRM_KEYBOARD if _has_submittable(results, removed) else InlineKeyboardMarkup([[
                InlineKeyboardButton("✏️ Make another request", callback_data="req_confirm_restart"),
                InlineKeyboardButton("❌ Done", callback_data="req_cancel"),
            ]])
            await update.message.reply_html(results_msg, reply_markup=keyboard)
        return CONFIRMING

    # ── "X is wrong" — start correction flow ─────────────────────────────────
    wrong_numbers = _parse_wrong_numbers(update.message.text)
    if not wrong_numbers:
        await update.message.reply_text(
            "Tap Submit Requests or Start Over, or:\n"
            "• type '<number> is wrong' to fix a match (e.g. '9 is wrong')\n"
            "• type 'remove <number>' to drop an item (e.g. 'remove 9' or 'remove 9 and 11')"
        )
        return CONFIRMING

    # Validate and deduplicate, preserving order
    valid: list[int] = []
    for n in wrong_numbers:
        if n < 1 or n > len(results):
            await update.message.reply_text(
                f"Item {n} doesn't exist — there are {len(results)} items."
            )
        elif n not in valid:
            valid.append(n)

    if not valid:
        return CONFIRMING

    context.user_data[_UD_CORRECTION_QUEUE] = valid[1:]
    found = await _search_and_show_candidates(update, context, valid[0], results, media_type)
    return CORRECTING if found else CONFIRMING


# ---------------------------------------------------------------------------
# CORRECTING state — user picks a candidate, then moves to next in queue
# ---------------------------------------------------------------------------

async def handle_correction_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    In CORRECTING state, handle:
      • a digit (1..len(candidates)) → pick that result by absolute index
      • 0 / "skip"            → leave item unchanged, advance queue
      • "more" / "next"       → show next page of candidates
      • "back" / "previous"   → show previous page
      • "remove" / "delete"   → drop this item from the request
      • "other db" / "switch" → re-run the search against the alternate DB
                                (anime ↔ xanime; otherwise no-op)
      • anything else         → treat as a new search term and re-search
    """
    if update.message is None or update.message.text is None:
        return CORRECTING

    assert context.user_data is not None
    results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
    correcting_idx: int = context.user_data.get(_UD_CORRECTING_IDX, -1)
    candidates: list[MediaResult] = context.user_data.get(_UD_CORRECTION_OPTS, [])
    page: int = context.user_data.get(_UD_CORRECTION_PAGE, 0)
    queue: list[int] = context.user_data.get(_UD_CORRECTION_QUEUE, [])
    media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "other")

    text = update.message.text.strip()
    text_lower = text.lower()

    # ── "remove" / "delete" — drop this item entirely ────────────────────────
    if text_lower in {"remove", "delete", "drop", "ditch", "scratch"}:
        removed: set[int] = context.user_data.get(_UD_REMOVED, set())
        removed.add(correcting_idx)
        context.user_data[_UD_REMOVED] = removed
        context.user_data.pop(_UD_CORRECTING_IDX, None)
        context.user_data.pop(_UD_CORRECTION_OPTS, None)
        context.user_data.pop(_UD_CORRECTION_PAGE, None)
        await update.message.reply_text(f"Removed #{correcting_idx + 1} from your request.")
        if queue:
            next_num = queue[0]
            context.user_data[_UD_CORRECTION_QUEUE] = queue[1:]
            found = await _search_and_show_candidates(update, context, next_num, results, media_type)
            return CORRECTING if found else await _finish_corrections(update, context, results, media_type)
        return await _finish_corrections(update, context, results, media_type)

    # ── "more" / "next" — advance to the next page of stored candidates ──────
    if text_lower in {"more", "next"}:
        next_page = page + 1
        if next_page * _CORRECTION_PAGE_SIZE >= len(candidates):
            await update.message.reply_text(
                "No more results. Try a different search term, or pick from the list above."
            )
            return CORRECTING
        context.user_data[_UD_CORRECTION_PAGE] = next_page
        lr = results[correcting_idx]
        await update.message.reply_html(
            _format_candidates_page(lr.request.display(), candidates, next_page, media_type)
        )
        return CORRECTING

    # ── "back" / "previous" — go to previous page ────────────────────────────
    if text_lower in {"back", "prev", "previous"}:
        if page == 0:
            await update.message.reply_text("Already on the first page.")
            return CORRECTING
        prev_page = page - 1
        context.user_data[_UD_CORRECTION_PAGE] = prev_page
        lr = results[correcting_idx]
        await update.message.reply_html(
            _format_candidates_page(lr.request.display(), candidates, prev_page, media_type)
        )
        return CORRECTING

    # ── "other db" / "switch" — re-run search against the alternate DB ───────
    if text_lower in {"other db", "switch db", "switch", "different db", "alt db", "alternate db"}:
        lr = results[correcting_idx]
        loop = asyncio.get_running_loop()
        new_candidates: list[MediaResult] = []
        switched_label = ""
        if media_type == "anime":
            switched_label = "xAnime DBs"
            new_candidates = await loop.run_in_executor(
                None,
                lambda q=lr.request.title: search_anidb(q) or search_jikan_anime(
                    q, explicit=True, limit=_CORRECTION_FETCH_LIMIT
                ),
            )
        elif media_type == "xanime":
            switched_label = "regular anime DB"
            new_candidates = await loop.run_in_executor(
                None,
                lambda q=lr.request.title: search_jikan_anime(
                    q, explicit=False, limit=_CORRECTION_FETCH_LIMIT
                ),
            )
        elif media_type == "movie":
            # Movies fall through to OMDB (IMDB-backed) when configured. If
            # the user hasn't set OMDB_API_KEY yet, point them at Settings.
            if not config.OMDB_API_KEY:
                await update.message.reply_text(
                    "No alternate movie database is configured. "
                    "Add an OMDB_API_KEY (free at omdbapi.com) in the "
                    "Settings tab and try again, or type a different "
                    "search term to retry against TMDB."
                )
                return CORRECTING
            switched_label = "OMDB / IMDB"
            new_candidates = await loop.run_in_executor(
                None,
                lambda q=lr.request.title, y=lr.request.year:
                    search_omdb_movies(q, y, limit=_CORRECTION_FETCH_LIMIT),
            )
        else:
            await update.message.reply_text(
                "No alternate database is configured for this media type. "
                "Try typing a different search term instead."
            )
            return CORRECTING

        if not new_candidates:
            await update.message.reply_text(
                f"No results from the {switched_label} either."
            )
            return CORRECTING

        context.user_data[_UD_CORRECTION_OPTS] = new_candidates
        context.user_data[_UD_CORRECTION_PAGE] = 0
        await update.message.reply_html(
            _format_candidates_page(
                f"{lr.request.display()} ({switched_label})",
                new_candidates,
                0,
                media_type,
            )
        )
        return CORRECTING

    # ── numeric pick (absolute index across pages) ───────────────────────────
    try:
        pick = int(text_lower)
    except ValueError:
        pick = None

    if pick is not None:
        if pick != 0:
            if pick < 1 or pick > len(candidates):
                await update.message.reply_text(
                    f"Please choose between 1 and {len(candidates)}, or 0 to skip."
                )
                return CORRECTING

            chosen = candidates[pick - 1]
            lr = results[correcting_idx]
            if lr.request.qualifier:
                chosen = MediaResult(
                    title=chosen.title, year=chosen.year,
                    external_id=chosen.external_id, external_url=chosen.external_url,
                    media_type=chosen.media_type, overview=chosen.overview,
                    source=chosen.source, qualifier=lr.request.qualifier,
                )

            # Re-check the library against the canonical chosen title.
            # Strict mode here — the chosen title is from an external DB, so
            # we want exact-ish matches, not the loose fuzzy/substring/word-
            # fallback logic that produces false positives like "Reacher 2"
            # matching "Jack Frost".
            loop = asyncio.get_running_loop()
            in_lib, lib_matches = await loop.run_in_executor(
                None,
                lambda t=chosen.title: check_library_for_title(t, media_type, strict=True),
            )
            if in_lib:
                await update.message.reply_html(
                    f"✅ <b>{chosen.title}</b> is actually already in your library! "
                    "Marking it as found."
                )
                results[correcting_idx] = LookupResult(
                    request=lr.request, in_library=True, library_matches=lib_matches,
                    external_matches=candidates, best_match=chosen, search_attempted=True,
                )
            else:
                results[correcting_idx] = LookupResult(
                    request=lr.request, in_library=False, library_matches=[],
                    external_matches=candidates, best_match=chosen, search_attempted=True,
                )
            context.user_data[_UD_RESULTS] = results

        # Clear correction state for this item
        context.user_data.pop(_UD_CORRECTING_IDX, None)
        context.user_data.pop(_UD_CORRECTION_OPTS, None)
        context.user_data.pop(_UD_CORRECTION_PAGE, None)

        if queue:
            next_num = queue[0]
            context.user_data[_UD_CORRECTION_QUEUE] = queue[1:]
            found = await _search_and_show_candidates(update, context, next_num, results, media_type)
            return CORRECTING if found else await _finish_corrections(update, context, results, media_type)

        return await _finish_corrections(update, context, results, media_type)

    # ── free-text → re-search with new query ─────────────────────────────────
    await _search_and_show_candidates(
        update, context, correcting_idx + 1, results, media_type,
        override_title=text,
    )
    return CORRECTING


async def _finish_corrections(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    results: list[LookupResult],
    media_type: str,
) -> int:
    """Clear correction state and redisplay the full confirmation."""
    assert context.user_data is not None
    context.user_data.pop(_UD_CORRECTION_QUEUE, None)
    removed: set[int] = context.user_data.get(_UD_REMOVED, set())
    results_msg = _build_results_message(results, media_type, removed=removed)
    keyboard = _CONFIRM_KEYBOARD if _has_submittable(results, removed) else InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Make another request", callback_data="req_confirm_restart"),
        InlineKeyboardButton("❌ Done", callback_data="req_cancel"),
    ]])
    await update.message.reply_html(results_msg, reply_markup=keyboard)  # type: ignore[union-attr]
    return CONFIRMING


# ---------------------------------------------------------------------------
# Cancel / fallback
# ---------------------------------------------------------------------------

async def cancel_request_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel from a command (/cancel) or the Cancel button."""
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.message.reply_text("Request cancelled.")  # type: ignore[union-attr]
    elif update.message is not None:
        await update.message.reply_text("Request cancelled.")

    _clear_flow_state(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler — register this in telegram_service.build_application()
# ---------------------------------------------------------------------------

REQUEST_CONV_HANDLER = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_request_flow, pattern=r"^cmd_requests$"),
    ],
    states={
        SELECT_TYPE: [
            CallbackQueryHandler(handle_type_selection, pattern=r"^req_type_"),
            CallbackQueryHandler(cancel_request_flow, pattern=r"^req_cancel$"),
        ],
        AWAITING_CONTENT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_content_input),
        ],
        CONFIRMING: [
            CallbackQueryHandler(handle_confirmation, pattern=r"^req_confirm_"),
            CallbackQueryHandler(cancel_request_flow, pattern=r"^req_cancel$"),
            # Text in CONFIRMING means the user is requesting a correction ("9 is wrong")
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_correction_request),
        ],
        CORRECTING: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_correction_pick),
            # Allow the confirm/cancel buttons to still work if tapped from old message
            CallbackQueryHandler(handle_confirmation, pattern=r"^req_confirm_"),
            CallbackQueryHandler(cancel_request_flow, pattern=r"^req_cancel$"),
        ],
        SELECTING_SEASONS: [
            CallbackQueryHandler(handle_season_selection, pattern=r"^req_season_"),
            CallbackQueryHandler(cancel_request_flow, pattern=r"^req_cancel$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_season_text),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_request_flow),
    ],
    allow_reentry=True,
    conversation_timeout=300,   # 5-minute idle timeout
)
