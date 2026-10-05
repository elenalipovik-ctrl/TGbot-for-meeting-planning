"""aiogram long-polling adapter for visitor and administrator dialogs."""

import asyncio
import logging
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .admin_flow import AdminFlow
from .env_config import ENV_FILE, read_env
from .google_auth import calendar_from_env
from .runtime_mode import allowed_test_users
from .service import BookingService
from .technical_log import configure_logging, log_event
from .visitor_flow import Reply, VisitorFlow


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button.label, callback_data=button.action)
         for button in row] for row in reply.rows
    ])


def _username(message_or_query: Message | CallbackQuery) -> str | None:
    user = message_or_query.from_user
    return user.username if user else None


def build_router(flow: VisitorFlow, admin_flow: AdminFlow | None = None,
                 allowed_user_ids: frozenset[int] | None = None) -> Router:
    router = Router(name="dialogs")

    def permitted(user_id: int) -> bool:
        return allowed_user_ids is None or user_id in allowed_user_ids

    async def send(message: Message, reply: Reply) -> None:
        await message.answer(reply.text, reply_markup=_markup(reply))
        log_event(flow.service.logger, "telegram_reply_sent")

    @router.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        if message.from_user is None or not permitted(message.from_user.id):
            return
        if admin_flow is not None and message.from_user.id == flow.service.administrator_id:
            reply = await asyncio.to_thread(admin_flow.handle, message.from_user.id,
                                            "start", int(time.time()))
        else:
            reply = await asyncio.to_thread(flow.handle, message.from_user.id,
                                            _username(message), "start", int(time.time()))
        await send(message, reply)

    @router.message(Command("admin"), F.chat.type == "private")
    async def admin_menu(message: Message) -> None:
        if message.from_user is None or admin_flow is None or not permitted(message.from_user.id):
            return
        reply = await asyncio.to_thread(admin_flow.handle, message.from_user.id,
                                        "adm:menu", int(time.time()))
        await send(message, reply)

    @router.callback_query(F.message.chat.type == "private")
    async def callback(query: CallbackQuery) -> None:
        await query.answer()
        if query.from_user is None or not permitted(query.from_user.id) or \
                not isinstance(query.message, Message):
            return
        action = query.data or ""
        if len(action) > 64:
            return
        if admin_flow is not None and query.from_user.id == flow.service.administrator_id:
            reply = await asyncio.to_thread(admin_flow.handle, query.from_user.id,
                                            action, int(time.time()))
        elif action.startswith("adm:"):
            return
        else:
            reply = await asyncio.to_thread(flow.handle, query.from_user.id,
                                            _username(query), action, int(time.time()))
        await send(query.message, reply)

    @router.message(F.chat.type == "private", F.text)
    async def text_message(message: Message) -> None:
        if message.from_user is None or not permitted(message.from_user.id):
            return
        if admin_flow is not None and message.from_user.id == flow.service.administrator_id:
            reply = await asyncio.to_thread(admin_flow.handle, message.from_user.id,
                                            "text", int(time.time()), text=message.text)
        else:
            reply = await asyncio.to_thread(flow.handle, message.from_user.id,
                                            _username(message), "text", int(time.time()),
                                            text=message.text)
        await send(message, reply)

    return router


async def run() -> None:
    values = read_env(ENV_FILE)
    required = ("TELEGRAM_BOT_TOKEN", "BOT_ADMIN_ID", "POLICY_URL", "POLICY_VERSION",
                "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
    if any(not values.get(key) for key in required):
        raise RuntimeError("Telegram, policy or Google settings are incomplete in .env")
    allowed_user_ids = allowed_test_users(values)
    try:
        administrator_id = int(values["BOT_ADMIN_ID"])
    except ValueError as error:
        raise RuntimeError("BOT_ADMIN_ID must be a Telegram numeric ID") from error
    if administrator_id <= 0:
        raise RuntimeError("BOT_ADMIN_ID must be positive")
    logger = configure_logging(Path(values.get("BOT_LOG_PATH") or "logs/technical.jsonl"))
    service = BookingService(Path(values.get("BOT_DB_PATH") or "data/bot.sqlite"),
                             administrator_id=administrator_id, logger=logger)
    service.initialize()
    calendar = calendar_from_env(ENV_FILE, values.get("BOT_CALENDAR_ID") or "primary",
                                 logger=logger)
    flow = VisitorFlow(service, calendar, policy_url=values["POLICY_URL"],
                       policy_version=values["POLICY_VERSION"])
    admin_flow = AdminFlow(service, calendar)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(flow, admin_flow, allowed_user_ids))
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    bot = Bot(token=values["TELEGRAM_BOT_TOKEN"])
    log_event(logger, "telegram_polling_started")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        log_event(logger, "telegram_polling_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
