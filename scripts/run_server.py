"""Run one restricted VPS process and record fatal failures without their text."""

import sys
from pathlib import Path

from check_server_readiness import main as check_server_readiness
from meeting_bot.technical_log import configure_logging, log_event


def main() -> None:
    if (len(sys.argv) not in (2, 3) or sys.argv[1] not in ("bot", "worker", "api")
            or (len(sys.argv) == 3 and sys.argv[2] != "--check")):
        raise SystemExit("Choose bot, worker or api, optionally --check")
    role = sys.argv[1]
    try:
        check_server_readiness()
        from meeting_bot.env_config import read_env
        from meeting_bot.database import SCHEMA_VERSION
        from meeting_bot.release_database import require_schema
        values = read_env()
        require_schema(Path(values["BOT_DB_PATH"]), SCHEMA_VERSION)
        if role == "api" and len(sys.argv) == 3:
            from meeting_bot.api.production import build_app
            build_app()
        if len(sys.argv) == 3:
            print("Server process: ready")
            return
        sys.argv = [sys.argv[0]]
        if role == "bot":
            from meeting_bot.telegram_app import main as run_bot
            run_bot()
        elif role == "api":
            from meeting_bot.api.production import main as run_api
            run_api()
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
