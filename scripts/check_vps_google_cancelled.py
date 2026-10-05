"""Confirm the smoke-test event is absent from Google after visitor cancellation."""

import sqlite3
from pathlib import Path

from meeting_bot.env_config import read_env
from meeting_bot.google_auth import calendar_from_env


def main() -> None:
    root = Path("/app")
    with sqlite3.connect(f"file:{root / 'data' / 'bot.sqlite'}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT b.status, m.sync_status, m.google_event_id "
            "FROM bookings b JOIN meetings m ON m.booking_id = b.id WHERE b.id = 1"
        ).fetchone()
    if row is None or row[0] != "cancelled_by_visitor" or row[1] != "cancelled" or not row[2]:
        raise SystemExit("Server cancellation is incomplete")
    values = read_env(root / ".env")
    calendar = calendar_from_env(root / ".env", values.get("BOT_CALENDAR_ID") or "primary")
    event = calendar._get(row[2])
    if event is not None and event.get("status") != "cancelled":
        raise SystemExit("Cancelled event is still active in Google")
    print("Google event #1 is absent or cancelled; server booking and notices agree")


if __name__ == "__main__":
    main()
