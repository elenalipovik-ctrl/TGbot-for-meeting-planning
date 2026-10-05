"""Verify that Google OAuth values occur only in the local .env file."""

from pathlib import Path

from meeting_bot.env_config import read_env


root = Path.cwd().resolve()
values = read_env(root / ".env")
keys = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
if any(not values.get(key) for key in keys):
    raise SystemExit("Google OAuth settings are incomplete in .env")
if (root / ".secrets").exists() or list(root.glob(".env-*")):
    raise SystemExit("Legacy or temporary credential files remain")

secret_bytes = [values[key].encode("utf-8") for key in keys]
excluded_dirs = {".venv", ".pytest_cache", "__pycache__", ".git"}
matches = []
for path in root.rglob("*"):
    if (not path.is_file() or path.name == ".env"
            or any(part in excluded_dirs for part in path.parts)):
        continue
    if any(secret in path.read_bytes() for secret in secret_bytes):
        matches.append(str(path.relative_to(root)))
if matches:
    raise SystemExit("Google OAuth values found outside .env in: " + ", ".join(matches))
print("Google OAuth values are present only in the local .env")
