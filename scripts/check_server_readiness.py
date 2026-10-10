"""Fail closed before either VPS service starts, without printing secrets."""

import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.runtime_mode import allowed_test_users


def main() -> None:
    root = Path.cwd().resolve()
    values = read_env(ENV_FILE)
    required = (
        "TELEGRAM_BOT_TOKEN", "BOT_ADMIN_ID", "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "POLICY_URL",
        "POLICY_VERSION",
    )
    if any(not values.get(key) for key in required):
        raise SystemExit("Server .env is incomplete")
    if values.get("BOT_STORAGE_IN_RUSSIA") != "true":
        raise SystemExit("Russian VPS storage has not been confirmed")
    if values.get("BOT_LOCAL_TEST_MODE") != "false":
        raise SystemExit("Local test mode must be disabled on the VPS")
    mode = values.get("BOT_SERVER_TEST_MODE")
    if mode not in ("true", "false"):
        raise SystemExit("Choose the restricted or public VPS mode explicitly")
    if mode == "true" and not values.get("BOT_SERVER_TEST_VISITOR_ID"):
        raise SystemExit("Restricted VPS mode needs a test visitor ID")
    policy = urlsplit(values["POLICY_URL"])
    if policy.scheme != "https" or policy.netloc != "meetcalendar.fin-partner.pro":
        raise SystemExit("Use the approved HTTPS policy host")
    if Path(values.get("BOT_DB_PATH") or "").resolve() != root / "data" / "bot.sqlite":
        raise SystemExit("Server database must be data/bot.sqlite")
    allowed_test_users(values)
    if Path(values.get("BOT_LOG_PATH") or "").resolve() != root / "logs" / "technical.jsonl":
        raise SystemExit("Server technical log must be logs/technical.jsonl")
    if os.name != "nt":
        env_stat = ENV_FILE.stat()
        if stat.S_IMODE(env_stat.st_mode) != 0o600 or env_stat.st_uid != os.geteuid():
            raise SystemExit("Server .env must be writable only by the app user")
        for name in ("data", "logs"):
            directory = root / name
            if not directory.is_dir():
                raise SystemExit(f"Server directory is missing: {name}")
            directory_stat = directory.stat()
            if (stat.S_IMODE(directory_stat.st_mode) != 0o700 or
                    directory_stat.st_uid != os.geteuid()):
                raise SystemExit(f"Server directory must belong only to the app user: {name}")
    print("Server configuration: ready; " +
          ("two Telegram accounts allowed" if mode == "true" else "public Telegram access"))


if __name__ == "__main__":
    main()
