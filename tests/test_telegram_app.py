"""Telegram adapter must process a button even if its acknowledgement expires."""

import asyncio
import socket

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

from meeting_bot.technical_log import configure_logging
from meeting_bot.telegram_app import _acknowledge_callback, _configure_command_menu
from meeting_bot.telegram_transport import telegram_bot


def test_expired_callback_acknowledgement_does_not_abort(tmp_path):
    class ExpiredQuery:
        async def answer(self):
            raise TelegramBadRequest(
                method=AnswerCallbackQuery(callback_query_id="expired"),
                message="query is too old",
            )

    log_path = tmp_path / "technical.jsonl"
    asyncio.run(_acknowledge_callback(ExpiredQuery(), configure_logging(log_path)))
    log_text = log_path.read_text(encoding="utf-8")
    assert "telegram_callback_ack_failed" in log_text
    assert "query is too old" not in log_text


def test_telegram_session_uses_ipv4_route():
    bot = telegram_bot("123456:TEST_TOKEN")
    assert bot.session._connector_init["family"] == socket.AF_INET
    asyncio.run(bot.session.close())


def test_command_menu_offers_start_without_typing(tmp_path):
    class BotStub:
        def __init__(self):
            self.commands = None
            self.button = None

        async def set_my_commands(self, commands):
            self.commands = commands

        async def set_chat_menu_button(self, menu_button):
            self.button = menu_button

    bot = BotStub()
    logger = configure_logging(tmp_path / "technical.jsonl")
    asyncio.run(_configure_command_menu(bot, logger))
    assert [(command.command, command.description) for command in bot.commands] == [
        ("start", "Открыть меню")
    ]
    assert bot.button.type == "commands"


def test_form_callback_includes_revision_and_preserves_navigation():
    from meeting_bot.telegram_app import _callback_data
    assert _callback_data("send", 12) == "r:12:send"
    assert _callback_data("time:1791200000", 12) == "r:12:time:1791200000"
    assert _callback_data("profile:delete:yes", 12) == "r:12:profile:delete:yes"
    assert _callback_data("menu", 12) == "menu"
    assert len(_callback_data("time:1791200000", 123456789).encode("utf-8")) <= 64
