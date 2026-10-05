"""Publish one future test template and interval in the isolated local database."""

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.runtime_mode import allowed_test_users
from meeting_bot.service import BookingService


settings = read_env(ENV_FILE)
allowed_test_users(settings)
database_path = Path(settings["BOT_DB_PATH"])
administrator_id = int(settings["BOT_ADMIN_ID"])
now = int(time.time())
service = BookingService(database_path, administrator_id=administrator_id)
service.initialize()
with sqlite3.connect(database_path) as connection:
    template = connection.execute(
        "SELECT id FROM meeting_templates WHERE name = 'Проверка записи' "
        "AND is_active = 1 ORDER BY id LIMIT 1").fetchone()
if template is None:
    service.create_template(administrator_id, "Проверка записи",
                            "Тестовая онлайн-встреча", "Только для проверки бота", now)
zone = ZoneInfo("Europe/Moscow")
local_now = datetime.fromtimestamp(now, zone)
for days_ahead in range(1, 7):
    day = (local_now + timedelta(days=days_ahead)).date()
    if day.weekday() == 2 or day.weekday() >= 5:
        continue
    start = datetime(day.year, day.month, day.day, 16, tzinfo=zone)
    end = datetime(day.year, day.month, day.day, 18, tzinfo=zone)
    if start.timestamp() >= now + 4 * 3600:
        break
else:
    raise RuntimeError("No future weekday in the test horizon")
with sqlite3.connect(database_path) as connection:
    exists = connection.execute(
        "SELECT 1 FROM availability_intervals WHERE starts_at = ? AND ends_at = ? "
        "AND is_published = 1", (int(start.timestamp()), int(end.timestamp()))).fetchone()
if exists is None:
    service.publish_interval(administrator_id, int(start.timestamp()),
                             int(end.timestamp()), "Europe/Moscow", now)
print("Local test interval ready: " + start.strftime("%d.%m.%Y 16:00–18:00 МСК"))
