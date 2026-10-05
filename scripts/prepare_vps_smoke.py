"""Create one clearly labeled server test template and future interval."""

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from meeting_bot.env_config import read_env
from meeting_bot.service import BookingService
from meeting_bot.technical_log import configure_logging

ROOT = Path("/app")
DATABASE = ROOT / "data" / "bot.sqlite"
NAME = "Проверка VPS"
START = int(datetime(2026, 10, 6, 16, 0, tzinfo=ZoneInfo("Europe/Moscow")).timestamp())
END = int(datetime(2026, 10, 6, 17, 0, tzinfo=ZoneInfo("Europe/Moscow")).timestamp())


def main() -> None:
    values = read_env(ROOT / ".env")
    admin_id = int(values["BOT_ADMIN_ID"])
    logger = configure_logging(ROOT / "logs" / "technical.jsonl")
    service = BookingService(DATABASE, administrator_id=admin_id, logger=logger)
    now = int(time.time())
    with sqlite3.connect(DATABASE) as connection:
        template = connection.execute(
            "SELECT id FROM meeting_templates WHERE name = ? AND is_active = 1", (NAME,)
        ).fetchone()
        interval = connection.execute(
            "SELECT id FROM availability_intervals WHERE starts_at = ? AND ends_at = ? "
            "AND is_published = 1", (START, END)
        ).fetchone()
    if template is None:
        service.create_template(
            admin_id, NAME, "Тестовая запись для проверки бота на сервере",
            "После проверки отмените тестовую встречу в боте.", now,
        )
    if interval is None:
        service.publish_interval(admin_id, START, END, "Europe/Moscow", now)
    print("Server smoke slot ready: 06.10.2026, 16:00-17:00 MSK")


if __name__ == "__main__":
    main()
