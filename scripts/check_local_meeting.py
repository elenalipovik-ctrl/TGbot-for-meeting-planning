"""Verify the local test booking against Google without displaying guest details."""

import sqlite3
from pathlib import Path

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.google_auth import calendar_from_env
from meeting_bot.runtime_mode import allowed_test_users


settings = read_env(ENV_FILE)
allowed_test_users(settings)
with sqlite3.connect(Path(settings["BOT_DB_PATH"])) as connection:
    row = connection.execute(
        "SELECT b.id, b.status, b.starts_at, b.ends_at, b.email_snapshot, "
        "m.google_event_id FROM bookings b LEFT JOIN meetings m ON m.booking_id = b.id "
        "WHERE b.submitted_at IS NOT NULL ORDER BY b.id DESC LIMIT 1").fetchone()
    meeting_count = connection.execute(
        "SELECT COUNT(*) FROM meetings WHERE booking_id = ?", (row[0],)
    ).fetchone()[0] if row else 0
if row is None:
    raise RuntimeError("No submitted local test booking")
booking_id, status, starts_at, ends_at, email, event_id = row
if status != "confirmed" or not event_id or meeting_count != 1:
    raise RuntimeError("Local test booking is not yet confirmed exactly once")
calendar = calendar_from_env(ENV_FILE, settings.get("BOT_CALENDAR_ID") or "primary")
found = calendar.find_matching(event_id=event_id, booking_id=booking_id,
                               starts_at=starts_at, ends_at=ends_at, email=email)
assert found
print(f"Booking {booking_id}: one matching Google event with expected guest; no duplicate meeting")
