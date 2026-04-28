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
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from media_lookup import LookupResult, ParsedRequest, lookup_media, parse_request_list, title_similarity
from queue_store import add_request, format_requests_message_user

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

SELECT_TYPE = 0
AWAITING_CONTENT = 1
CONFIRMING = 2

# user_data keys
_UD_MEDIA_TYPE = "req_media_type"       # str: "movie" | "tv" | "anime" | "xanime"
_UD_RESULTS = "req_lookup_results"      # list[LookupResult]

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
) -> str:
    """
    Build the HTML confirmation message that shows the user what was found.

    Sections:
        ✅ Already in your library
        🔍 Not in library — found online  (hyperlinks to DB)
        ⚠️  Uncertain match — please verify
        ❌ Not found anywhere
    """
    in_library: list[LookupResult] = []
    found_online: list[LookupResult] = []
    uncertain: list[LookupResult] = []
    not_found: list[LookupResult] = []

    for lr in results:
        if lr.in_library:
            in_library.append(lr)
        elif lr.best_match is not None:
            sim = title_similarity(lr.request.title, lr.best_match.title)
            if sim >= 0.55:
                found_online.append(lr)
            else:
                uncertain.append(lr)
        else:
            not_found.append(lr)

    lines: list[str] = []
    label = _MEDIA_TYPE_LABEL.get(media_type, "request")
    emoji = _MEDIA_TYPE_EMOJI.get(media_type, "📝")
    lines.append(f"{emoji} <b>Here's what I found for your {label} request(s):</b>\n")

    if in_library:
        lines.append("✅ <b>Already in your library:</b>")
        for lr in in_library:
            display = lr.library_matches[0] if lr.library_matches else lr.request.display()
            lines.append(f"  • <i>{display}</i>")
        lines.append("")

    if found_online:
        lines.append("🔍 <b>Not in library — found online:</b>")
        for lr in found_online:
            m = lr.best_match
            assert m is not None
            year_str = f" ({m.year})" if m.year else ""
            qualifier_str = f" [{m.qualifier}]" if m.qualifier else ""
            if m.external_url:
                title_link = f'<a href="{m.external_url}">{m.title}</a>'
            else:
                title_link = f"<b>{m.title}</b>"
            lines.append(f"  • {title_link}{qualifier_str}{year_str}  <i>[{m.source.upper()}]</i>")
        lines.append("")

    if uncertain:
        lines.append("⚠️ <b>Uncertain match — please verify:</b>")
        for lr in uncertain:
            m = lr.best_match
            assert m is not None
            if m.external_url:
                title_link = f'<a href="{m.external_url}">{m.title}</a>'
            else:
                title_link = f"<b>{m.title}</b>"
            lines.append(
                f'  • You asked for "<i>{lr.request.display()}</i>" '
                f"→ found {title_link} — correct?"
            )
        lines.append("")

    if not_found:
        lines.append("❌ <b>Not found anywhere:</b>")
        for lr in not_found:
            lines.append(f"  • {lr.request.display()}")
        lines.append("")

    # Summary note
    submittable = [lr for lr in results if not lr.in_library]
    if submittable:
        lines.append(
            "Tap <b>Submit Requests</b> to add the un-found title(s) to the queue, "
            "or <b>Start Over</b> to try again."
        )
    else:
        lines.append("Everything you asked for is already in the library! 🎉")

    return "\n".join(lines)


def _has_submittable(results: list[LookupResult]) -> bool:
    """True if at least one result is not already in the library."""
    return any(not lr.in_library for lr in results)


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
    await query.answer()

    data = query.data or ""
    # e.g. "req_type_movie" → media_type = "movie"
    media_type = data.removeprefix("req_type_")

    if media_type == "queue":
        queue_text = format_requests_message_user()
        await query.message.reply_text(queue_text)
        return ConversationHandler.END

    # Store chosen type and send instructions
    assert context.user_data is not None
    context.user_data[_UD_MEDIA_TYPE] = media_type

    instruction = _INSTRUCTIONS.get(media_type, _INSTRUCTIONS["other"])
    await query.message.reply_html(instruction)
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
    await query.answer()

    data = query.data or ""
    assert context.user_data is not None

    if data == "req_confirm_restart":
        await query.message.reply_text(
            "No problem — what would you like to request?",
            reply_markup=_TYPE_KEYBOARD,
        )
        context.user_data.pop(_UD_RESULTS, None)
        context.user_data.pop(_UD_MEDIA_TYPE, None)
        return SELECT_TYPE

    if data == "req_confirm_yes":
        results: list[LookupResult] = context.user_data.get(_UD_RESULTS, [])
        media_type: str = context.user_data.get(_UD_MEDIA_TYPE, "unknown")
        requester = _requester_name(update)

        loop = asyncio.get_running_loop()
        added: list[str] = []

        for lr in results:
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
            await query.message.reply_html(
                f"✅ <b>Added {len(added)} request(s) to the queue:</b>\n{bullet_list}\n\n"
                f"Use the <b>📝 Requests</b> button anytime to see the full queue."
            )
        else:
            await query.message.reply_text(
                "Nothing new to add — everything was already in your library!"
            )

        context.user_data.pop(_UD_RESULTS, None)
        context.user_data.pop(_UD_MEDIA_TYPE, None)
        return ConversationHandler.END

    # Unknown confirm action — just end
    return ConversationHandler.END


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
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_request_flow),
    ],
    allow_reentry=True,
    conversation_timeout=300,   # 5-minute idle timeout
)
