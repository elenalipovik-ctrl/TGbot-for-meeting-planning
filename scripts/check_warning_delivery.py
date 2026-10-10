"""Send one clearly marked test of the real calendar warning to the owner."""

from pathlib import Path

from meeting_bot.env_config import ENV_FILE, read_env
from meeting_bot.notification_delivery import Notice, render_notice, send_telegram
from meeting_bot.technical_log import configure_logging, log_event


def main() -> None:
    values = read_env(ENV_FILE)
    administrator_id = int(values["BOT_ADMIN_ID"])
    database = Path("data/local_test.sqlite")
    notice = Notice(0, 1, None, "telegram", "calendar_attention", 0)
    recipient, text = render_notice(database, notice, administrator_id)
    if recipient != administrator_id or not text.startswith("Требует внимания заявка №1:"):
        raise RuntimeError("Calendar warning did not target the administrator")
    send_telegram(values["TELEGRAM_BOT_TOKEN"], recipient,
                  "ТЕСТ доставки предупреждения. Действий не требуется.\n" + text)
    logger = configure_logging(Path(values.get("BOT_LOG_PATH") or "logs/technical.jsonl"))
    log_event(logger, "admin_warning_delivery_check", code="sent", record_id=1)
    print("Test calendar warning delivered to the configured administrator")


if __name__ == "__main__":
    main()
