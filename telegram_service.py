import asyncio
import logging
import threading

from telegram import Update
from telegram.ext import (Application, ApplicationBuilder, ApplicationHandlerStop,
                          CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, TypeHandler, filters)

import auth_store
import config
from bot import (cmd_button_handler, hardreset_confirm_handler,
                 hardreset_handler, help_handler, launch_handler,
                 libraries_handler, metrics_handler, reindex_handler,
                 request_handler, requests_handler, reset_handler,
                 search_handler, start_handler, status_handler,
                 welcome_fallback_handler)
from request_flow import REQUEST_CONV_HANDLER

logger = logging.getLogger(__name__)


async def _authorization_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler (group -1). Blocks unknown users.

    Allowed: IDs already in the allowlist, or users who can claim a seat
    seeded from a past requester name (see auth_store). Everyone else gets a
    polite denial that includes their numeric ID so the admin can add them to
    TELEGRAM_ALLOWED_USER_IDS in .env.
    """
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop  # channel posts / anonymous — ignore

    if auth_store.is_user_allowed(user.id):
        return

    display_name = user.full_name or ""
    if auth_store.try_claim_seat(user.id, display_name, user.username):
        return

    # Unknown user → file (or re-surface) an access request the admin can
    # approve from the desktop app's Users tab.
    chat = update.effective_chat
    status = auth_store.create_access_request(
        user.id,
        display_name=display_name or None,
        username=user.username,
        chat_id=chat.id if chat is not None else None,
    )
    logger.warning(
        "Blocked Telegram user id=%s name=%r username=%r (access request: %s)",
        user.id, display_name, user.username, status,
    )
    if status == "pending":
        reply = (
            "👋 You're not on this bot's user list yet, so I've sent an "
            "access request to the admin. You'll get a message here as soon "
            "as they approve you.\n"
            f"(Your Telegram ID, in case they ask: {user.id})"
        )
    else:  # 'denied' — admin already said no; stay firm but polite.
        reply = "⛔ You're not authorized to use this bot."
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(
                "Not authorized yet — access request sent to the admin."
                if status == "pending" else "Not authorized.",
                show_alert=True,
            )
        elif update.effective_message is not None:
            await update.effective_message.reply_text(reply)
    except Exception:
        logger.exception("Failed to send authorization reply.")
    raise ApplicationHandlerStop


def build_application() -> Application:
    auth_store.initialize_auth_db()
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Authorization gate runs in group -1, i.e. before every handler below.
    # ApplicationHandlerStop prevents any group-0 handler from running for
    # unauthorized users.
    app.add_handler(TypeHandler(Update, _authorization_gate), group=-1)

    # -----------------------------------------------------------------------
    # IMPORTANT: ConversationHandler must be registered BEFORE the wildcard
    # CallbackQueryHandler(cmd_button_handler, pattern="^cmd_") so that
    # "cmd_requests" is captured by the conversation flow first.
    # -----------------------------------------------------------------------
    app.add_handler(REQUEST_CONV_HANDLER)

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(CommandHandler("launch", launch_handler))
    app.add_handler(CommandHandler("libraries", libraries_handler))
    app.add_handler(CommandHandler("metrics", metrics_handler))
    app.add_handler(CommandHandler("request", request_handler))   # legacy free-text add
    app.add_handler(CommandHandler("requests", requests_handler)) # shows queue (no names)
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CommandHandler("reindex", reindex_handler))
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(CommandHandler("hardreset", hardreset_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CallbackQueryHandler(hardreset_confirm_handler, pattern="^hardreset_"))
    app.add_handler(CallbackQueryHandler(cmd_button_handler, pattern="^cmd_"))

    # Catch-all for any free-form text the user sends outside an active
    # conversation (REQUEST_CONV_HANDLER's per-state handlers consume their own
    # messages first, so this only fires when no menu/submenu is open).
    # Must be registered LAST so it doesn't shadow other handlers.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, welcome_fallback_handler)
    )

    return app


class TelegramBotService:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._startup_error: Exception | None = None
        self._app: Application | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._startup_error is None

    def start(self, timeout: float = 20.0) -> None:
        if self.running:
            return

        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="telegram-bot",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(timeout):
            raise RuntimeError("Timed out while starting the Telegram bot service.")
        if self._startup_error is not None:
            raise RuntimeError("Telegram bot service failed to start.") from self._startup_error

    def stop(self, timeout: float = 20.0) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)

        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "Telegram bot service did not stop within %.1f seconds; continuing shutdown.",
                    timeout,
                )
            else:
                self._thread = None
                self._app = None

    def notify_user(self, chat_id: int, text: str, *, timeout: float = 10.0) -> bool:
        """Send a proactive message to a chat from any thread.

        Used by the desktop app to tell a user their access request was
        approved/denied. Returns True on success, False if the bot isn't
        running or the send failed.
        """
        loop = self._loop
        app = self._app
        if loop is None or app is None or not self.running:
            logger.warning("Cannot notify chat %s — bot service is not running.", chat_id)
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=chat_id, text=text), loop
            )
            future.result(timeout=timeout)
            return True
        except Exception:
            logger.exception("Failed to send notification to chat %s.", chat_id)
            return False

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        try:
            loop.run_until_complete(self._async_run())
        except Exception as exc:
            self._startup_error = exc
            logger.exception("Telegram bot service crashed during startup.")
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()
                self._loop = None
                self._stop_event = None

    async def _async_run(self) -> None:
        self._stop_event = asyncio.Event()
        app = build_application()
        self._app = app

        try:
            await app.initialize()
            await app.start()
            if app.updater is None:
                raise RuntimeError("Telegram updater was not created.")
            await app.updater.start_polling()
            logger.info("PlexResetButton bot started. Waiting for commands...")
            self._ready.set()
            await self._stop_event.wait()
        finally:
            if app.updater is not None:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Telegram bot service stopped.")
