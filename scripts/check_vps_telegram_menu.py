"""Read back the bot's Telegram command menu without printing credentials."""

import asyncio

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.telegram_transport import telegram_bot


async def main() -> None:
    token = read_env(ENV_FILE).get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Telegram token missing from server .env")
    bot = telegram_bot(token)
    try:
        commands = await bot.get_my_commands()
        menu = await bot.get_chat_menu_button()
    finally:
        await bot.session.close()
    if [(item.command, item.description) for item in commands] != [
        ("start", "Открыть меню")
    ] or menu.type != "commands":
        raise SystemExit("Telegram command menu has not been configured")
    print("Telegram command menu: /start available through Menu")


if __name__ == "__main__":
    asyncio.run(main())
