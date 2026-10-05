"""Read-only health check of the isolated local Telegram test."""

import json
import sqlite3
from collections import Counter
from pathlib import Path

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.runtime_mode import allowed_test_users


settings = read_env(ENV_FILE)
allowed_users = allowed_test_users(settings)
assert allowed_users is not None and len(allowed_users) == 2
database_path = Path(settings["BOT_DB_PATH"])
log_path = Path(settings["BOT_LOG_PATH"])
rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()]
allowed_fields = {"at", "level", "event", "code", "record_id"}
assert rows and all(set(row) <= allowed_fields for row in rows)
log_text = log_path.read_text(encoding="utf-8")
for key in ("TELEGRAM_BOT_TOKEN", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
            "POLICY_URL"):
    value = settings.get(key, "")
    assert not value or value not in log_text
assert "https://" not in log_text and "@" not in log_text
with sqlite3.connect(database_path) as connection:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert version == 8 and integrity == "ok"
    visitor_ids = {row[0] for row in connection.execute("SELECT telegram_id FROM visitors")}
    assert visitor_ids <= allowed_users
    booking_count = connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    booking_statuses = dict(connection.execute(
        "SELECT status, COUNT(*) FROM bookings GROUP BY status"))
    notice_statuses = dict(connection.execute(
        "SELECT status, COUNT(*) FROM notifications GROUP BY status"))
events = Counter(row["event"] for row in rows)
print("Local Telegram test: schema v8 and integrity ok; "
      f"bookings={booking_count}; polling starts={events['telegram_polling_started']}; "
      f"replies={events['telegram_reply_sent']}; safe log rows={len(rows)}")
print(f"Booking states={booking_statuses}; notification states={notice_statuses}")
