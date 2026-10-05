"""Copy the reviewed bot code and its only secret file to the selected VPS.

No archive or temporary file containing secrets is created. SSH verifies the
server's saved host key, and the remote .env is writable only by the app UID.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from meeting_bot.env_config import read_env  # noqa: E402

REMOTE_ROOT = "/opt/meeting-bot"
ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def ssh(target: str, *command: str, input_bytes: bytes | None = None) -> None:
    result = subprocess.run(
        [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", target, *command,
        ],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        # Remote failure text could contain operational details; do not echo it.
        raise RuntimeError(f"SSH deployment step failed (exit {result.returncode})")


def code_archive() -> tuple[bytes, int]:
    paths = [ROOT / name for name in (
        ".dockerignore", "Dockerfile", "compose.yaml", "pyproject.toml", "README.md",
        "scripts/check_server_readiness.py", "scripts/run_server.py",
    )]
    paths.extend(
        path for path in (ROOT / "src").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    return buffer.getvalue(), len(paths)


def server_env() -> bytes:
    values = read_env(ROOT / ".env")
    visitor_id = values.get("BOT_SERVER_TEST_VISITOR_ID") or values.get("BOT_LOCAL_TEST_VISITOR_ID")
    if not visitor_id or not visitor_id.isdecimal():
        raise RuntimeError("The local .env has no numeric second Telegram test ID")
    required = (
        "TELEGRAM_BOT_TOKEN", "BOT_ADMIN_ID", "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "POLICY_URL", "POLICY_VERSION",
    )
    if any(not values.get(key) for key in required):
        raise RuntimeError("The local .env is incomplete for VPS testing")
    updates = {
        "BOT_STORAGE_IN_RUSSIA": "true",
        "BOT_LOCAL_TEST_MODE": "false",
        "BOT_SERVER_TEST_MODE": "true",
        "BOT_SERVER_TEST_VISITOR_ID": visitor_id,
        "BOT_DB_PATH": "data/bot.sqlite",
        "BOT_LOG_PATH": "logs/technical.jsonl",
    }
    lines: list[str] = []
    seen: set[str] = set()
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        match = ENV_LINE.match(line)
        if match and match.group(1) in updates:
            key = match.group(1)
            if key in seen:
                raise RuntimeError("Duplicate server .env key")
            lines.append(f"{key}={json.dumps(updates[key])}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={json.dumps(value)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="SSH user@host already in known_hosts")
    parser.add_argument("--code-only", action="store_true",
                        help="Update code without replacing the VPS .env or data")
    arguments = parser.parse_args()
    archive, file_count = code_archive()
    env_bytes = None if arguments.code_only else server_env()
    ssh(arguments.target, "sudo", "-n", "install", "-d", "-m", "0755", REMOTE_ROOT)
    ssh(arguments.target, "sudo", "-n", "tar", "-xz", "-C", REMOTE_ROOT, input_bytes=archive)
    if arguments.code_only:
        print(f"Updated {file_count} code files; the server .env and data were not touched")
        return
    ssh(
        arguments.target, "sudo", "-n", "install", "-d", "-m", "0700",
        f"{REMOTE_ROOT}/data", f"{REMOTE_ROOT}/logs",
    )
    ssh(
        arguments.target, "sudo", "-n", "chown", "10001:10001",
        f"{REMOTE_ROOT}/data", f"{REMOTE_ROOT}/logs",
    )
    # Only the initial deployment may create this file. A later deployment must
    # preserve the refresh token that Google might rotate on the VPS.
    ssh(arguments.target, "sudo", "-n", "test", "!", "-e", f"{REMOTE_ROOT}/.env")
    ssh(
        arguments.target, "sudo", "-n", "install", "-m", "0600",
        "/dev/null", f"{REMOTE_ROOT}/.env",
    )
    ssh(arguments.target, "sudo", "-n", "chown", "10001:10001", f"{REMOTE_ROOT}/.env")
    ssh(
        arguments.target, "sudo", "-n", "dd", f"of={REMOTE_ROOT}/.env",
        "status=none", input_bytes=env_bytes,
    )
    print(f"Transferred {file_count} code files and configured the protected server .env")


if __name__ == "__main__":
    main()
