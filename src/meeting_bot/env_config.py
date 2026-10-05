"""Read the project's local .env without copying secrets into logs or other stores."""

import json
import os
import re
from pathlib import Path

if os.name != "nt":
    import fcntl


ENV_FILE = Path.cwd() / ".env"
KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def _entry(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    key, separator, raw = line.partition("=")
    key = key.strip()
    if not separator or not KEY_PATTERN.fullmatch(key):
        raise ValueError("Invalid .env entry")
    raw = raw.strip()
    if raw.startswith('"'):
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError("Invalid .env value")
    else:
        value = raw
    return key, value


def read_env(path: Path = ENV_FILE, *, required: bool = True) -> dict[str, str]:
    if not path.exists():
        if required:
            raise RuntimeError("Local .env file is missing")
        return {}
    with path.open("r", encoding="utf-8-sig") as stream:
        if os.name != "nt":
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        lines = stream.read().splitlines()
    values: dict[str, str] = {}
    for line in lines:
        entry = _entry(line)
        if entry is None:
            continue
        key, value = entry
        if key in values:
            raise ValueError("Duplicate .env key")
        values[key] = value
    return values


def require_keys(values: dict[str, str], *keys: str) -> None:
    if any(not values.get(key) for key in keys):
        raise RuntimeError("Google OAuth settings are incomplete in .env")


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Update only .env, preserving unrelated settings and comments."""
    if any(not KEY_PATTERN.fullmatch(key) for key in updates):
        raise ValueError("Invalid .env key")
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("r+" if existed else "x+", encoding="utf-8", newline="\n") as stream:
        if os.name != "nt":
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        if os.name != "nt" and not existed:
            os.fchmod(stream.fileno(), 0o600)
        old_lines = stream.read().lstrip("\ufeff").splitlines()
        seen: set[str] = set()
        new_lines: list[str] = []
        for line in old_lines:
            entry = _entry(line)
            if entry is None:
                new_lines.append(line)
                continue
            key, _ = entry
            if key in seen:
                raise ValueError("Duplicate .env key")
            seen.add(key)
            if key in updates:
                new_lines.append(f"{key}={json.dumps(updates[key], ensure_ascii=False)}")
            else:
                new_lines.append(line)
        for key, value in updates.items():
            if key not in seen:
                new_lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        stream.seek(0)
        stream.write("\n".join(new_lines) + "\n")
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
