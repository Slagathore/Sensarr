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
# "Other" type bypasses the lookup pipeline and goes straight to Gemini
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
    LookupResult, MediaResult, ParsedRequest,
    check_library_for_title, clean_library_name, lookup_media,
    parse_request_list, title_similarity,
    search_tmdb_movies, search_tmdb_shows, search_tvdb_shows,
    search_jikan_anime, search_anidb,
)
from queue_store import add_request, format_requests_message_user

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

SELECT_TYPE = 0
AWAITING_CONTENT = 1
CONFIRMING = 2
CORRECTING = 3

# user_data keys
_UD_MEDIA_TYPE = "req_media_type"           # str: "movie" | "tv" | "anime" | "xanime"
_UD_RESULTS = "req_lookup_results"          # list[LookupResult]
_UD_CORRECTING_IDX = "req_correcting_idx"   # int: 0-based index of item being corrected
_UD_CORRECTION_OPTS = "req_correction_opts" # list[MediaResult]: candidates for correction
_UD_CORRECTION_QUEUE = "req_correction_queue"  # list[int]: 0-based indices still to fix
_UD_CORRECTION_PAGE  = "req_correction_page"   # int: current result page (0-based, 5 per page)
_UD_REMOVED          = "req_removed"           # set[int]: 0-based indices dropped by the user

_CORRECTION_PAGE_SIZE = 5
_CORRECTION_FETCH_LIMIT = 15  # fetch this many results upfront so paging works without extra calls

# ---------------------------------------------------------------------------
# Static keyboards
# ---------------------------------------------------------------------------

_TYPE_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🎬 Movie(s)",   callback_data="req_type_movie"),
        InlineKeyboardButton("📺 TV Show(s)", callback_data="req_type_tv"),
    ],
    [
        InlineKeyboardButton("🍜 Anime",   callback_data="req_type_anime"),
        InlineKeyboardButton("🔞 xAnime",  callback_data="req_type_xanime"),
    ],
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
            raw = lr.library_matches[0] if lr.library_matches else ""
            display = clean_library_name(raw) if raw else lr.request.display()
            lines.append(f"  <b>{num}.</b> <i>{display}</i>")
        lines.append("")

    if in_lib_qual:
        lines.append("⚠️ <b>In library — but you added a note (please verify):</b>")
        for num, lr in in_lib_qual:
            raw = lr.library_matches[0] if lr.library_matches else ""
            display = clean_library_name(raw) if raw else lr.request.display()
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
            title_link = f'<a href="{m.external_url}">{m.title}</a>' if m.external_url else f"<b>{m.title}</b>"
            lines.append(f"  <b>{num}.</b> {title_link}{qualifier_str}{year_str}  <i>[{m.source.upper()}]</i>")
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

    # Correction hint — shown whenever there's anything that could need fixing
    fixable = found_online + uncertain + nf_searched + in_lib_qual
    if fixable:
        lines.append(
            "💡 <i>If a match looks wrong, type its number followed by \"is wrong\" "
            "(e.g. <code>9 is wrong</code>) to search again.</i>\n"
        )

    submittable = [lr for i, lr in enumerate(results) if i not in _removed and not lr.in_library]
    if submittable:
        lines.append(
            "Tap <b>Submit Requests</b> to add the un-found title(s) to the queue, "
            "or <b>Start Over</b> to try again."
        )
    else:
        lines.append("Everything you asked for is already in the library! 🎉")

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
            reply_markup=_TYPE_KEYBOARD,
        )
    elif update.message is not None:
        await update.message.reply_text(
            "What would you like to request?",
            reply_markup=_TYPE_KEYBOARD,
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

    # ---- "Other" type: hand off to Gemini immediately ----------------------
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

        # Add to queue with what we know
        await loop.run_in_executor(
            None,
            lambda: add_request(
                raw_text,
                requester,
                media_type=category,
                resolved_title=guessed_title,
            ),
        )

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
                    # Swap best_match to the Gemini-preferred candidate
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
            reply_markup=_TYPE_KEYBOARD,
        )
        context.user_data.pop(_UD_RESULTS, None)
        context.user_data.pop(_UD_MEDIA_TYPE, None)
        context.user_data.pop(_UD_REMOVED, None)
        return SELECT_TYPE

    if data == "req_confirm_yes":
        results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
        media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "unknown")
        removed: set[int] = context.user_data.get(_UD_REMOVED, set())
        requester = _requester_name(update)

        loop = asyncio.get_running_loop()
        added: list[str] = []

        for i, lr in enumerate(results):
            if i in removed:
                continue  # user dropped this item
            if lr.in_library:
                continue  # already there — skip

            display = lr.request.display()
            match = lr.best_match

            def _add(lr=lr, match=match, display=display) -> None:
                add_request(
                    content=display,
                    requester=requester,
                    media_type=media_type,
                    resolved_title=match.title if match else None,
                    external_id=match.external_id if match else None,
                    external_url=match.external_url if match else None,
                )

            await loop.run_in_executor(None, _add)
            added.append(display)

        if added:
            bullet_list = "\n".join(f"• {t}" for t in added)
            await message.reply_html(
                f"✅ <b>Added {len(added)} request(s) to the queue:</b>\n{bullet_list}\n\n"
                f"Use the <b>📝 Requests</b> button anytime to see the full queue."
            )
        else:
            await message.reply_text(
                "Nothing new to add — everything was already in your library!"
            )

        context.user_data.pop(_UD_RESULTS, None)
        context.user_data.pop(_UD_MEDIA_TYPE, None)
        context.user_data.pop(_UD_REMOVED, None)
        return ConversationHandler.END

    # Unknown confirm action — just end
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
) -> str:
    """Format one page of candidates as an HTML string ready to send."""
    start = page * _CORRECTION_PAGE_SIZE
    page_items = candidates[start : start + _CORRECTION_PAGE_SIZE]
    has_more = (start + _CORRECTION_PAGE_SIZE) < len(candidates)

    lines = [f"🔍 Results for <b>{label}</b> (page {page + 1}):\n"]
    for i, m in enumerate(page_items, start=1):
        year_str = f" ({m.year})" if m.year else ""
        title_link = f'<a href="{m.external_url}">{m.title}</a>' if m.external_url else f"<b>{m.title}</b>"
        snippet = f" — <i>{m.overview[:80]}…</i>" if m.overview else ""
        lines.append(f"  <b>{i}.</b> {title_link}{year_str}{snippet}")

    footer_parts = ["Reply with a number (1–5) to select", "0 to leave as-is"]
    if has_more:
        footer_parts.append("<code>more</code> for next page")
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
        _format_candidates_page(search_label, candidates, 0)
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
            "Tap Submit Requests or Start Over, or type '<number> is wrong' "
            "to fix a specific item (e.g. '9 is wrong'), or 'remove 9' to drop one."
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
      • a digit      → pick that result from the current page
      • 0 / "skip"   → leave item unchanged, advance queue
      • "more"/"next" → show next page of candidates
      • anything else → treat as a new search term and re-search
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
            _format_candidates_page(lr.request.display(), candidates, next_page)
        )
        return CORRECTING

    # ── numeric pick ─────────────────────────────────────────────────────────
    try:
        pick = int(text_lower)
    except ValueError:
        pick = None

    if pick is not None:
        if pick != 0:
            page_start = page * _CORRECTION_PAGE_SIZE
            page_items = candidates[page_start : page_start + _CORRECTION_PAGE_SIZE]
            if pick < 1 or pick > len(page_items):
                await update.message.reply_text(
                    f"Please choose between 1 and {len(page_items)}, or 0 to skip."
                )
                return CORRECTING

            chosen = candidates[page_start + pick - 1]
            lr = results[correcting_idx]
            if lr.request.qualifier:
                chosen = MediaResult(
                    title=chosen.title, year=chosen.year,
                    external_id=chosen.external_id, external_url=chosen.external_url,
                    media_type=chosen.media_type, overview=chosen.overview,
                    source=chosen.source, qualifier=lr.request.qualifier,
                )

            # Re-check the library for the corrected title — it may already be there
            loop = asyncio.get_running_loop()
            in_lib, lib_matches = await loop.run_in_executor(
                None,
                lambda t=chosen.title: check_library_for_title(t, media_type),
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

    if context.user_data:
        context.user_data.pop(_UD_RESULTS, None)
        context.user_data.pop(_UD_MEDIA_TYPE, None)
        context.user_data.pop(_UD_REMOVED, None)

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
    },
    fallbacks=[
        CommandHandler("cancel", cancel_request_flow),
    ],
    allow_reentry=True,
    conversation_timeout=300,   # 5-minute idle timeout
)
