"""Owner-approved stage 3 live probe. Run by Codex, never with real client data."""

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path

from .calendar_flow import GAP_SECONDS, _conflicts
from .google_auth import calendar_from_env
from .env_config import ENV_FILE
from .technical_log import configure_logging


STATE_FILE = Path.cwd() / "data" / "stage3-test-event.json"
TEST_LINK = "https://example.com/meeting-test"


def _start(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("A time zone offset is required")
    return int(parsed.timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Google Calendar stage 3 test")
    parser.add_argument("action", choices=("probe", "create", "cancel"))
    parser.add_argument("--start", help="ISO local datetime with UTC offset")
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--email", help="Separate test attendee mailbox")
    parser.add_argument("--calendar-id", default="primary")
    args = parser.parse_args()
    logger = configure_logging(Path.cwd() / "logs" / "technical.jsonl")
    calendar = calendar_from_env(ENV_FILE, args.calendar_id, logger=logger)
    if args.action == "cancel":
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        calendar.delete_or_find_absent(state["event_id"], 0)
        STATE_FILE.unlink()
        print("Test event deletion confirmed by Google.")
        return
    if not args.start or args.minutes not in (30, 45, 60, 90):
        parser.error("A start with UTC offset and an allowed duration are required")
    starts_at = _start(args.start)
    ends_at = starts_at + args.minutes * 60
    busy = calendar.freebusy(starts_at - GAP_SECONDS, ends_at + GAP_SECONDS)
    print("Calendar availability:", "busy" if _conflicts(starts_at, ends_at, busy) else "free")
    if args.action == "probe":
        return
    if not args.email:
        parser.error("A separate test attendee email is required")
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if (state["starts_at"], state["ends_at"], state["email"]) != (
                starts_at, ends_at, args.email):
            raise RuntimeError("A different stage 3 test event is awaiting reconciliation")
    else:
        if _conflicts(starts_at, ends_at, busy):
            raise RuntimeError("The chosen test time is occupied")
        state = {"event_id": "m" + uuid.uuid4().hex, "starts_at": starts_at,
                 "ends_at": ends_at, "email": args.email}
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    event_id = calendar.create_or_find(
        event_id=state["event_id"], booking_id=0, starts_at=starts_at,
        ends_at=ends_at, email=args.email, video_url=TEST_LINK,
    )
    print("Test event and invitation accepted by Google; event ID:", event_id)


if __name__ == "__main__":
    main()
