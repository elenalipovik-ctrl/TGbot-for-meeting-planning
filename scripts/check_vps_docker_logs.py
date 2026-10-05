"""Check Docker's own captured output for current server secrets without showing it."""

import json
import subprocess
from pathlib import Path

ENV_FILE = Path("/opt/meeting-bot/.env")
KEYS = {"TELEGRAM_BOT_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"}


def main() -> None:
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, separator, raw = line.partition("=")
        if separator and key in KEYS:
            values[key] = json.loads(raw) if raw.startswith('"') else raw
    if set(values) != KEYS or any(not value for value in values.values()):
        raise SystemExit("Server secret scan could not read all expected keys")
    result = subprocess.run(
        ["docker", "compose", "-f", "/opt/meeting-bot/compose.yaml",
         "--project-directory", "/opt/meeting-bot", "logs", "--no-color"],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise SystemExit("Could not inspect Docker logs")
    if any(value.encode("utf-8") in result.stdout for value in values.values()):
        raise SystemExit("A server secret appeared in Docker logs")
    print("Docker logs contain none of the four server credential values")


if __name__ == "__main__":
    main()
