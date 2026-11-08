# =============================================================================
# Telegram bot service lifecycle (2026-10 outages).
#
# A single getMe timeout at logon used to leave the bot off until the next
# restart, and the teardown then hid the real error behind "This Updater is not
# running!". A handler that raised (a locked database) left the user with no
# reply at all. These pin the retry, the teardown, and the error reply against
# a stand-in Application, so no test touches the network.
# =============================================================================

import asyncio
import datetime as dt
import types

import pytest
from telegram import Chat, Message, Update
from telegram.error import BadRequest, InvalidToken, NetworkError, TimedOut
from telegram.ext import CallbackQueryHandler, CommandHandler

import telegram_service


class _FakeBot:
    def __init__(self):
        self.shutdowns = 0
        self.sent = []

    async def shutdown(self):
        self.shutdowns += 1

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeUpdater:
    def __init__(self):
        self.running = False

    async def start_polling(self):
        self.running = True

    async def stop(self):
        if not self.running:
            raise RuntimeError("This Updater is not running!")
        self.running = False


class _FakeApp:
    """Mirrors the PTB calls the service makes, including stop() raising when
    nothing is running, so a sloppy teardown fails the test."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.initialize_calls = 0
        self.bot = _FakeBot()
        self.updater = _FakeUpdater()
        self.running = False
        self.shut_down = False

    async def initialize(self):
        self.initialize_calls += 1
        if self.failures:
            raise self.failures.pop(0)

    async def start(self):
        self.running = True

    async def stop(self):
        if not self.running:
            raise RuntimeError("This Application is not running!")
        self.running = False

    async def shutdown(self):
        self.shut_down = True


@pytest.fixture
def fake_app(monkeypatch):
    holder = {}

    def install(failures=(), retry_first=0.01):
        app = _FakeApp(failures)
        holder["app"] = app
        monkeypatch.setattr(telegram_service, "build_application", lambda: app)
        monkeypatch.setattr(telegram_service, "_CONNECT_RETRY_FIRST", retry_first)
        return app

    return install


def test_startup_retries_until_telegram_answers(fake_app):
    app = fake_app(failures=[TimedOut(), NetworkError("connect failed")])
    service = telegram_service.TelegramBotService()

    assert service.start(timeout=5) is True
    assert service.connected
    assert app.initialize_calls == 3
    # The bot is shut down after each failed attempt so the next one runs
    # getMe again instead of skipping it as already initialized.
    assert app.bot.shutdowns == 2

    service.stop(timeout=5)
    assert not service.running
    assert not app.updater.running and not app.running and app.shut_down


def test_stop_while_still_connecting_exits_cleanly(fake_app):
    app = fake_app(failures=[TimedOut()] * 100, retry_first=30)
    service = telegram_service.TelegramBotService()

    assert service.start(timeout=0.2) is False
    assert service.running and not service.connected

    service.stop(timeout=5)
    assert not service.running
    assert service._startup_error is None
    assert app.shut_down


def test_rejected_token_fails_at_once_with_the_real_error(fake_app):
    app = fake_app(failures=[InvalidToken("rejected")])
    service = telegram_service.TelegramBotService()

    with pytest.raises(RuntimeError) as excinfo:
        service.start(timeout=5)

    assert isinstance(excinfo.value.__cause__, InvalidToken)
    assert app.initialize_calls == 1
    assert not service.running


def _update_in_chat(chat_id):
    message = Message(message_id=1, date=dt.datetime.now(dt.timezone.utc),
                      chat=Chat(id=chat_id, type="private"), text="1")
    return Update(update_id=1, message=message)


def test_failed_handler_tells_the_user():
    bot = _FakeBot()
    context = types.SimpleNamespace(
        error=RuntimeError("database is locked"), bot=bot)

    asyncio.run(telegram_service._on_handler_error(_update_in_chat(42), context))

    assert len(bot.sent) == 1 and bot.sent[0][0] == 42


def test_stale_button_press_is_not_answered():
    bot = _FakeBot()
    context = types.SimpleNamespace(
        error=BadRequest("Query is too old and response timeout expired "
                         "or query id is invalid"),
        bot=bot)

    asyncio.run(telegram_service._on_handler_error(_update_in_chat(42), context))

    assert bot.sent == []


def test_slow_plex_actions_do_not_block_other_updates():
    app = telegram_service.build_application()

    assert app.error_handlers
    handlers = app.handlers[0]
    callbacks = {h.pattern.pattern: h for h in handlers
                 if isinstance(h, CallbackQueryHandler) and h.pattern is not None}
    commands = {name: h for h in handlers if isinstance(h, CommandHandler)
                for name in h.commands}
    assert callbacks["^hardreset_"].block is False
    assert callbacks["^cmd_"].block is False
    assert commands["launch"].block is False
    assert commands["reindex"].block is False
