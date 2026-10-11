"""Offline, explicitly invoked release backup/migration/restore. Never runs at import."""
import argparse
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3

from .database import SCHEMA_VERSION, initialize_database


@contextmanager
def readonly(path):
    path = Path(path).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        yield connection
    finally:
        connection.close()


def require_schema(path, expected):
    with readonly(path) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != expected:
            raise RuntimeError("Offline database migration is required")
        connection.execute("SELECT 1 FROM bookings LIMIT 1").fetchone()


def verify(path, expected):
    with readonly(path) as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] != expected:
            raise RuntimeError("Unexpected schema version")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Database foreign key check failed")


def backup_database(path, backup):
    path, backup = Path(path), Path(backup)
    if path.is_symlink() or backup.is_symlink():
        raise ValueError("Database paths must not be symlinks")
    # O_EXCL: a previous recovery point can never be overwritten by a rerun.
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with readonly(path) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    with backup.open("r+b") as stream:
        os.fsync(stream.fileno())
    if os.name != "nt":
        directory = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return backup


def migrate(path, backup, *, maintenance_confirmed=False):
    if not maintenance_confirmed:
        raise ValueError("Stop bot, worker and API before migrating")
    require_schema(path, 8)
    verify(path, 8)
    backup_database(path, backup)
    verify(backup, 8)
    # Rehearse the full upgrade on a second copy before touching the database.
    rehearsal = Path(str(backup) + ".rehearsal")
    backup_database(backup, rehearsal)
    initialize_database(rehearsal)
    verify(rehearsal, SCHEMA_VERSION)
    initialize_database(path)
    verify(path, SCHEMA_VERSION)
    # Keep the rehearsal for comparison. It has the same protection as the backup.


def restore(path, backup, *, maintenance_confirmed=False):
    if not maintenance_confirmed:
        raise ValueError("Stop all writers before restoring")
    path, backup = Path(path), Path(backup)
    if path.is_symlink() or backup.is_symlink() or path.resolve() == backup.resolve():
        raise ValueError("Invalid restore paths")
    path.resolve(strict=True)
    verify(backup, 8)
    # SQLite backup API replaces the database safely without stale -wal copies.
    with readonly(backup) as source, closing(sqlite3.connect(path)) as target:
        source.backup(target)
    verify(path, 8)


def main():
    parser = argparse.ArgumentParser(description="Offline release database operation")
    parser.add_argument("action", choices=("migrate", "restore"))
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--maintenance-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        operation = migrate if args.action == "migrate" else restore
        operation(args.database, args.backup, maintenance_confirmed=args.maintenance_confirmed)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print("Release database operation failed; keep services stopped and inspect the backup.")
        return 1
    print("Release database operation completed; integrity and foreign keys verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
