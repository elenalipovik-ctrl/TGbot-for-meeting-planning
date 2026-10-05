"""Check local stage 5 storage and technical logs without printing private values."""

import json
import sqlite3
from pathlib import Path

from meeting_bot.env_config import read_env


ROOT = Path(__file__).resolve().parents[1]
log_path = ROOT / "logs" / "technical.jsonl"
database_path = ROOT / "data" / "bot.sqlite"
settings = read_env(ROOT / ".env")
assert not {"ADMIN_NOTIFICATION_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_TLS_MODE",
            "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"}.intersection(settings)
rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()]
allowed = {"at", "level", "event", "code", "record_id"}
assert rows and all(set(row) <= allowed for row in rows)
log_text = log_path.read_text(encoding="utf-8")
for key in ("TELEGRAM_BOT_TOKEN", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN",
            "GOOGLE_ACCESS_TOKEN", "ADMIN_NOTIFICATION_EMAIL", "POLICY_URL"):
    value = settings.get(key, "")
    assert not value or value not in log_text, f"Private setting appeared in log: {key}"
assert "https://" not in log_text and "@" not in log_text
with sqlite3.connect(database_path) as connection:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert version == 8 and integrity == "ok"
    assert connection.execute(
        "SELECT COUNT(*) FROM notifications WHERE channel = 'email'").fetchone()[0] == 0
print(f"Stage 5 local check passed: schema v{version}; log rows={len(rows)}; safe fields/values")
