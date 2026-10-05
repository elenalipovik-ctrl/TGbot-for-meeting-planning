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
        "POLICY_VERSION", "BOT_SERVER_TEST_VISITOR_ID",
    )
    if any(not values.get(key) for key in required):
        raise SystemExit("Server .env is incomplete")
    if values.get("BOT_STORAGE_IN_RUSSIA") != "true":
        raise SystemExit("Russian VPS storage has not been confirmed")
    if values.get("BOT_LOCAL_TEST_MODE") != "false":
        raise SystemExit("Local test mode must be disabled on the VPS")
    if values.get("BOT_SERVER_TEST_MODE") != "true":
        raise SystemExit("Only the restricted VPS test mode is permitted now")
    policy = urlsplit(values["POLICY_URL"])
    if policy.scheme != "https" or not policy.netloc:
        raise SystemExit("Public policy URL must use HTTPS")
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
    print("Server test configuration: ready; two Telegram accounts allowed")


if __name__ == "__main__":
    main()
