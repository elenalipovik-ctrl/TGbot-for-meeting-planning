"""Check local Telegram configuration without displaying token or numeric owner ID."""

import asyncio

from aiogram import Bot

from meeting_bot.env_config import ENV_FILE, read_env


async def main() -> None:
    values = read_env(ENV_FILE)
    token = values.get("TELEGRAM_BOT_TOKEN") or ""
    owner_id = values.get("BOT_ADMIN_ID") or ""
    print(f"TELEGRAM_BOT_TOKEN: {'present' if token else 'missing'}")
    print(f"BOT_ADMIN_ID: {'valid numeric value' if owner_id.isdigit() and int(owner_id) > 0 else 'missing or invalid'}")
    print(f"POLICY_URL: {'present' if values.get('POLICY_URL') else 'missing'}")
    print(f"POLICY_VERSION: {'present' if values.get('POLICY_VERSION') else 'missing'}")
    print(f"BOT_STORAGE_IN_RUSSIA: {'verified' if values.get('BOT_STORAGE_IN_RUSSIA') == 'true' else 'not verified'}")
    if not token:
        return
    try:
        bot = Bot(token=token)
    except Exception as error:
        print(f"Telegram token check: invalid format ({type(error).__name__})")
        return
    try:
        me = await bot.get_me()
        print(f"Telegram API getMe: success; bot username @{me.username}")
        webhook = await bot.get_webhook_info()
        print(f"Telegram webhook: {'configured' if webhook.url else 'not configured'}")
    except Exception as error:
        print(f"Telegram API getMe: failed ({type(error).__name__})")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
