"""Set the public policy URL inside the one protected .env file."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit


def set_policy_url(path: Path, url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "meetcalendar.fin-partner.pro":
        raise ValueError("Expected the approved HTTPS policy host")
    if not path.is_file():
        raise ValueError("Existing .env file is required")
    sys.path.insert(0, str(path.resolve().parent / "src"))
    from meeting_bot.env_config import read_env, write_env_values

    if "POLICY_URL" not in read_env(path):
        raise ValueError("Expected exactly one POLICY_URL setting")
    write_env_values(path, {"POLICY_URL": url})
    print("POLICY_URL updated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    parser.add_argument("url")
    arguments = parser.parse_args()
    set_policy_url(arguments.env_file, arguments.url)
