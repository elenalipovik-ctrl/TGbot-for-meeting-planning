"""One persisted visitor form for chat and HTTP; no booking rules are copied here."""

import sqlite3
from functools import wraps

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .errors import AccessDenied, RuleError, StateConflict, StorageUnavailable
from .technical_log import log_event
from .validation import valid_email, valid_phone, valid_visitor_text

if TYPE_CHECKING:
    from .service import BookingService


SESSION_SECONDS = 30 * 60
DIALOG_FIELDS = (
    "step", "template_id", "duration_minutes", "video_service", "selected_day",
    "booking_id", "contact_name", "contact_email", "contact_phone", "purpose",
    "expires_at", "used_saved_contacts",
)
EDITABLE_FIELDS = {
    "template_id", "duration_minutes", "video_service", "selected_day", "booking_id",
    "contact_name", "contact_email", "contact_phone", "purpose", "used_saved_contacts",
}
CONTACT_FIELDS = {"contact_name", "contact_email", "contact_phone", "purpose"}


def draft_revision(connection, visitor_id: int) -> int:
    row = connection.execute(
        "SELECT revision FROM draft_generations WHERE visitor_id = ?", (visitor_id,),
    ).fetchone()
    if row is None:
        raise AccessDenied("Посетитель не найден.")
    return row[0]


def check_draft_revision(connection, visitor_id: int, expected: int | None) -> int:
    actual = draft_revision(connection, visitor_id)
    if expected is not None and (type(expected) is not int or expected != actual):
        raise StateConflict("Запись уже изменена. Обновите данные и повторите действие.")
    return actual


def require_allowed(connection, visitor_id: int) -> None:
    row = connection.execute(
        "SELECT access_status FROM visitors WHERE telegram_id = ?", (visitor_id,),
    ).fetchone()
    if row is None or row[0] != "allowed":
        raise AccessDenied("Доступ к записи закрыт.")


@dataclass(frozen=True)
class DraftSnapshot:
    revision: int
    values: tuple | None = field(repr=False)
    hold_expires_at: int | None = None
    expired: bool = False


def _storage_errors(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except sqlite3.OperationalError as error:
            log_event(self.service.logger, "storage_unavailable", code=type(error).__name__)
            raise StorageUnavailable("Не удалось сохранить действие. Попробуйте ещё раз позднее.") from error
    return wrapped


class DraftStore:
    def __init__(self, service: "BookingService"):
        self.service = service

    def _authorize(self, connection, actor_id: int, visitor_id: int) -> None:
        self.service._require_self(actor_id, visitor_id)
        require_allowed(connection, visitor_id)

    def _snapshot(self, connection, visitor_id: int) -> DraftSnapshot:
        row = connection.execute(
            f"SELECT {', '.join(DIALOG_FIELDS)} FROM visitor_dialogs WHERE visitor_id = ?",
            (visitor_id,),
        ).fetchone()
        expiry = None
        if row and row[5]:
            booking = connection.execute(
                "SELECT hold_expires_at FROM bookings WHERE id = ?", (row[5],),
            ).fetchone()
            expiry = booking[0] if booking else None
        return DraftSnapshot(draft_revision(connection, visitor_id), row, expiry)

    @_storage_errors
    def read(self, actor_id: int, visitor_id: int, now: int) -> DraftSnapshot:
        connection = self.service._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, actor_id, visitor_id)
            expired = connection.execute(
                "SELECT 1 FROM visitor_dialogs d JOIN bookings b ON b.id=d.booking_id "
                "WHERE d.visitor_id=? AND b.status='held' AND b.hold_expires_at<=?",
                (visitor_id, now),
            ).fetchone() is not None
            self.service._expire_holds(connection, now)
            row = connection.execute(
                "SELECT booking_id, expires_at FROM visitor_dialogs WHERE visitor_id = ?",
                (visitor_id,),
            ).fetchone()
            if row and row[1] <= now:
                self._reset(connection, visitor_id, now)
            elif row and row[0]:
                status = connection.execute(
                    "SELECT status FROM bookings WHERE id = ?", (row[0],),
                ).fetchone()
                if status is None or status[0] != "held":
                    self._reset(connection, visitor_id, now)
            snapshot = self._snapshot(connection, visitor_id)
            connection.execute("COMMIT")
            return DraftSnapshot(snapshot.revision, snapshot.values,
                                 snapshot.hold_expires_at, expired)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @_storage_errors
    def save(self, actor_id: int, visitor_id: int, step: str, now: int, *,
             expected_revision: int | None = None, **fields) -> DraftSnapshot:
        """Internal dialog transition; HTTP must use patch_contacts for hold edits."""
        if set(fields) - EDITABLE_FIELDS:
            raise ValueError("Unexpected dialog field")
        connection = self.service._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, actor_id, visitor_id)
            self.service._expire_holds(connection, now)
            check_draft_revision(connection, visitor_id, expected_revision)
            if fields.get("booking_id") is not None:
                owner = connection.execute(
                    "SELECT visitor_id, status FROM bookings WHERE id = ?",
                    (fields["booking_id"],),
                ).fetchone()
                if owner != (visitor_id, "held"):
                    raise AccessDenied("Удержание времени не найдено.")
            columns = ["visitor_id", "step", "expires_at", "updated_at", *fields]
            updates = [f"{key}=excluded.{key}" for key in columns if key != "visitor_id"]
            connection.execute(
                f"INSERT INTO visitor_dialogs ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(visitor_id) DO UPDATE SET {','.join(updates)}",
                (visitor_id, step, now + SESSION_SECONDS, now, *fields.values()),
            )
            snapshot = self._snapshot(connection, visitor_id)
            connection.execute("COMMIT")
            return snapshot
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @_storage_errors
    def select(self, actor_id, visitor_id, now, *, expected_revision, template_id=None,
               duration_minutes=None, video_service=None, selected_day=None):
        """Pre-hold selection; a live hold must be explicitly released first."""
        from .rules import allowed_durations, DURATIONS
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        if type(expected_revision) is not int or expected_revision < 0:
            raise StateConflict("Укажите текущую версию черновика.")
        c=self.service._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            self._authorize(c,actor_id,visitor_id)
            self.service._expire_holds(c,now)
            check_draft_revision(c,visitor_id,expected_revision)
            current=self._snapshot(c,visitor_id)
            if current.values and current.values[5]:
                raise StateConflict("Сначала освободите удерживаемое время.")
            if template_id is not None and not c.execute("SELECT 1 FROM meeting_templates WHERE id=? AND is_active=1",(template_id,)).fetchone():
                raise RuleError("Шаблон встречи недоступен.")
            if duration_minutes is not None and duration_minutes not in DURATIONS:
                raise RuleError("Недопустимая длительность.")
            if video_service is not None and (video_service not in ("telemost","zoom") or duration_minutes not in allowed_durations(video_service)):
                raise RuleError("Эта длительность недоступна для выбранного сервиса.")
            if selected_day is not None:
                today=datetime.fromtimestamp(now,ZoneInfo(self.service._schedule(c).time_zone)).date()
                if not today<=selected_day<=today+timedelta(days=7):
                    raise RuleError("Выберите дату в ближайшие 7 дней.")
            if template_id is None:
                duration_minutes=video_service=selected_day=None
            elif duration_minutes is None:
                video_service=selected_day=None
            elif video_service is None:
                selected_day=None
            step="template" if template_id is None else "duration" if duration_minutes is None else "video" if video_service is None else "day" if selected_day is None else "time"
            c.execute("INSERT INTO visitor_dialogs(visitor_id,step,template_id,duration_minutes,video_service,selected_day,expires_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(visitor_id) DO UPDATE SET step=excluded.step,template_id=excluded.template_id,duration_minutes=excluded.duration_minutes,video_service=excluded.video_service,selected_day=excluded.selected_day,expires_at=excluded.expires_at,updated_at=excluded.updated_at",(visitor_id,step,template_id,duration_minutes,video_service,selected_day.isoformat() if selected_day else None,now+SESSION_SECONDS,now))
            snapshot=self._snapshot(c,visitor_id)
            c.execute("COMMIT")
            return snapshot
        except BaseException:
            if c.in_transaction:c.execute("ROLLBACK")
            raise
        finally:c.close()

    @_storage_errors
    def patch_contacts(self, actor_id: int, visitor_id: int, booking_id: int,
                       now: int, *, expected_revision: int, **fields) -> DraftSnapshot:
        if type(expected_revision) is not int or expected_revision < 0:
            raise StateConflict("Укажите текущую версию черновика.")
        if not fields or set(fields) - CONTACT_FIELDS:
            raise RuleError("Можно изменить только контакты и цель встречи.")
        if fields.get("contact_phone") == "":
            fields["contact_phone"] = None
        for key, value in fields.items():
            if value is None and key == "contact_phone":
                continue
            if not isinstance(value, str) or not valid_visitor_text(value):
                raise RuleError("Напишите текст длиной от 1 до 500 символов.")
            if key == "contact_email" and not valid_email(value):
                raise RuleError("Укажите корректный email.")
            if key == "contact_phone" and not valid_phone(value):
                raise RuleError("Укажите корректный телефон или пропустите.")
        connection = self.service._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, actor_id, visitor_id)
            check_draft_revision(connection, visitor_id, expected_revision)
            row = connection.execute(
                "SELECT b.visitor_id, b.status, b.hold_expires_at, d.booking_id "
                "FROM bookings b JOIN visitor_dialogs d ON d.visitor_id = b.visitor_id "
                "WHERE b.id = ?", (booking_id,),
            ).fetchone()
            if row is None or row[0] != visitor_id or row[3] != booking_id:
                raise AccessDenied("Удержание времени не найдено.")
            if row[1] != "held" or row[2] <= now:
                raise StateConflict("Время удержания истекло. Выберите время заново.")
            clean = {key: value.strip() if value is not None else None
                     for key, value in fields.items()}
            if set(clean) & {"contact_name", "contact_email", "contact_phone"}:
                clean["used_saved_contacts"] = 0
            connection.execute(
                "UPDATE visitor_dialogs SET " + ",".join(f"{key}=?" for key in clean)
                + ",updated_at=? WHERE visitor_id=?", (*clean.values(), now, visitor_id),
            )
            row = connection.execute("SELECT contact_name,contact_email,purpose FROM visitor_dialogs WHERE visitor_id=?", (visitor_id,)).fetchone()
            step = "name" if not row[0] else "email" if not row[1] else "purpose" if not row[2] else "consent"
            connection.execute("UPDATE visitor_dialogs SET step=? WHERE visitor_id=?", (step,visitor_id))
            snapshot = self._snapshot(connection, visitor_id)
            connection.execute("COMMIT")
            return snapshot
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @_storage_errors
    def use_saved_contacts(self, actor_id, visitor_id, booking_id, now, *, expected_revision):
        """Explicit reuse reads current profile, never trusts a client reuse flag."""
        connection = self.service._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, actor_id, visitor_id)
            check_draft_revision(connection, visitor_id, expected_revision)
            row = connection.execute("SELECT b.status,b.hold_expires_at FROM bookings b JOIN visitor_dialogs d ON d.booking_id=b.id WHERE b.id=? AND b.visitor_id=? AND d.visitor_id=?", (booking_id,visitor_id,visitor_id)).fetchone()
            if row is None:
                raise AccessDenied("Удержание времени не найдено.")
            if row[0] != "held" or row[1] <= now:
                raise StateConflict("Время удержания истекло. Выберите время заново.")
            profile = connection.execute("SELECT display_name,email,phone FROM visitors WHERE telegram_id=?", (visitor_id,)).fetchone()
            if not profile or not profile[0] or not profile[1]:
                raise RuleError("Сохранённых контактов нет. Заполните форму.")
            connection.execute("UPDATE visitor_dialogs SET contact_name=?,contact_email=?,contact_phone=?,used_saved_contacts=1,step='purpose',updated_at=? WHERE visitor_id=?", (*profile,now,visitor_id))
            snapshot = self._snapshot(connection, visitor_id)
            connection.execute("COMMIT")
            return snapshot
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _reset(self, connection, visitor_id: int, now: int) -> None:
        row = connection.execute(
            "SELECT booking_id FROM visitor_dialogs WHERE visitor_id = ?", (visitor_id,),
        ).fetchone()
        if row and row[0]:
            self.service._abandon_hold(connection, visitor_id, row[0], now)
        removed = connection.execute(
            "DELETE FROM visitor_dialogs WHERE visitor_id = ?", (visitor_id,),
        ).rowcount
        if not removed:
            connection.execute(
                "UPDATE draft_generations SET revision=revision+1 WHERE visitor_id=?",
                (visitor_id,),
            )

    @_storage_errors
    def reset(self, actor_id: int, visitor_id: int, now: int, *,
              expected_revision: int | None = None) -> DraftSnapshot:
        connection = self.service._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._authorize(connection, actor_id, visitor_id)
            check_draft_revision(connection, visitor_id, expected_revision)
            self._reset(connection, visitor_id, now)
            snapshot = self._snapshot(connection, visitor_id)
            connection.execute("COMMIT")
            return snapshot
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
