"""Verify that the local test cancellation reached SQLite and Google Calendar."""

import sqlite3
from pathlib import Path

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.google_auth import calendar_from_env
from meeting_bot.runtime_mode import allowed_test_users


settings = read_env(ENV_FILE)
allowed_test_users(settings)
with sqlite3.connect(Path(settings["BOT_DB_PATH"])) as connection:
    row = connection.execute(
        "SELECT b.id, b.status, m.google_event_id, m.sync_status FROM bookings b "
        "LEFT JOIN meetings m ON m.booking_id = b.id "
        "WHERE b.submitted_at IS NOT NULL ORDER BY b.id DESC LIMIT 1"
    ).fetchone()
    notices = connection.execute(
        "SELECT kind, status FROM notifications WHERE booking_id = ? "
        "AND kind IN ('meeting_cancelled', 'visitor_cancelled')",
        (row[0],),
    ).fetchall() if row else []
if row is None:
    raise RuntimeError("No submitted local test booking")
booking_id, status, event_id, sync_status = row
if status != "cancelled_by_visitor" or sync_status != "cancelled" or not event_id:
    raise RuntimeError("Local test meeting has not been cancelled by visitor")
if sorted(notices) != sorted([("meeting_cancelled", "sent"),
                                  ("visitor_cancelled", "sent")]):
    raise RuntimeError("Cancellation notifications are not both sent")
calendar = calendar_from_env(ENV_FILE, settings.get("BOT_CALENDAR_ID") or "primary")
event = calendar._get(event_id)
print("Google event state after cancellation:",
      event.get("status", "unspecified") if event else "absent")
if event and event.get("status") != "cancelled":
    raise RuntimeError("Cancelled event is still active in Google Calendar")
print(f"Booking {booking_id}: cancelled in SQLite and Google; both Telegram notices sent")
