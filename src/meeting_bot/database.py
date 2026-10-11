"""SQLite setup and the one transactional primitive needed by later booking rules."""

import logging
import sqlite3
import time
from importlib.resources import files
from pathlib import Path

from .technical_log import log_event


SCHEMA_VERSION = 11
MIGRATIONS = {
    1: "migrations/001_initial.sql",
    2: "migrations/002_rules.sql",
    3: "migrations/003_cancel_actor.sql",
    4: "migrations/004_visitor_dialog.sql",
    5: "migrations/005_admin_dialog.sql",
    6: "migrations/006_reuse_metric.sql",
    7: "migrations/007_manual_blocks.sql",
    8: "migrations/008_telegram_only.sql",
    9: "migrations/009_shared_drafts.sql",
    10: "migrations/010_api_state.sql",
    11: "migrations/011_booking_start_options.sql",
}
ACTIVE_STATUSES = (
    "held", "pending", "awaiting_link", "creating_calendar", "confirmed", "attention"
)
GAP_SECONDS = 30 * 60


class SlotConflictError(Exception):
    """The requested interval conflicts with a live reservation or its gap."""


def connect(path: Path, *, busy_timeout_ms: int = 3000,
            logger: logging.Logger | None = None) -> sqlite3.Connection:
    path = Path(path)
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000,
                                     isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous = FULL")
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        log_event(logger, "database_error", code=type(error).__name__)
        raise
    log_event(logger, "database_opened")
    return connection


def _statements(source: str):
    statement = ""
    for line in source.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                yield statement
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete SQL migration")


def initialize_database(path: Path, *, logger: logging.Logger | None = None) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        log_event(logger, "database_error", code=type(error).__name__)
        raise
    connection = connect(path, logger=logger)
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            raise RuntimeError("SQLite WAL mode is unavailable")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this application")
        for target_version in range(version + 1, SCHEMA_VERSION + 1):
            # v2 rebuilds bookings to make the profile link nullable for retention.
            if target_version == 2:
                connection.execute("PRAGMA foreign_keys = OFF")
            migration = files("meeting_bot").joinpath(MIGRATIONS[target_version]).read_text(
                encoding="utf-8")
            if target_version == 3:
                # Early local v2 files already received this field before v3 existed.
                columns = {row[1] for row in connection.execute("PRAGMA table_info(bookings)")}
                if "cancel_requested_by" in columns:
                    migration = ""
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _statements(migration):
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {target_version}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            finally:
                if target_version == 2:
                    connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Migration left invalid foreign keys")
        log_event(logger, "schema_ready", code=f"v{SCHEMA_VERSION}")
    except (sqlite3.Error, RuntimeError, ValueError) as error:
        log_event(logger, "schema_error", code=type(error).__name__)
        raise
    finally:
        connection.close()


def reserve_slot(path: Path, *, visitor_id: int, starts_at: int, ends_at: int,
                 time_zone: str, hold_expires_at: int,
                 busy_timeout_ms: int = 3000,
                 logger: logging.Logger | None = None) -> int:
    """Create a held booking, rejecting overlaps and a 30-minute neighboring gap.

    Times are UTC Unix seconds. The caller handles business eligibility in stage 2.
    All competing writers enter BEGIN IMMEDIATE before the overlap check.
    """
    if ends_at <= starts_at or not time_zone or hold_expires_at <= 0:
        raise ValueError("Invalid interval or hold expiry")
    connection = connect(path, busy_timeout_ms=busy_timeout_ms, logger=logger)
    try:
        connection.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        conflict = connection.execute(
            f"SELECT 1 FROM bookings WHERE status IN ({placeholders}) "
            "AND ? < ends_at + ? AND starts_at < ? + ? LIMIT 1",
            (*ACTIVE_STATUSES, starts_at, GAP_SECONDS, ends_at, GAP_SECONDS),
        ).fetchone()
        if conflict:
            connection.execute("ROLLBACK")
            log_event(logger, "reservation_conflict", code="overlap_or_gap")
            raise SlotConflictError
        now = int(time.time())
        cursor = connection.execute(
            "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
            "hold_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'held', ?, ?, ?)",
            (visitor_id, starts_at, ends_at, time_zone, hold_expires_at, now, now),
        )
        connection.execute("COMMIT")
        booking_id = int(cursor.lastrowid)
        log_event(logger, "reservation_created", record_id=booking_id)
        return booking_id
    except SlotConflictError:
        raise
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if isinstance(error, sqlite3.IntegrityError) and "slot_conflict" in str(error):
            log_event(logger, "reservation_conflict", code="overlap_or_gap")
            raise SlotConflictError from error
        log_event(logger, "reservation_error", code=type(error).__name__)
        raise
    finally:
        connection.close()
