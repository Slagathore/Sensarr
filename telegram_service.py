import asyncio
import logging
import threading

from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler)

import config
from bot import (cmd_button_handler, hardreset_confirm_handler,
                 hardreset_handler, launch_handler, libraries_handler,
                 metrics_handler, reindex_handler, request_handler, requests_handler,
                 reset_handler, search_handler, start_handler,
                 status_handler)

logger = logging.getLogger(__name__)


def build_application() -> Application:
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("launch", launch_handler))
    app.add_handler(CommandHandler("libraries", libraries_handler))
    app.add_handler(CommandHandler("metrics", metrics_handler))
    app.add_handler(CommandHandler("request", request_handler))
    app.add_handler(CommandHandler("requests", requests_handler))
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CommandHandler("reindex", reindex_handler))
    app.add_handler(CommandHandler("reset", reset_handler))
    app.add_handler(CommandHandler("hardreset", hardreset_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.add_handler(CallbackQueryHandler(hardreset_confirm_handler, pattern="^hardreset_"))
    app.add_handler(CallbackQueryHandler(cmd_button_handler, pattern="^cmd_"))

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
