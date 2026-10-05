"""Run one restricted VPS process and record fatal failures without their text."""

import sys
from pathlib import Path

from check_server_readiness import main as check_server_readiness
from meeting_bot.technical_log import configure_logging, log_event


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("bot", "worker"):
        raise SystemExit("Choose bot or worker")
    role = sys.argv[1]
    try:
        check_server_readiness()
        sys.argv = [sys.argv[0]]
        if role == "bot":
            from meeting_bot.telegram_app import main as run_bot
            run_bot()
        else:
            from meeting_bot.worker import main as run_worker
            run_worker()
    except BaseException as error:
        try:
            logger = configure_logging(Path("logs/technical.jsonl"))
            log_event(logger, "server_process_failed", code=type(error).__name__)
        except Exception:
            pass
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
