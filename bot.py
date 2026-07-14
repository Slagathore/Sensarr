# =============================================================================
# PlexResetButton — bot.py
# =============================================================================
# Mission: Provide a Telegram bot interface for remotely controlling Plex Media
# Server on a Windows machine. Goals: launch, optional hard reset (admin-gated
# via TELEGRAM_HARD_RESET_ENABLED), live status reporting, and a friendly
# welcome/help menu so any household member can use the bot without knowing
# Telegram command syntax.
#
# This module owns all Telegram handler logic. It is intentionally decoupled
# from the Plex control machinery (plex_control.py). Blocking Plex calls are
# offloaded to a thread executor so the async event loop is never stalled.
# =============================================================================

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from library_index import (format_library_summary_message,
                           format_reindex_result_message,
                           format_search_results_message, rebuild_library_index)
from metrics_report import format_combined_metrics_message
import config
from plex_control import control_busy, get_status, hard_reset, launch_plex
import request_intake
from queue_store import format_requests_message_user

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Welcome text and shared keyboard shown by /start
# ---------------------------------------------------------------------------

_WELCOME_BASE = (
    "Hey! I'm your <b>Plex Reset Bot</b>. — Here to keep movie night running smooth.\n\n"
    "<b>Commands:</b>\n"
    "▶️ <b>Launch Plex</b> — Starts Plex Media Server directly from its configured "
    "Windows executable path.\n\n"
)

_WELCOME_HARD_RESET = (
    "☠️ <b>Hard Reset</b> — Force-kills ALL Plex processes and relaunches. "
    "<i>Will interrupt anyone currently watching.</i>\n\n"
)

_WELCOME_REST = (
    "📝 <b>Request Queue</b> — Use <code>/request &lt;what you want&gt;</code> to add something "
    "to the queue, and <code>/requests</code> to see what is waiting.\n\n"
    "🔎 <b>Library Search</b> — Use <code>/search &lt;title&gt;</code> to search indexed library "
    "when a Plex token is configured, with folder indexing as a fallback. "
    "Use <code>/reindex</code> for the fallback folder index.\n\n"
    "📈 <b>Metrics</b> — Use <code>/metrics</code> for indexed file counts, Plex usage stats, "
    "and Plex library section counts.\n\n"
    "📊 <b>Status</b> — Shows whether Plex is currently running.\n\n"
    "Tap a button below or type the command:"
)


def _welcome_text() -> str:
    hard = _WELCOME_HARD_RESET if config.TELEGRAM_HARD_RESET_ENABLED else ""
    return _WELCOME_BASE + hard + _WELCOME_REST


def _main_keyboard() -> InlineKeyboardMarkup:
    """Built per-message so the hard-reset button honours the live setting."""
    top_row = [InlineKeyboardButton("▶️ Launch Plex", callback_data="cmd_launch")]
    if config.TELEGRAM_HARD_RESET_ENABLED:
        top_row.append(InlineKeyboardButton("☠️ Hard Reset", callback_data="cmd_hardreset"))
    return InlineKeyboardMarkup(
        [
            top_row,
            [
                InlineKeyboardButton("📝 Requests", callback_data="cmd_requests"),
                InlineKeyboardButton("📊 Status", callback_data="cmd_status"),
            ],
            [
                InlineKeyboardButton("🔎 Library", callback_data="cmd_libraries"),
                InlineKeyboardButton("📈 Metrics", callback_data="cmd_metrics"),
            ],
        ]
    )


# ---------------------------------------------------------------------------
# /start — welcome + command menu
# ---------------------------------------------------------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_html(_welcome_text(), reply_markup=_main_keyboard())


# ---------------------------------------------------------------------------
# /help — same content as /start; documented as a command in Telegram clients
# ---------------------------------------------------------------------------

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_handler(update, context)


# ---------------------------------------------------------------------------
# Welcome fallback — fired when a user sends free-form text outside of any
# active conversation. Gives them a quick orientation plus a button that
# re-opens the full start menu.
# ---------------------------------------------------------------------------

_WELCOME_FALLBACK_TEXT = (
    "👋 <b>Hi! I'm your Plex Reset Bot.</b>\n\n"
    "I didn't catch that, but I can help you with:\n"
    "• Launching, resetting, or checking on your Plex server\n"
    "• Adding to the request queue\n"
    "• Searching the library\n"
    "• Showing usage metrics\n\n"
    "Type <code>/help</code> any time, or tap the button below to open the main menu."
)

_WELCOME_FALLBACK_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("📋 Open Start Menu", callback_data="cmd_help")]]
)


async def welcome_fallback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Catch-all for free-form text when the user isn't in a flow."""
    if update.message is None:
        return
    await update.message.reply_html(
        _WELCOME_FALLBACK_TEXT, reply_markup=_WELCOME_FALLBACK_KEYBOARD,
    )


# ---------------------------------------------------------------------------
# /status — Plex process + asset health check
# ---------------------------------------------------------------------------

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    loop = asyncio.get_running_loop()
    status_text = await loop.run_in_executor(None, get_status)
    await update.message.reply_text(status_text)


async def requests_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current request queue (user-facing: no requester names)."""
    if update.message is None:
        return
    loop = asyncio.get_running_loop()
    # format_requests_message_user omits requester names for privacy
    requests_text = await loop.run_in_executor(None, format_requests_message_user)
    await update.message.reply_text(
        requests_text + "\n\nTap 📝 Requests to add one."
    )


async def libraries_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    loop = asyncio.get_running_loop()
    library_text = await loop.run_in_executor(None, format_library_summary_message)
    await update.message.reply_text(
        library_text + "\n\nSearch with: /search <title>\nRebuild index with: /reindex"
    )


async def metrics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    loop = asyncio.get_running_loop()
    metrics_text = await loop.run_in_executor(None, format_combined_metrics_message)
    await update.message.reply_text(metrics_text)


def _requester_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "Unknown"
    if user.username:
        return f"@{user.username}"
    return user.full_name or "Unknown"


async def request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    args = context.args or []
    content = " ".join(args).strip()
    if not content:
        await update.message.reply_text(
            "Usage: /request <what you want added>\nExample: /request Add the new Dune movie"
        )
        return

    requester = _requester_name(update)
    loop = asyncio.get_running_loop()
    # A bare /request carries no type and no resolved identity, so it lands as
    # needs_identity (visible, never auto-grabbed) rather than an untyped 'open'
    # row that the old auto-grab coerced to 'other' and grabbed — the #85 path.
    # Users resolve it via the structured 📝 Requests flow or the desktop app.
    created = await loop.run_in_executor(
        None, lambda: request_intake.add_needs_identity(content, requester))
    await update.message.reply_text(
        f"Added request #{created.request_id} from {created.requester}: "
        f"{created.content}\n\nI couldn't resolve a specific title from that, so "
        f"it's waiting on identification. Use the 📝 Requests button to pick the "
        f"exact movie or show, or it will stay visible but won't auto-download."
    )


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    args = context.args or []
    query = " ".join(args).strip()
    if not query:
        await update.message.reply_text(
            "Usage: /search <title>\nExample: /search severance"
        )
        return

    loop = asyncio.get_running_loop()
    results_text = await loop.run_in_executor(
        None,
        lambda: format_search_results_message(query),
    )
    await update.message.reply_text(results_text)


async def reindex_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text("Starting library reindex. This can take a while...")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, rebuild_library_index)
    await update.message.reply_text(format_reindex_result_message(result))


# ---------------------------------------------------------------------------
# Inline button handler — routes cmd_* buttons from the main menu
# ---------------------------------------------------------------------------

async def cmd_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses from the main /start menu."""
    query = update.callback_query
    if query is None or query.message is None:
        return
    await query.answer()

    chat_id = query.message.chat.id

    if query.data == "cmd_help":
        await context.bot.send_message(
            chat_id=chat_id, text=_welcome_text(),
            reply_markup=_main_keyboard(), parse_mode="HTML",
        )
        return

    if query.data == "cmd_launch":
        if control_busy():
            await context.bot.send_message(
                chat_id=chat_id, text="A Plex action is already in progress. Please wait."
            )
            return
        await context.bot.send_message(chat_id=chat_id, text="Starting Plex...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, launch_plex)
        await context.bot.send_message(chat_id=chat_id, text=result)

    elif query.data == "cmd_hardreset":
        if not config.TELEGRAM_HARD_RESET_ENABLED:
            await context.bot.send_message(
                chat_id=chat_id, text="Hard reset via Telegram is disabled by the admin."
            )
            return
        if control_busy():
            await context.bot.send_message(
                chat_id=chat_id, text="A Plex action is already in progress. Please wait."
            )
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, do it", callback_data="hardreset_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="hardreset_cancel"),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ Hard reset will forcefully kill ALL Plex processes. "
                "This may interrupt anyone currently watching. Are you sure?"
            ),
            reply_markup=keyboard,
        )

    elif query.data == "cmd_status":
        loop = asyncio.get_running_loop()
        status_text = await loop.run_in_executor(None, get_status)
        await context.bot.send_message(chat_id=chat_id, text=status_text)

    # NOTE: "cmd_requests" is handled by REQUEST_CONV_HANDLER in request_flow.py
    # (the ConversationHandler is registered before this wildcard handler).

    elif query.data == "cmd_libraries":
        loop = asyncio.get_running_loop()
        library_text = await loop.run_in_executor(None, format_library_summary_message)
        await context.bot.send_message(
            chat_id=chat_id,
            text=library_text + "\n\nSearch with: /search <title>\nRebuild index with: /reindex",
        )

    elif query.data == "cmd_metrics":
        loop = asyncio.get_running_loop()
        metrics_text = await loop.run_in_executor(None, format_combined_metrics_message)
        await context.bot.send_message(chat_id=chat_id, text=metrics_text)

    # #todo: add cmd_logs button that tails the last N lines of the bot's log file


# ---------------------------------------------------------------------------
# /launch — direct start
# ---------------------------------------------------------------------------

async def launch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if control_busy():
        await update.message.reply_text("A Plex action is already in progress. Please wait.")
        return

    await update.message.reply_text("Starting Plex...")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, launch_plex)

    await update.message.reply_text(result)


# ---------------------------------------------------------------------------
# /hardreset — confirmation prompt then hard reset
# ---------------------------------------------------------------------------

async def hardreset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not config.TELEGRAM_HARD_RESET_ENABLED:
        await update.message.reply_text("Hard reset via Telegram is disabled by the admin.")
        return
    if control_busy():
        await update.message.reply_text("A Plex action is already in progress. Please wait.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, do it", callback_data="hardreset_confirm"),
                InlineKeyboardButton("Cancel", callback_data="hardreset_cancel"),
            ]
        ]
    )

    await update.message.reply_text(
        "⚠️ Hard reset will forcefully kill ALL Plex processes. "
        "This may interrupt anyone currently watching. Are you sure?",
        reply_markup=keyboard,
    )


async def hardreset_confirm_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    if query.data == "hardreset_cancel":
        await query.edit_message_text("Hard reset cancelled.")
        return

    if query.data == "hardreset_confirm":
        if not config.TELEGRAM_HARD_RESET_ENABLED:
            await query.edit_message_text("Hard reset via Telegram is disabled by the admin.")
            return
        if control_busy():
            await query.edit_message_text("A Plex action is already in progress. Please wait.")
            return

        await query.edit_message_text("Starting hard reset...")

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, hard_reset)

        if query.message is None:
            logger.warning("hardreset_confirm_handler: query.message is None, cannot send result.")
            return
        await context.bot.send_message(chat_id=query.message.chat.id, text=result)
