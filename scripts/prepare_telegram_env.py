"""Add stage 4 settings to the existing local .env without printing secrets."""

from meeting_bot.env_config import ENV_FILE, read_env, write_env_values


def main() -> None:
    existing = read_env(ENV_FILE)
    keys = ("TELEGRAM_BOT_TOKEN", "BOT_ADMIN_ID", "POLICY_URL", "POLICY_VERSION")
    missing = {key: "" for key in keys if key not in existing}
    if "BOT_STORAGE_IN_RUSSIA" not in existing:
        missing["BOT_STORAGE_IN_RUSSIA"] = "false"
    if missing:
        write_env_values(ENV_FILE, missing)
    print("Telegram and policy keys are present in the local .env; values were not displayed.")


if __name__ == "__main__":
    main()
