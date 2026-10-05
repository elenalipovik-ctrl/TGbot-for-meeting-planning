"""Verify one booked Google event without showing the guest or meeting link."""

import sqlite3
from pathlib import Path

from meeting_bot.env_config import read_env
from meeting_bot.google_auth import calendar_from_env


def main() -> None:
    root = Path("/app")
    with sqlite3.connect(f"file:{root / 'data' / 'bot.sqlite'}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT b.starts_at, b.ends_at, b.email_snapshot, m.google_event_id, m.video_url "
            "FROM bookings b JOIN meetings m ON m.booking_id = b.id "
            "WHERE b.id = 1 AND b.status = 'confirmed' AND m.sync_status = 'synced'"
        ).fetchone()
    if row is None or any(value is None for value in row):
        raise SystemExit("Confirmed booking or Google event reference is missing")
    starts_at, ends_at, email, event_id, video_url = row
    values = read_env(root / ".env")
    calendar = calendar_from_env(root / ".env", values.get("BOT_CALENDAR_ID") or "primary")
    if not calendar.find_matching(
        event_id=event_id, booking_id=1, starts_at=starts_at,
        ends_at=ends_at, email=email,
    ):
        raise SystemExit("Google event is absent")
    event = calendar._get(event_id)
    if event is None or event.get("description") != video_url:
        raise SystemExit("Google event link does not match")
    print("Google event #1: time, guest and Telemost link match the server booking")


if __name__ == "__main__":
    main()
