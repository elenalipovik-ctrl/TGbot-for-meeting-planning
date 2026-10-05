"""Persistent jobs with leases, idempotent effects and safe retries."""

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .database import connect, initialize_database
from .env_config import ENV_FILE, read_env
from .notification_delivery import run_due_notifications
from .rules import add_calendar_months
from .service import (BookingService, PROCESSABLE_JOBS, StorageUnavailable, FRIENDLY_STORAGE_ERROR,
                      _audit_booking, _queue_job, _queue_notification)
from .technical_log import configure_logging, log_event


def schedule_cleanup(path: Path, now: int, *, logger: logging.Logger | None = None) -> None:
    connection = connect(path, logger=logger)
    try:
        key = datetime.fromtimestamp(now, timezone.utc).strftime("cleanup:%Y-%m-%d")
        _queue_job(connection, "cleanup", now, key)
        log_event(logger, "cleanup_scheduled")
    finally:
        connection.close()


def claim_due_job(path: Path, now: int, *, lease_seconds: int = 60,
                  logger: logging.Logger | None = None,
                  kinds: tuple[str, ...] = PROCESSABLE_JOBS) -> int | None:
    connection = connect(path, logger=logger)
    try:
        connection.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in kinds)
        row = connection.execute(
            f"SELECT id FROM jobs WHERE kind IN ({placeholders}) AND run_at <= ? "
            "AND (status = 'queued' OR (status = 'claimed' AND claimed_until <= ?)) "
            "ORDER BY run_at, id LIMIT 1",
            (*kinds, now, now),
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        job_id = int(row[0])
        connection.execute(
            "UPDATE jobs SET status = 'claimed', claimed_until = ?, attempts = attempts + 1 "
            "WHERE id = ?", (now + lease_seconds, job_id),
        )
        connection.execute("COMMIT")
        log_event(logger, "job_claimed", record_id=job_id)
        return job_id
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        log_event(logger, "job_claim_error", code=type(error).__name__)
        raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error
    finally:
        connection.close()


def _cleanup(connection: sqlite3.Connection, now: int) -> tuple[int, int]:
    zone = connection.execute(
        "SELECT value FROM app_settings WHERE key = 'time_zone'"
    ).fetchone()[0]
    booking_ids = [row[0] for row in connection.execute(
        "SELECT id, completed_at FROM bookings WHERE completed_at IS NOT NULL"
    ) if add_calendar_months(row[1], 2, zone) <= now]
    for booking_id in booking_ids:
        connection.execute("DELETE FROM meetings WHERE booking_id = ?", (booking_id,))
        connection.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    connection.execute("DELETE FROM drafts WHERE expires_at <= ?", (now,))
    visitor_ids = [row[0] for row in connection.execute(
        "SELECT telegram_id, last_used_at FROM visitors"
    ) if add_calendar_months(row[1], 2, zone) <= now]
    deleted_visitors = 0
    for visitor_id in visitor_ids:
        active = connection.execute(
            "SELECT 1 FROM bookings WHERE visitor_id = ? AND status IN "
            "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention') "
            "LIMIT 1", (visitor_id,),
        ).fetchone()
        if active:
            continue
        connection.execute(
            "UPDATE audit_actions SET actor_telegram_id = NULL WHERE actor_telegram_id = ?",
            (visitor_id,),
        )
        connection.execute("DELETE FROM visitors WHERE telegram_id = ?", (visitor_id,))
        deleted_visitors += 1
    return len(booking_ids), deleted_visitors


def _retry_calendar_job(path: Path, job_id: int, now: int,
                        logger: logging.Logger | None) -> None:
    connection = connect(path, logger=logger)
    try:
        connection.execute("BEGIN IMMEDIATE")
        attempts = connection.execute(
            "SELECT attempts FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
        delay = min(3600, 60 * 2 ** min(attempts, 6))
        connection.execute(
            "UPDATE jobs SET status = 'queued', claimed_until = NULL, run_at = ? "
            "WHERE id = ? AND status = 'claimed'", (now + delay, job_id),
        )
        connection.execute("COMMIT")
        log_event(logger, "calendar_job_requeued", code="google_error", record_id=job_id)
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def process_job(path: Path, job_id: int, now: int,
                *, logger: logging.Logger | None = None,
                calendar=None, administrator_id: int | None = None) -> bool:
    if calendar is not None:
        inspection = connect(path, logger=logger)
        try:
            job = inspection.execute(
                "SELECT kind, booking_id, status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            inspection.close()
        if job and job[0] == "calendar_cancel" and job[2] == "claimed":
            if administrator_id is None:
                raise ValueError("Administrator ID is required for calendar cancellation")
            from .calendar_flow import finish_calendar_cancellation
            from .google_calendar import CalendarError
            service = BookingService(path, administrator_id=administrator_id, logger=logger)
            try:
                finish_calendar_cancellation(service, calendar, job[1], now)
                log_event(logger, "job_completed", code="calendar_cancel", record_id=job_id)
                return True
            except CalendarError:
                _retry_calendar_job(path, job_id, now, logger)
                return False
    connection = connect(path, logger=logger)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT kind, booking_id, status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None or job[2] != "claimed":
            connection.execute("COMMIT")
            return False
        kind, booking_id, _ = job
        if kind == "hold_expire":
            changed = connection.execute(
                "UPDATE bookings SET status = 'expired', completed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'held' AND hold_expires_at <= ?",
                (now, now, booking_id, now),
            ).rowcount
            if changed:
                connection.execute("DELETE FROM drafts WHERE booking_id = ?", (booking_id,))
                _audit_booking(connection, None, booking_id, "hold_expired", now)
        elif kind == "request_expire":
            row = connection.execute(
                "SELECT visitor_id FROM bookings WHERE id = ? AND status IN "
                "('pending', 'awaiting_link') AND decision_deadline_at <= ?",
                (booking_id, now),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE bookings SET status = 'expired', completed_at = ?, updated_at = ? "
                    "WHERE id = ?", (now, now, booking_id),
                )
                _queue_notification(connection, booking_id, "telegram", "request_expired", row[0])
                _audit_booking(connection, None, booking_id, "request_expired", now)
        elif kind in ("reminder_24h", "reminder_2h"):
            row = connection.execute(
                "SELECT visitor_id FROM bookings WHERE id = ? AND status = 'confirmed' "
                "AND starts_at > ?", (booking_id, now),
            ).fetchone()
            if row:
                _queue_notification(connection, booking_id, "telegram", kind, row[0])
        elif kind == "cleanup":
            deleted_bookings, deleted_visitors = _cleanup(connection, now)
            next_run = now + 86400
            key = datetime.fromtimestamp(next_run, timezone.utc).strftime("cleanup:%Y-%m-%d")
            _queue_job(connection, "cleanup", next_run, key)
        else:
            raise RuntimeError("Unknown job kind")
        connection.execute(
            "UPDATE jobs SET status = 'done', claimed_until = NULL WHERE id = ?",
            (job_id,),
        )
        connection.execute("COMMIT")
        if kind == "cleanup":
            log_event(logger, "cleanup_completed", code=f"b{deleted_bookings}_v{deleted_visitors}")
        log_event(logger, "job_completed", code=kind, record_id=job_id)
        return True
    except (sqlite3.Error, RuntimeError) as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        log_event(logger, "job_error", code=type(error).__name__, record_id=job_id)
        if isinstance(error, sqlite3.OperationalError):
            raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error
        raise
    finally:
        connection.close()


def run_due_jobs(path: Path, now: int, *, limit: int = 100,
                 logger: logging.Logger | None = None, calendar=None,
                 administrator_id: int | None = None) -> int:
    completed = 0
    kinds = PROCESSABLE_JOBS + (("calendar_cancel",) if calendar is not None else ())
    for _ in range(limit):
        job_id = claim_due_job(path, now, logger=logger, kinds=kinds)
        if job_id is None:
            break
        if process_job(path, job_id, now, logger=logger, calendar=calendar,
                       administrator_id=administrator_id):
            completed += 1
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description="Process persisted meeting-bot jobs")
    parser.add_argument("--once", action="store_true", help="Process currently due jobs and exit")
    args = parser.parse_args()
    settings = read_env(ENV_FILE, required=False)
    from .runtime_mode import allowed_test_users
    allowed_recipients = (allowed_test_users(settings)
                          if settings.get("BOT_LOCAL_TEST_MODE") == "true" or
                          settings.get("BOT_STORAGE_IN_RUSSIA") == "true" else None)
    database_path = Path(settings.get("BOT_DB_PATH", "data/bot.sqlite")).resolve()
    log_path = Path(settings.get("BOT_LOG_PATH", "logs/technical.jsonl")).resolve()
    logger = configure_logging(log_path)
    initialize_database(database_path, logger=logger)
    calendar = None
    administrator_id = None
    if settings.get("GOOGLE_REFRESH_TOKEN") and settings.get("BOT_ADMIN_ID"):
        from .google_auth import calendar_from_env
        calendar = calendar_from_env(ENV_FILE,
                                     settings.get("BOT_CALENDAR_ID", "primary"), logger)
        administrator_id = int(settings["BOT_ADMIN_ID"])
    elif settings.get("GOOGLE_REFRESH_TOKEN"):
        log_event(logger, "calendar_worker_unconfigured", code="missing_admin_id")
    while True:
        now = int(time.time())
        try:
            schedule_cleanup(database_path, now, logger=logger)
            run_due_jobs(database_path, now, logger=logger, calendar=calendar,
                         administrator_id=administrator_id)
            if (settings.get("BOT_STORAGE_IN_RUSSIA") == "true" or
                    allowed_recipients is not None) and settings.get("BOT_ADMIN_ID"):
                run_due_notifications(database_path, now, settings, logger=logger,
                                      allowed_recipients=allowed_recipients)
        except (sqlite3.Error, StorageUnavailable) as error:
            log_event(logger, "worker_error", code=type(error).__name__)
            if args.once:
                raise SystemExit(1) from error
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
