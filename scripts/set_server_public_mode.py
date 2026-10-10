"""Switch the existing VPS .env to public Telegram access in place."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    env_file = root / ".env"
    if not env_file.is_file():
        raise SystemExit("Existing server .env is required")
    sys.path.insert(0, str(root / "src"))
    from meeting_bot.env_config import read_env, write_env_values

    values = read_env(env_file)
    expected_db = root / "data" / "bot.sqlite"
    actual_db = (root / (values.get("BOT_DB_PATH") or "")).resolve()
    policy = urlsplit(values.get("POLICY_URL") or "")
    if values.get("BOT_SERVER_TEST_MODE") != "true":
        raise SystemExit("Expected the currently restricted VPS mode")
    if values.get("BOT_STORAGE_IN_RUSSIA") != "true":
        raise SystemExit("Russian server storage must be verified")
    if values.get("BOT_LOCAL_TEST_MODE") != "false":
        raise SystemExit("Local test mode must be off on the VPS")
    if actual_db != expected_db or policy.scheme != "https" or policy.netloc != "meetcalendar.fin-partner.pro":
        raise SystemExit("Unexpected database or approved policy URL")
    write_env_values(env_file, {"BOT_SERVER_TEST_MODE": "false"})
    print("Public Telegram access configured in the existing .env")


if __name__ == "__main__":
    main()
