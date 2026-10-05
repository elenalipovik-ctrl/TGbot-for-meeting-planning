"""Check Google free/busy for the isolated local test interval, without event details."""

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.google_auth import calendar_from_env
from meeting_bot.runtime_mode import allowed_test_users
from meeting_bot.service import AccessDenied, BookingService


settings = read_env(ENV_FILE)
allowed_test_users(settings)
with sqlite3.connect(Path(settings["BOT_DB_PATH"])) as connection:
    row = connection.execute(
        "SELECT starts_at, ends_at FROM availability_intervals "
        "WHERE is_published = 1 ORDER BY starts_at LIMIT 1").fetchone()
if row is None:
    raise RuntimeError("No published local test interval")
calendar = calendar_from_env(ENV_FILE, settings.get("BOT_CALENDAR_ID") or "primary")
busy = calendar.freebusy(row[0], row[1])
print(f"Google Calendar free/busy succeeded; busy intervals in test window={len(busy)}")
service = BookingService(Path(settings["BOT_DB_PATH"]),
                         administrator_id=int(settings["BOT_ADMIN_ID"]))
now = int(time.time())
try:
    slots = service.list_slots(int(settings["BOT_LOCAL_TEST_VISITOR_ID"]), now,
                               30, "telemost", busy)
except AccessDenied:
    print("Visitor access is still awaiting administrator approval")
else:
    assert all(now + 2 * 3600 <= slot < now + 7 * 86400 for slot in slots)
    first = (datetime.fromtimestamp(slots[0], ZoneInfo("Europe/Moscow"))
             .strftime("%d.%m.%Y %H:%M МСК") if slots else "none")
    print(f"Future 30-minute slots={len(slots)}; first={first}")
