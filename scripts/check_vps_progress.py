"""Show aggregate smoke-test state without contacts or Telegram IDs."""

import sqlite3


def main() -> None:
    with sqlite3.connect("file:/app/data/bot.sqlite?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT id, status FROM bookings ORDER BY id"
        ).fetchall()
        notices = connection.execute(
            "SELECT kind, status FROM notifications WHERE booking_id = 1 ORDER BY id"
        ).fetchall()
    print("Booking states:", ", ".join(f"#{ident}={status}" for ident, status in rows) or "none")
    print("Booking #1 notices:", ", ".join(f"{kind}={status}" for kind, status in notices) or "none")


if __name__ == "__main__":
    main()
