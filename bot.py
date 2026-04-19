# =============================================================================
# PlexResetButton — bot.py
# =============================================================================
# Mission: Provide a Telegram bot interface for remotely controlling Plex Media
# Server on a Windows machine. Goals: graceful soft reset via tray icon,
# forceful hard reset via process kill, live status reporting, and a friendly
# welcome/help menu so any household member can use the bot without knowing
# Telegram command syntax.
#
# This module owns all Telegram handler logic. It is intentionally decoupled
# from the Plex control machinery (plex_control.py) and the OS interaction
# layer (icon_finder.py). Blocking Plex calls are offloaded to a thread
# executor so the async event loop is never stalled.
# =============================================================================

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from library_index import (format_library_summary_message,
                           format_reindex_result_message,
                           format_search_results_message, rebuild_library_index)
from metrics_report import format_combined_metrics_message
from plex_control import control_busy, get_status, hard_reset, launch_plex, soft_reset
from queue_store import add_request, format_requests_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Welcome text and shared keyboard shown by /start
# ---------------------------------------------------------------------------

_WELCOME_TEXT = (
    "Hey! I'm your <b>Plex Reset Bot</b>. — Here to keep movie night running smooth.\n\n"
    "<b>Commands:</b>\n"
    "▶️ <b>Launch Plex</b> — Starts Plex Media Server directly from its configured "
    "Windows executable path.\n\n"
    "🔄 <b>Soft Reset</b> — Gracefully exits Plex via the system "
    "tray icon and relaunches it. Use this first; least disruptive.\n\n"
    "☠️ <b>Hard Reset</b> — Force-kills ALL Plex processes and relaunches. "
    "Use this if soft reset fails. <i>Will interrupt anyone currently watching.</i>\n\n"
    "📝 <b>Request Queue</b> — Use <code>/request &lt;what you want&gt;</code> to add something "
    "to the queue, and <code>/requests</code> to see what is waiting.\n\n"
    "🔎 <b>Library Search</b> — Use <code>/search &lt;title&gt;</code> to search indexed library "
    "when a Plex token is configured, with folder indexing as a fallback. "
    "Use <code>/reindex</code> for the fallback folder index.\n\n"
    "📈 <b>Metrics</b> — Use <code>/metrics</code> for indexed file counts, Plex usage stats, "
    "and Plex library section counts.\n\n"
    "📊 <b>Status</b> — Shows whether Plex is currently running and "
    "whether the image assets the bot uses for screen matching are in place.\n\n"
    "Tap a button below or type the command:"
)

_MAIN_KEYBOARD = InlineKeyboardMarkup(
    [
        [
            InlineKeyboardButton("▶️ Launch Plex", callback_data="cmd_launch"),
        ],
        [
            InlineKeyboardButton("🔄 Soft Reset", callback_data="cmd_reset"),
            InlineKeyboardButton("☠️ Hard Reset", callback_data="cmd_hardreset"),
        ],
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
    await update.message.reply_html(_WELCOME_TEXT, reply_markup=_MAIN_KEYBOARD)


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
    if update.message is None:
        return
    loop = asyncio.get_running_loop()
    requests_text = await loop.run_in_executor(None, format_requests_message)
    await update.message.reply_text(
        requests_text + "\n\nAdd one with: /request <what you want>"
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
    created = await loop.run_in_executor(None, lambda: add_request(content, requester))
    await update.message.reply_text(
        f"Added request #{created.request_id} from {created.requester}: {created.content}"
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

    elif query.data == "cmd_reset":
        if control_busy():
            await context.bot.send_message(
                chat_id=chat_id, text="A Plex action is already in progress. Please wait."
            )
            return
        await context.bot.send_message(chat_id=chat_id, text="🔄 Starting soft reset...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, soft_reset)
        await context.bot.send_message(chat_id=chat_id, text=result)

    elif query.data == "cmd_hardreset":
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

    elif query.data == "cmd_requests":
        loop = asyncio.get_running_loop()
        requests_text = await loop.run_in_executor(None, format_requests_message)
        await context.bot.send_message(
            chat_id=chat_id,
            text=requests_text + "\n\nAdd one with: /request <what you want>",
        )

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
# /reset — soft reset
# ---------------------------------------------------------------------------

async def reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if control_busy():
        await update.message.reply_text("A Plex action is already in progress. Please wait.")
        return

    await update.message.reply_text("Starting soft reset...")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, soft_reset)

    await update.message.reply_text(result)


# ---------------------------------------------------------------------------
# /hardreset — confirmation prompt then hard reset
# ---------------------------------------------------------------------------

async def hardreset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
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
