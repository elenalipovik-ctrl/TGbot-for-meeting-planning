"""Read-only runtime checks inside a running VPS container; print no user data."""

import os
import sqlite3
import stat
from pathlib import Path


def check_mode(path: Path, mode: int) -> None:
    details = path.stat()
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != mode:
        raise SystemExit(f"Incorrect ownership or mode: {path.name}")


def main() -> None:
    if os.geteuid() != 10001:
        raise SystemExit("Unexpected container user")
    check_mode(Path("/app/.env"), 0o600)
    check_mode(Path("/app/data"), 0o700)
    check_mode(Path("/app/logs"), 0o700)
    database = Path("/app/data/bot.sqlite")
    if not database.is_file() or database.stat().st_uid != os.geteuid():
        raise SystemExit("Persistent SQLite file is missing or has the wrong owner")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("SQLite integrity check failed")
    print("VPS runtime: permissions and SQLite integrity OK")


if __name__ == "__main__":
    main()
