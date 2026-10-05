"""Stage 2 booking operations. External availability is supplied as data."""

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from .database import connect, initialize_database
from .rules import (
    Availability, BusyInterval, ReviewSchedule, ReviewWindow, add_calendar_months,
    allowed_durations, available_slots, can_cancel_confirmed, decision_deadline,
)
from .technical_log import log_event


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TERMINAL_STATUSES = (
    "rejected", "expired", "cancelled_by_visitor", "cancelled_by_admin"
)
PROCESSABLE_JOBS = ("hold_expire", "request_expire", "reminder_24h", "reminder_2h", "cleanup")
FRIENDLY_STORAGE_ERROR = "Не удалось сохранить действие. Попробуйте ещё раз позднее."


class RuleError(Exception):
    """A user action does not satisfy a booking rule."""


class AccessDenied(RuleError):
    """The visitor or actor lacks the needed rights."""


class SlotUnavailable(RuleError):
    """The chosen slot is no longer available."""


class StorageUnavailable(Exception):
    """The database is temporarily unavailable; safe to show to the user."""


@dataclass(frozen=True)
class SavedContacts:
    name: str
    email: str
    phone: str | None
    username: str | None


@dataclass(frozen=True)
class CalendarBooking:
    booking_id: int
    status: str
    starts_at: int
    ends_at: int
    email: str
    video_url: str
    google_event_id: str | None
    cancel_requested_at: int | None


def _queue_job(connection: sqlite3.Connection, kind: str, run_at: int,
               dedup_key: str, booking_id: int | None = None) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO jobs (booking_id, kind, run_at, status, dedup_key) "
        "VALUES (?, ?, ?, 'queued', ?)",
        (booking_id, kind, run_at, dedup_key),
    )


def _queue_notification(connection: sqlite3.Connection, booking_id: int,
                        channel: str, kind: str, recipient_id: int | None = None) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO notifications "
        "(booking_id, visitor_id, channel, kind, status, dedup_key) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        (booking_id, recipient_id, channel, kind, f"{kind}:{channel}:{booking_id}"),
    )


def _queue_access_notification(connection: sqlite3.Connection, request_id: int | str,
                               visitor_id: int, channel: str, kind: str) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO notifications "
        "(visitor_id, channel, kind, status, dedup_key) "
        "VALUES (?, ?, ?, 'queued', ?)",
        (visitor_id, channel, kind, f"{kind}:{channel}:{request_id}"),
    )


def _audit_booking(connection: sqlite3.Connection, actor_id: int | None,
                   booking_id: int, action: str, now: int) -> None:
    connection.execute(
        "INSERT INTO audit_actions (actor_telegram_id, booking_id, action, created_at) "
        "VALUES (?, ?, ?, ?)", (actor_id, booking_id, action, now),
    )


def _manual_blocked(connection: sqlite3.Connection, starts_at: int,
                    ends_at: int) -> bool:
    return connection.execute(
        "SELECT 1 FROM manual_blocks WHERE starts_at < ? AND ends_at > ? LIMIT 1",
        (ends_at, starts_at),
    ).fetchone() is not None


class BookingService:
    def __init__(self, path: Path, *, administrator_id: int,
                 logger: logging.Logger | None = None, busy_timeout_ms: int = 3000):
        self.path = Path(path)
        self.administrator_id = administrator_id
        self.logger = logger
        self.busy_timeout_ms = busy_timeout_ms

    def initialize(self) -> None:
        initialize_database(self.path, logger=self.logger)

    def _connect(self) -> sqlite3.Connection:
        try:
            return connect(self.path, busy_timeout_ms=self.busy_timeout_ms, logger=self.logger)
        except sqlite3.OperationalError as error:
            log_event(self.logger, "storage_unavailable", code=type(error).__name__)
            raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error

    def _require_admin(self, actor_id: int) -> None:
        if actor_id != self.administrator_id:
            log_event(self.logger, "access_denied", code="admin_only")
            raise AccessDenied("Действие доступно только администратору.")

    def _require_self(self, actor_id: int, visitor_id: int) -> None:
        if actor_id != visitor_id:
            log_event(self.logger, "access_denied", code="other_profile")
            raise AccessDenied("Доступны только собственные данные.")

    def _schedule(self, connection: sqlite3.Connection) -> ReviewSchedule:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone'"
        ).fetchone()
        windows = tuple(ReviewWindow(*item) for item in connection.execute(
            "SELECT weekday, starts_minute, ends_minute FROM review_windows "
            "ORDER BY weekday, starts_minute"
        ))
        return ReviewSchedule(row[0], windows)

    def review_schedule(self) -> ReviewSchedule:
        connection = self._connect()
        try:
            return self._schedule(connection)
        finally:
            connection.close()

    def set_review_schedule(self, actor_id: int, schedule: ReviewSchedule, now: int) -> None:
        self._require_admin(actor_id)
        if not schedule.windows:
            raise ValueError("Review hours cannot be empty")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'time_zone'",
                (schedule.time_zone, now),
            )
            connection.execute("DELETE FROM review_windows")
            connection.executemany(
                "INSERT INTO review_windows (weekday, starts_minute, ends_minute) VALUES (?, ?, ?)",
                [(w.weekday, w.starts_minute, w.ends_minute) for w in schedule.windows],
            )
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, action, created_at) "
                "VALUES (?, 'review_hours_changed', ?)", (actor_id, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "review_hours_changed")
        except sqlite3.OperationalError as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            log_event(self.logger, "storage_unavailable", code=type(error).__name__)
            raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error
        finally:
            connection.close()

    def request_access(self, visitor_id: int, username: str | None, now: int) -> int:
        if visitor_id <= 0:
            raise ValueError("Invalid Telegram ID")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT access_status FROM visitors WHERE telegram_id = ?", (visitor_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO visitors (telegram_id, username, access_status, created_at, last_used_at) "
                    "VALUES (?, ?, 'pending', ?, ?)", (visitor_id, username, now, now),
                )
            elif row[0] == "blocked":
                raise AccessDenied("Доступ к записи заблокирован.")
            elif row[0] == "allowed":
                connection.execute("COMMIT")
                return 0
            else:
                connection.execute(
                    "UPDATE visitors SET username = ?, last_used_at = ? WHERE telegram_id = ?",
                    (username, now, visitor_id),
                )
            pending = connection.execute(
                "SELECT id FROM access_requests WHERE visitor_id = ? AND status = 'pending' "
                "ORDER BY id DESC LIMIT 1", (visitor_id,)
            ).fetchone()
            if pending:
                request_id = pending[0]
            else:
                request_id = connection.execute(
                    "INSERT INTO access_requests (visitor_id, status, requested_at) "
                    "VALUES (?, 'pending', ?)", (visitor_id, now),
                ).lastrowid
                _queue_access_notification(connection, request_id, visitor_id,
                                           "telegram", "admin_access_request")
            connection.execute("COMMIT")
            log_event(self.logger, "access_requested", record_id=request_id)
            return int(request_id)
        except (sqlite3.Error, RuleError) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.OperationalError):
                log_event(self.logger, "storage_unavailable", code=type(error).__name__)
                raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error
            raise
        finally:
            connection.close()

    def decide_access(self, actor_id: int, visitor_id: int, status: str, now: int) -> None:
        self._require_admin(actor_id)
        if status not in ("allowed", "rejected", "blocked"):
            raise ValueError("Invalid access decision")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE visitors SET access_status = ? WHERE telegram_id = ?",
                (status, visitor_id),
            ).rowcount
            if not changed:
                raise RuleError("Запрос доступа не найден.")
            pending_ids = [item[0] for item in connection.execute(
                "SELECT id FROM access_requests WHERE visitor_id = ? AND status = 'pending'",
                (visitor_id,),
            )]
            connection.execute(
                "UPDATE access_requests SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE visitor_id = ? AND status = 'pending'",
                (status, now, actor_id, visitor_id),
            )
            for request_id in pending_ids:
                _queue_access_notification(connection, request_id, visitor_id,
                                           "telegram", f"access_{status}")
            if not pending_ids:
                _queue_access_notification(connection, f"{visitor_id}:{now}", visitor_id,
                                           "telegram", f"access_{status}")
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, action, created_at) "
                "VALUES (?, ?, ?)", (actor_id, f"access_{status}", now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "access_decided", code=status)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def record_consent(self, visitor_id: int, policy_version: str, now: int) -> None:
        if not policy_version.strip():
            raise ValueError("Policy version is required")
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO consents (visitor_id, policy_version, accepted_at, action) "
                "VALUES (?, ?, ?, 'accepted')", (visitor_id, policy_version, now),
            )
            log_event(self.logger, "consent_recorded")
        finally:
            connection.close()

    def withdraw_consent(self, actor_id: int, visitor_id: int,
                         policy_version: str, now: int) -> None:
        self._require_self(actor_id, visitor_id)
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO consents (visitor_id, policy_version, accepted_at, action) "
                "VALUES (?, ?, ?, 'withdrawn')", (visitor_id, policy_version, now),
            )
            log_event(self.logger, "consent_withdrawn")
        finally:
            connection.close()

    def create_template(self, actor_id: int, name: str, description: str,
                        instructions: str, now: int) -> int:
        self._require_admin(actor_id)
        if not name.strip():
            raise ValueError("Template name is required")
        connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO meeting_templates (name, description, instructions, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)", (name.strip(), description.strip(), instructions.strip(), now, now),
            )
            log_event(self.logger, "template_created", record_id=cursor.lastrowid)
            return int(cursor.lastrowid)
        finally:
            connection.close()

    def update_template(self, actor_id: int, template_id: int, *, name: str,
                        description: str, instructions: str, is_active: bool,
                        now: int) -> None:
        self._require_admin(actor_id)
        if not name.strip():
            raise ValueError("Template name is required")
        connection = self._connect()
        try:
            changed = connection.execute(
                "UPDATE meeting_templates SET name = ?, description = ?, instructions = ?, "
                "is_active = ?, updated_at = ? WHERE id = ?",
                (name.strip(), description.strip(), instructions.strip(),
                 int(is_active), now, template_id),
            ).rowcount
            if not changed:
                raise RuleError("Шаблон не найден.")
            log_event(self.logger, "template_updated", record_id=template_id)
        finally:
            connection.close()

    def publish_interval(self, actor_id: int, starts_at: int, ends_at: int,
                         time_zone: str, now: int, *, manual_exception: bool = False) -> int:
        self._require_admin(actor_id)
        if starts_at >= ends_at or starts_at < now or starts_at >= now + 7 * 86400:
            raise RuleError("Интервал должен начинаться в ближайшие 7 дней.")
        zone = ZoneInfo(time_zone)
        from datetime import datetime
        start_date = datetime.fromtimestamp(starts_at, zone).date()
        end_date = datetime.fromtimestamp(ends_at - 1, zone).date()
        if start_date != end_date:
            raise RuleError("Интервал должен помещаться в один день.")
        if start_date.weekday() in (2, 5, 6) and not manual_exception:
            raise RuleError("Для закрытого дня требуется ручное исключение.")
        connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO availability_intervals "
                "(starts_at, ends_at, time_zone, is_manual_exception, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (starts_at, ends_at, time_zone, int(manual_exception), now),
            )
            log_event(self.logger, "availability_published", record_id=cursor.lastrowid)
            return int(cursor.lastrowid)
        finally:
            connection.close()

    def close_interval(self, actor_id: int, interval_id: int, now: int) -> list[int]:
        self._require_admin(actor_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE availability_intervals SET is_published = 0 WHERE id = ?",
                (interval_id,),
            )
            affected = [row[0] for row in connection.execute(
                "SELECT id FROM bookings WHERE availability_id = ? AND status IN "
                "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')",
                (interval_id,),
            )]
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, action, created_at) "
                "VALUES (?, 'availability_closed', ?)", (actor_id, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "availability_closed", record_id=interval_id)
            return affected
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def close_day(self, actor_id: int, local_day: date,
                  time_zone: str, now: int) -> list[int]:
        """Unpublish every interval on a local date; return affected booking IDs."""
        self._require_admin(actor_id)
        from datetime import datetime, time, timedelta
        zone = ZoneInfo(time_zone)
        start = int(datetime.combine(local_day, time.min, tzinfo=zone).timestamp())
        end = int(datetime.combine(local_day + timedelta(days=1), time.min,
                                   tzinfo=zone).timestamp())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            interval_ids = [row[0] for row in connection.execute(
                "SELECT id FROM availability_intervals WHERE starts_at >= ? AND starts_at < ? "
                "AND time_zone = ? AND is_published = 1", (start, end, time_zone),
            )]
            affected: set[int] = set()
            for interval_id in interval_ids:
                connection.execute(
                    "UPDATE availability_intervals SET is_published = 0 WHERE id = ?",
                    (interval_id,),
                )
                affected.update(row[0] for row in connection.execute(
                    "SELECT id FROM bookings WHERE availability_id = ? AND status IN "
                    "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')",
                    (interval_id,),
                ))
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, action, created_at) "
                "VALUES (?, 'day_closed', ?)", (actor_id, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "day_closed")
            return sorted(affected)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def block_time(self, actor_id: int, starts_at: int, ends_at: int,
                   now: int) -> tuple[int, list[int]]:
        """Hide one precise time range without cancelling existing bookings."""
        self._require_admin(actor_id)
        if starts_at < now or starts_at >= now + 7 * 86400 or ends_at <= starts_at:
            raise RuleError("Закрываемое время должно быть в ближайшие 7 дней.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            block_id = connection.execute(
                "INSERT INTO manual_blocks (starts_at, ends_at, created_at) "
                "VALUES (?, ?, ?)", (starts_at, ends_at, now),
            ).lastrowid
            affected = [row[0] for row in connection.execute(
                "SELECT id FROM bookings WHERE status IN "
                "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention') "
                "AND starts_at < ? AND ends_at > ?", (ends_at, starts_at),
            )]
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, action, created_at) "
                "VALUES (?, 'time_blocked', ?)", (actor_id, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "time_blocked", record_id=block_id)
            return int(block_id), affected
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def unblock_time(self, actor_id: int, block_id: int, now: int) -> None:
        self._require_admin(actor_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("DELETE FROM manual_blocks WHERE id = ?",
                                         (block_id,)).rowcount
            if not changed:
                raise RuleError("Закрытое время не найдено.")
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, action, created_at) "
                "VALUES (?, 'time_unblocked', ?)", (actor_id, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "time_unblocked", record_id=block_id)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def list_slots(self, visitor_id: int, now: int, duration_minutes: int, video_service: str,
                   google_busy: list[BusyInterval] | None = None) -> list[int]:
        connection = self._connect()
        try:
            visitor = connection.execute(
                "SELECT access_status FROM visitors WHERE telegram_id = ?", (visitor_id,)
            ).fetchone()
            if visitor is None or visitor[0] != "allowed":
                log_event(self.logger, "access_denied", code="slots")
                raise AccessDenied("Сначала требуется разрешение на запись.")
            schedule = self._schedule(connection)
            intervals = [Availability(*row) for row in connection.execute(
                "SELECT id, starts_at, ends_at, time_zone, is_published, is_manual_exception "
                "FROM availability_intervals WHERE ends_at > ? AND starts_at < ?",
                (now, now + 7 * 86400),
            )]
            occupied = [BusyInterval(*row) for row in connection.execute(
                "SELECT starts_at, ends_at FROM bookings WHERE status IN "
                "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention') "
                "AND (status != 'held' OR hold_expires_at > ?)", (now,),
            )]
            result = available_slots(now, intervals, duration_minutes, video_service,
                                      schedule, occupied + (google_busy or []))
            result = [slot for slot in result if not _manual_blocked(
                connection, slot, slot + duration_minutes * 60)]
            log_event(self.logger, "slots_calculated", code="ok")
            return result
        finally:
            connection.close()

    def create_hold(self, visitor_id: int, template_id: int, interval_id: int,
                    starts_at: int, duration_minutes: int, video_service: str,
                    now: int, google_busy: list[BusyInterval] | None = None) -> int:
        if duration_minutes not in allowed_durations(video_service):
            raise RuleError("Эта длительность недоступна для выбранного сервиса.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_holds(connection, now)
            visitor = connection.execute(
                "SELECT access_status FROM visitors WHERE telegram_id = ?", (visitor_id,)
            ).fetchone()
            if visitor is None or visitor[0] != "allowed":
                raise AccessDenied("Сначала требуется разрешение на запись.")
            template = connection.execute(
                "SELECT is_active FROM meeting_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if template is None or not template[0]:
                raise RuleError("Шаблон встречи недоступен.")
            interval_row = connection.execute(
                "SELECT id, starts_at, ends_at, time_zone, is_published, is_manual_exception "
                "FROM availability_intervals WHERE id = ?", (interval_id,)
            ).fetchone()
            if interval_row is None:
                raise SlotUnavailable("Выбранное время больше недоступно.")
            interval = Availability(*interval_row)
            occupied = [BusyInterval(*row) for row in connection.execute(
                "SELECT starts_at, ends_at FROM bookings WHERE status IN "
                "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')"
            )]
            slots = available_slots(now, [interval], duration_minutes, video_service,
                                    self._schedule(connection), occupied + (google_busy or []))
            if starts_at not in slots:
                raise SlotUnavailable("Выбранное время больше недоступно.")
            ends_at = starts_at + duration_minutes * 60
            if _manual_blocked(connection, starts_at, ends_at):
                raise SlotUnavailable("Выбранное время закрыто администратором.")
            expires_at = now + 600
            booking_id = connection.execute(
                "INSERT INTO bookings "
                "(visitor_id, template_id, availability_id, starts_at, ends_at, time_zone, "
                "status, hold_expires_at, video_service, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'held', ?, ?, ?, ?)",
                (visitor_id, template_id, interval_id, starts_at, ends_at,
                 interval.time_zone, expires_at, video_service, now, now),
            ).lastrowid
            connection.execute(
                "INSERT INTO drafts "
                "(visitor_id, step, template_id, availability_id, starts_at, ends_at, "
                "time_zone, video_service, expires_at, created_at, updated_at, booking_id) "
                "VALUES (?, 'contacts', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (visitor_id, template_id, interval_id, starts_at, ends_at,
                 interval.time_zone, video_service, expires_at, now, now, booking_id),
            )
            _queue_job(connection, "hold_expire", expires_at, f"hold:{booking_id}", booking_id)
            _audit_booking(connection, visitor_id, booking_id, "hold_created", now)
            connection.execute("COMMIT")
            log_event(self.logger, "hold_created", record_id=booking_id)
            return int(booking_id)
        except (RuleError, sqlite3.Error) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.IntegrityError) and "slot_conflict" in str(error):
                log_event(self.logger, "hold_rejected", code="conflict")
                raise SlotUnavailable("Выбранное время больше недоступно.") from error
            if isinstance(error, sqlite3.OperationalError):
                log_event(self.logger, "storage_unavailable", code=type(error).__name__)
                raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error
            if isinstance(error, RuleError):
                log_event(self.logger, "hold_rejected", code=type(error).__name__)
            raise
        finally:
            connection.close()

    def _expire_holds(self, connection: sqlite3.Connection, now: int) -> None:
        ids = [row[0] for row in connection.execute(
            "SELECT id FROM bookings WHERE status = 'held' AND hold_expires_at <= ?",
            (now,),
        )]
        if ids:
            connection.executemany(
                "UPDATE bookings SET status = 'expired', completed_at = ?, updated_at = ? "
                "WHERE id = ?", [(now, now, item) for item in ids],
            )
            connection.executemany("DELETE FROM drafts WHERE booking_id = ?", [(item,) for item in ids])
            connection.executemany(
                "UPDATE jobs SET status = 'done', claimed_until = NULL "
                "WHERE booking_id = ? AND kind = 'hold_expire'",
                [(item,) for item in ids],
            )
            for item in ids:
                _audit_booking(connection, None, item, "hold_expired", now)
            log_event(self.logger, "holds_expired", code="sweep")

    def abandon_hold(self, visitor_id: int, booking_id: int, now: int) -> None:
        """Release an unfinished form without sending a meeting cancellation notice."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT visitor_id, status FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if row is None or row[0] != visitor_id:
                raise AccessDenied("Удержание времени не найдено.")
            if row[1] == "held":
                connection.execute(
                    "UPDATE bookings SET status = 'expired', completed_at = ?, updated_at = ? "
                    "WHERE id = ?", (now, now, booking_id),
                )
                connection.execute("DELETE FROM drafts WHERE booking_id = ?", (booking_id,))
                connection.execute(
                    "UPDATE jobs SET status = 'done' WHERE booking_id = ? AND kind = 'hold_expire'",
                    (booking_id,),
                )
                _audit_booking(connection, visitor_id, booking_id, "hold_abandoned", now)
            connection.execute("COMMIT")
            if row[1] == "held":
                log_event(self.logger, "hold_abandoned", record_id=booking_id)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def saved_contacts(self, actor_id: int, visitor_id: int) -> SavedContacts | None:
        self._require_self(actor_id, visitor_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT display_name, email, phone, username FROM visitors "
                "WHERE telegram_id = ?", (visitor_id,),
            ).fetchone()
            return SavedContacts(*row) if row and row[0] and row[1] else None
        finally:
            connection.close()

    def touch_visitor(self, actor_id: int, visitor_id: int,
                      current_username: str | None, now: int) -> None:
        self._require_self(actor_id, visitor_id)
        connection = self._connect()
        try:
            changed = connection.execute(
                "UPDATE visitors SET username = ?, last_used_at = ? WHERE telegram_id = ?",
                (current_username, now, visitor_id),
            ).rowcount
            if not changed:
                raise RuleError("Посетитель не найден.")
        finally:
            connection.close()

    def correct_saved_contacts(self, actor_id: int, visitor_id: int, *, name: str, email: str,
                               phone: str | None, now: int) -> None:
        self._require_self(actor_id, visitor_id)
        if not name.strip() or not EMAIL_PATTERN.fullmatch(email.strip()):
            raise RuleError("Укажите имя и корректный email.")
        connection = self._connect()
        try:
            changed = connection.execute(
                "UPDATE visitors SET display_name = ?, email = ?, phone = ?, last_used_at = ? "
                "WHERE telegram_id = ?",
                (name.strip(), email.strip(), phone.strip() if phone else None,
                 now, visitor_id),
            ).rowcount
            if not changed:
                raise RuleError("Посетитель не найден.")
            log_event(self.logger, "saved_contacts_corrected")
        finally:
            connection.close()

    def clear_saved_contacts(self, actor_id: int, visitor_id: int, now: int) -> None:
        self._require_self(actor_id, visitor_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE visitors SET username = NULL, display_name = NULL, email = NULL, phone = NULL, "
                "last_used_at = ? WHERE telegram_id = ?", (now, visitor_id),
            )
            connection.execute(
                "UPDATE drafts SET contact_name = NULL, contact_email = NULL, "
                "contact_phone = NULL, updated_at = ? WHERE visitor_id = ?",
                (now, visitor_id),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "saved_contacts_cleared")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def submit_request(self, visitor_id: int, booking_id: int, *, name: str,
                       username: str | None, email: str, phone: str | None,
                       purpose: str, policy_version: str, now: int,
                       google_busy: list[BusyInterval] | None = None,
                       used_saved_contacts: bool = False) -> int:
        if not name.strip() or not username or not username.strip() or \
                not EMAIL_PATTERN.fullmatch(email.strip()) or not purpose.strip():
            raise RuleError("Укажите имя, Telegram username, email и цель встречи.")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_holds(connection, now)
            row = connection.execute(
                "SELECT visitor_id, template_id, availability_id, starts_at, ends_at, "
                "time_zone, status, hold_expires_at, video_service "
                "FROM bookings WHERE id = ?", (booking_id,),
            ).fetchone()
            if row is not None and row[0] == visitor_id and row[6] == "pending":
                connection.execute("COMMIT")
                return booking_id
            if row is None or row[0] != visitor_id or row[6] != "held" or row[7] <= now:
                raise RuleError("Удержание времени истекло или не найдено.")
            if connection.execute(
                "SELECT access_status FROM visitors WHERE telegram_id = ?",
                (visitor_id,),
            ).fetchone()[0] != "allowed":
                raise AccessDenied("Доступ к записи закрыт.")
            consent = connection.execute(
                "SELECT action FROM consents WHERE visitor_id = ? AND policy_version = ? "
                "ORDER BY id DESC LIMIT 1", (visitor_id, policy_version),
            ).fetchone()
            if consent is None or consent[0] != "accepted":
                raise RuleError("Требуется согласие с текущей политикой.")
            interval_row = connection.execute(
                "SELECT id, starts_at, ends_at, time_zone, is_published, is_manual_exception "
                "FROM availability_intervals WHERE id = ?", (row[2],)
            ).fetchone()
            template = connection.execute(
                "SELECT name, description, instructions, is_active "
                "FROM meeting_templates WHERE id = ?", (row[1],)
            ).fetchone()
            if interval_row is None or template is None or not template[3]:
                raise SlotUnavailable("Время или шаблон больше недоступны.")
            deadline = decision_deadline(now, self._schedule(connection))
            duration = (row[4] - row[3]) // 60
            occupied = [BusyInterval(*item) for item in connection.execute(
                "SELECT starts_at, ends_at FROM bookings WHERE id != ? AND status IN "
                "('held', 'pending', 'awaiting_link', 'creating_calendar', 'confirmed', 'attention')",
                (booking_id,),
            )]
            slots = available_slots(now, [Availability(*interval_row)], duration,
                                    row[8], self._schedule(connection),
                                    occupied + (google_busy or []))
            if row[3] not in slots or deadline > row[3] - 7200:
                raise SlotUnavailable("Время больше недоступно для новой заявки.")
            if _manual_blocked(connection, row[3], row[4]):
                raise SlotUnavailable("Время закрыто администратором.")
            connection.execute(
                "UPDATE bookings SET status = 'pending', hold_expires_at = NULL, "
                "submitted_at = ?, decision_deadline_at = ?, contact_name_snapshot = ?, "
                "username_snapshot = ?, email_snapshot = ?, phone_snapshot = ?, "
                "template_name_snapshot = ?, template_description_snapshot = ?, "
                "template_instructions_snapshot = ?, purpose = ?, used_saved_contacts = ?, "
                "updated_at = ? WHERE id = ?",
                (now, deadline, name.strip(), username.strip(), email.strip(),
                 phone.strip() if phone else None, template[0], template[1],
                 template[2], purpose.strip(), int(used_saved_contacts), now, booking_id),
            )
            connection.execute(
                "UPDATE visitors SET display_name = ?, username = ?, email = ?, phone = ?, "
                "last_used_at = ? WHERE telegram_id = ?",
                (name.strip(), username.strip(), email.strip(),
                 phone.strip() if phone else None, now, visitor_id),
            )
            connection.execute("DELETE FROM drafts WHERE booking_id = ?", (booking_id,))
            connection.execute(
                "UPDATE jobs SET status = 'done' WHERE booking_id = ? AND kind = 'hold_expire'",
                (booking_id,),
            )
            _queue_job(connection, "request_expire", deadline,
                       f"request_expire:{booking_id}", booking_id)
            _queue_notification(connection, booking_id, "telegram", "admin_new_request")
            _audit_booking(connection, visitor_id, booking_id, "request_submitted", now)
            connection.execute("COMMIT")
            log_event(self.logger, "request_submitted", record_id=booking_id)
            return booking_id
        except (RuleError, sqlite3.Error) as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.OperationalError):
                log_event(self.logger, "storage_unavailable", code=type(error).__name__)
                raise StorageUnavailable(FRIENDLY_STORAGE_ERROR) from error
            if isinstance(error, RuleError):
                log_event(self.logger, "request_rejected", code=type(error).__name__)
            raise
        finally:
            connection.close()

    def admin_decision(self, actor_id: int, booking_id: int, approve: bool,
                       now: int) -> str:
        self._require_admin(actor_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, decision_deadline_at, visitor_id FROM bookings WHERE id = ?",
                (booking_id,),
            ).fetchone()
            if row is None:
                raise RuleError("Заявка не найдена.")
            if row[0] == "awaiting_link" and approve:
                connection.execute("COMMIT")
                return "awaiting_link"
            if row[0] in TERMINAL_STATUSES:
                connection.execute("COMMIT")
                return row[0]
            if row[0] != "pending" or now >= row[1]:
                raise RuleError("Срок решения истек или статус изменился.")
            status = "awaiting_link" if approve else "rejected"
            connection.execute(
                "UPDATE bookings SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, now, None if approve else now, booking_id),
            )
            if not approve:
                _queue_notification(connection, booking_id, "telegram", "request_rejected", row[2])
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, booking_id, action, created_at) "
                "VALUES (?, ?, ?, ?)", (actor_id, booking_id, status, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "request_decided", code=status, record_id=booking_id)
            return status
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def begin_calendar_creation(self, booking_id: int, video_url: str, now: int) -> str:
        """Record a link received before deadline; stage 3 performs the Google call."""
        if not video_url.strip():
            raise ValueError("Video URL is required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, decision_deadline_at FROM bookings WHERE id = ?",
                (booking_id,),
            ).fetchone()
            if row is None:
                raise RuleError("Заявка не найдена.")
            if row[0] in ("creating_calendar", "attention"):
                stored = connection.execute(
                    "SELECT video_url FROM meetings WHERE booking_id = ?", (booking_id,)
                ).fetchone()
                cancellation = connection.execute(
                    "SELECT cancel_requested_at FROM bookings WHERE id = ?", (booking_id,)
                ).fetchone()[0]
                if stored and stored[0] == video_url and cancellation is None:
                    connection.execute("COMMIT")
                    return row[0]
                raise RuleError("Для заявки уже сохранена другая ссылка.")
            if row[0] != "awaiting_link" or now >= row[1]:
                raise RuleError("Срок добавления ссылки истек или статус изменился.")
            connection.execute(
                "INSERT INTO meetings (booking_id, video_url, sync_status) "
                "VALUES (?, ?, 'pending')", (booking_id, video_url),
            )
            connection.execute(
                "UPDATE bookings SET status = 'creating_calendar', updated_at = ? WHERE id = ?",
                (now, booking_id),
            )
            _audit_booking(connection, self.administrator_id, booking_id,
                           "calendar_creation_started", now)
            connection.execute("COMMIT")
            log_event(self.logger, "calendar_creation_started", record_id=booking_id)
            return "creating_calendar"
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def calendar_booking(self, booking_id: int) -> CalendarBooking:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT b.id, b.status, b.starts_at, b.ends_at, b.email_snapshot, "
                "m.video_url, m.google_event_id, b.cancel_requested_at "
                "FROM bookings b LEFT JOIN meetings m ON m.booking_id = b.id WHERE b.id = ?",
                (booking_id,),
            ).fetchone()
            if row is None or row[4] is None or row[5] is None:
                raise RuleError("Заявка не готова для Google Календаря.")
            return CalendarBooking(*row)
        finally:
            connection.close()

    def booking_window(self, booking_id: int) -> tuple[int, int]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT starts_at, ends_at FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if row is None:
                raise RuleError("Заявка не найдена.")
            return row[0], row[1]
        finally:
            connection.close()

    def booking_status(self, booking_id: int) -> str:
        connection = self._connect()
        try:
            row = connection.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if row is None:
                raise RuleError("Заявка не найдена.")
            return row[0]
        finally:
            connection.close()

    def reserve_google_event_id(self, booking_id: int, now: int) -> str:
        """Persist an event ID before the first external insert; reuse on every retry."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT b.status, b.cancel_requested_at, m.google_event_id "
                "FROM bookings b JOIN meetings m ON m.booking_id = b.id WHERE b.id = ?",
                (booking_id,),
            ).fetchone()
            if row is None or row[0] not in ("creating_calendar", "attention") or row[1] is not None:
                raise RuleError("Заявка не готова для Google Календаря.")
            event_id = row[2] or "m" + uuid.uuid4().hex
            if row[2] is None:
                connection.execute(
                    "UPDATE meetings SET google_event_id = ? WHERE booking_id = ?",
                    (event_id, booking_id),
                )
                _audit_booking(connection, None, booking_id, "calendar_id_reserved", now)
            connection.execute("COMMIT")
            log_event(self.logger, "calendar_id_reserved", record_id=booking_id)
            return event_id
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_calendar_attention(self, booking_id: int, code: str, now: int) -> None:
        """Keep the hold, suppress confirmation, and alert the administrator."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if row is None or row[0] not in ("creating_calendar", "attention"):
                raise RuleError("Заявка не ожидает Google Календарь.")
            connection.execute(
                "UPDATE bookings SET status = 'attention', updated_at = ? WHERE id = ?",
                (now, booking_id),
            )
            connection.execute(
                "UPDATE meetings SET sync_status = 'attention' WHERE booking_id = ?",
                (booking_id,),
            )
            _queue_notification(connection, booking_id, "telegram", "calendar_attention")
            _audit_booking(connection, None, booking_id, "calendar_attention", now)
            connection.execute("COMMIT")
            log_event(self.logger, "calendar_attention", code=code, record_id=booking_id)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_calendar_created(self, booking_id: int, google_event_id: str,
                              video_url: str, now: int) -> str:
        """Integration boundary for stage 3; caller must verify Google success first."""
        if not google_event_id or not video_url:
            raise ValueError("Calendar event and video URL are required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, visitor_id, starts_at, cancel_requested_at FROM bookings WHERE id = ?",
                (booking_id,),
            ).fetchone()
            if row is None:
                raise RuleError("Заявка не найдена.")
            if row[0] == "confirmed":
                stored = connection.execute(
                    "SELECT google_event_id, video_url FROM meetings WHERE booking_id = ?",
                    (booking_id,)
                ).fetchone()
                if stored and stored == (google_event_id, video_url):
                    connection.execute("COMMIT")
                    return "confirmed"
                raise RuleError("У заявки уже другое событие календаря.")
            if row[0] not in ("creating_calendar", "attention") or row[3] is not None:
                raise RuleError("Заявка не готова к подтверждению.")
            stored = connection.execute(
                "SELECT google_event_id, video_url FROM meetings WHERE booking_id = ?",
                (booking_id,),
            ).fetchone()
            if stored is None or stored[1] != video_url or \
                    (stored[0] is not None and stored[0] != google_event_id):
                raise RuleError("Данные создаваемого события не совпадают.")
            connection.execute(
                "UPDATE meetings SET google_event_id = ?, sync_status = 'synced', "
                "last_synced_at = ? WHERE booking_id = ?",
                (google_event_id, now, booking_id),
            )
            connection.execute(
                "UPDATE bookings SET status = 'confirmed', updated_at = ? WHERE id = ?",
                (now, booking_id),
            )
            connection.execute(
                "UPDATE jobs SET status = 'done' WHERE booking_id = ? AND kind = 'request_expire'",
                (booking_id,),
            )
            for seconds, kind in ((86400, "reminder_24h"), (7200, "reminder_2h")):
                reminder_at = row[2] - seconds
                if reminder_at > now:
                    _queue_job(connection, kind, reminder_at, f"{kind}:{booking_id}", booking_id)
            _queue_notification(connection, booking_id, "telegram", "meeting_confirmed", row[1])
            _audit_booking(connection, self.administrator_id, booking_id,
                           "meeting_confirmed", now)
            connection.execute("COMMIT")
            log_event(self.logger, "meeting_confirmed", record_id=booking_id)
            return "confirmed"
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def cancel_by_visitor(self, visitor_id: int, booking_id: int, now: int) -> str:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT visitor_id, status, starts_at, cancel_requested_at "
                "FROM bookings WHERE id = ?", (booking_id,),
            ).fetchone()
            if row is None or row[0] != visitor_id:
                raise AccessDenied("Заявка не найдена.")
            if row[1] in TERMINAL_STATUSES:
                connection.execute("COMMIT")
                return row[1]
            if row[1] == "attention" and row[3] is not None:
                connection.execute("COMMIT")
                return "attention"
            if row[1] in ("held", "pending", "awaiting_link"):
                connection.execute(
                    "UPDATE bookings SET status = 'cancelled_by_visitor', completed_at = ?, "
                    "updated_at = ? WHERE id = ?", (now, now, booking_id),
                )
                connection.execute("DELETE FROM drafts WHERE booking_id = ?", (booking_id,))
                connection.execute(
                    "UPDATE jobs SET status = 'done' WHERE booking_id = ? AND status != 'done'",
                    (booking_id,),
                )
                _queue_notification(connection, booking_id, "telegram", "visitor_cancelled")
                status = "cancelled_by_visitor"
            elif row[1] == "confirmed":
                if not can_cancel_confirmed(now, row[2]):
                    raise RuleError("Отмена доступна не позднее чем за час до встречи.")
                connection.execute(
                    "UPDATE bookings SET status = 'attention', cancel_requested_at = ?, "
                    "cancel_requested_by = ?, updated_at = ? WHERE id = ?",
                    (now, visitor_id, now, booking_id),
                )
                _queue_job(connection, "calendar_cancel", now,
                           f"calendar_cancel:{booking_id}", booking_id)
                status = "attention"
            else:
                raise RuleError("Заявку нельзя отменить в текущем состоянии.")
            _audit_booking(connection, visitor_id, booking_id,
                           "visitor_cancel_requested", now)
            connection.execute("COMMIT")
            log_event(self.logger, "cancellation_requested", code=status, record_id=booking_id)
            return status
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def mark_calendar_cancelled(self, booking_id: int, now: int) -> str:
        """Stage 3 calls this only after Google confirms deletion or absence."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, cancel_requested_at, visitor_id, cancel_requested_by "
                "FROM bookings WHERE id = ?",
                (booking_id,),
            ).fetchone()
            if row and row[0] in ("cancelled_by_visitor", "cancelled_by_admin"):
                stored = connection.execute(
                    "SELECT sync_status FROM meetings WHERE booking_id = ?", (booking_id,)
                ).fetchone()
                if stored and stored[0] == "cancelled":
                    connection.execute("COMMIT")
                    return row[0]
            if row is None or row[0] != "attention" or row[1] is None:
                raise RuleError("Нет ожидающей отмены в календаре.")
            final_status = ("cancelled_by_admin" if row[3] == self.administrator_id
                            else "cancelled_by_visitor")
            connection.execute(
                "UPDATE meetings SET sync_status = 'cancelled', last_synced_at = ? "
                "WHERE booking_id = ?", (now, booking_id),
            )
            connection.execute(
                "UPDATE bookings SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (final_status, now, now, booking_id),
            )
            connection.execute(
                "UPDATE jobs SET status = 'done' WHERE booking_id = ? AND kind = 'calendar_cancel'",
                (booking_id,),
            )
            _queue_notification(connection, booking_id, "telegram", "meeting_cancelled", row[2])
            if final_status == "cancelled_by_visitor":
                _queue_notification(connection, booking_id, "telegram", "visitor_cancelled")
            _audit_booking(connection, row[3], booking_id, "meeting_cancelled", now)
            connection.execute("COMMIT")
            log_event(self.logger, "calendar_cancellation_recorded", record_id=booking_id)
            return final_status
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def cancel_by_admin(self, actor_id: int, booking_id: int, now: int) -> str:
        self._require_admin(actor_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, visitor_id, cancel_requested_at FROM bookings WHERE id = ?",
                (booking_id,),
            ).fetchone()
            if row is None:
                raise RuleError("Заявка не найдена.")
            if row[0] in TERMINAL_STATUSES:
                connection.execute("COMMIT")
                return row[0]
            if row[0] == "attention" and row[2] is not None:
                connection.execute("COMMIT")
                return "attention"
            if row[0] in ("held", "pending", "awaiting_link"):
                connection.execute(
                    "UPDATE bookings SET status = 'cancelled_by_admin', completed_at = ?, "
                    "updated_at = ? WHERE id = ?", (now, now, booking_id),
                )
                connection.execute("DELETE FROM drafts WHERE booking_id = ?", (booking_id,))
                connection.execute(
                    "UPDATE jobs SET status = 'done' WHERE booking_id = ? AND status != 'done'",
                    (booking_id,),
                )
                _queue_notification(connection, booking_id, "telegram", "admin_cancelled", row[1])
                status = "cancelled_by_admin"
            elif row[0] == "confirmed":
                connection.execute(
                    "UPDATE bookings SET status = 'attention', cancel_requested_at = ?, "
                    "cancel_requested_by = ?, updated_at = ? WHERE id = ?",
                    (now, actor_id, now, booking_id),
                )
                _queue_job(connection, "calendar_cancel", now,
                           f"calendar_cancel:{booking_id}", booking_id)
                status = "attention"
            else:
                raise RuleError("Заявку нельзя отменить в текущем состоянии.")
            connection.execute(
                "INSERT INTO audit_actions (actor_telegram_id, booking_id, action, created_at) "
                "VALUES (?, ?, 'admin_cancel_requested', ?)",
                (actor_id, booking_id, now),
            )
            connection.execute("COMMIT")
            log_event(self.logger, "admin_cancellation_requested", code=status,
                      record_id=booking_id)
            return status
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
