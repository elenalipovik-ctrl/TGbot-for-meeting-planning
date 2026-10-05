"""Pair one owner-controlled visitor account without printing IDs or tokens."""

import argparse
import asyncio

from aiogram import Bot

from meeting_bot.env_config import ENV_FILE, read_env, write_env_values


async def pair(username: str) -> bool:
    settings = read_env(ENV_FILE)
    token = settings.get("TELEGRAM_BOT_TOKEN")
    admin_id = settings.get("BOT_ADMIN_ID")
    if not token or not admin_id or not admin_id.isdecimal():
        raise RuntimeError("Bot token and administrator ID must be present in .env")
    bot = Bot(token=token)
    try:
        if (await bot.get_webhook_info()).url:
            raise RuntimeError("Remove the existing Telegram webhook before local polling")
        updates = await bot.get_updates(limit=100, timeout=0,
                                        allowed_updates=["message"])
    finally:
        await bot.session.close()
    candidates = [update.message.from_user.id for update in updates
                  if update.message and update.message.chat.type == "private"
                  and update.message.from_user
                  and (update.message.from_user.username or "").casefold()
                  == username.lstrip("@").casefold()
                  and (update.message.text or "").startswith("/start")]
    if not candidates:
        return False
    visitor_id = candidates[-1]
    if visitor_id <= 0 or visitor_id == int(admin_id):
        raise RuntimeError("The visitor must be a different Telegram account")
    write_env_values(ENV_FILE, {
        "BOT_LOCAL_TEST_MODE": "true",
        "BOT_LOCAL_TEST_VISITOR_ID": str(visitor_id),
        "BOT_DB_PATH": "data/local_test.sqlite",
        "BOT_LOG_PATH": "logs/local_test.jsonl",
    })
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("username", help="Second test account's Telegram username")
    args = parser.parse_args()
    if asyncio.run(pair(args.username)):
        print("Local test visitor paired; test-only settings saved in root .env")
    else:
        print("No pending /start from that username; ask the visitor to press Start")


if __name__ == "__main__":
    main()
