"""Send the owner-approved test visitor one booking button after the UI fix."""

from pathlib import Path

from meeting_bot.env_config import read_env
from meeting_bot.notification_delivery import send_telegram
from meeting_bot.runtime_mode import allowed_test_users


def main() -> None:
    values = read_env(Path("/app/.env"))
    allowed = allowed_test_users(values)
    visitor_id = int(values["BOT_SERVER_TEST_VISITOR_ID"])
    if allowed is None or visitor_id not in allowed:
        raise SystemExit("Server test visitor is not allowlisted")
    send_telegram(
        values["TELEGRAM_BOT_TOKEN"], visitor_id,
        "Доступ к записи открыт. Нажмите «Записаться», чтобы продолжить тест на сервере.",
        action="book",
    )
    print("Booking button delivered to the allowlisted test visitor")


if __name__ == "__main__":
    main()
