"""Copy the owner's refreshed Google token between protected .env files via SSH stdin."""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "FinElena@45.144.178.2"
REMOTE_SCRIPT = "/tmp/meeting-bot-sync-google-token.py"
REMOTE_ENV = Path("/opt/meeting-bot/.env")


def install_from_stdin() -> None:
    token = sys.stdin.read().strip()
    if len(token) < 20 or any(char.isspace() for char in token):
        raise RuntimeError("Invalid refresh token input")
    destination = REMOTE_ENV
    if stat.S_IMODE(destination.stat().st_mode) != 0o600:
        raise RuntimeError("Server .env permissions are not 600")
    sys.path.insert(0, str(destination.parent / "src"))
    from meeting_bot.env_config import read_env, write_env_values

    if "GOOGLE_REFRESH_TOKEN" not in read_env(destination):
        raise RuntimeError("Expected exactly one server Google token setting")
    write_env_values(destination, {"GOOGLE_REFRESH_TOKEN": token})
    print("Server Google refresh token installed in .env")


def run_checked(command: list[str], *, input_bytes: bytes | None = None) -> None:
    result = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"Secure token transfer step failed (exit {result.returncode})")


def send_to_server() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from meeting_bot.env_config import read_env

    token = read_env(ROOT / ".env").get("GOOGLE_REFRESH_TOKEN")
    if not token:
        raise RuntimeError("Local .env has no Google refresh token")
    common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    run_checked(["scp", *common, str(Path(__file__)), f"{TARGET}:{REMOTE_SCRIPT}"])
    run_checked(["ssh", "-T", *common, TARGET, "sudo", "-n", "python3",
                 REMOTE_SCRIPT, "--install"], input_bytes=token.encode("utf-8"))
    print("New Google refresh token transferred without printing its value")


if __name__ == "__main__":
    if sys.argv[1:] == ["--install"]:
        install_from_stdin()
    elif not sys.argv[1:]:
        send_to_server()
    else:
        raise SystemExit("Usage: sync_google_refresh_token.py [--install]")
