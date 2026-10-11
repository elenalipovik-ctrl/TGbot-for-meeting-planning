"""Persistent, transport-neutral visitor conversation for stage 4."""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .calendar_flow import submit_with_google
from .queries import Queries
from .drafts import DraftStore
from .errors import StateConflict
from .google_calendar import CalendarError, GoogleCalendar
from .rules import GAP_SECONDS, allowed_durations, decision_deadline
from .service import (AccessDenied, BookingService, RuleError,
                      SlotUnavailable, StorageUnavailable)
from .technical_log import log_event
from .validation import (MAX_VISITOR_TEXT, PHONE_PATTERN, valid_email,
                         valid_phone, valid_visitor_text)


_draft_revision_context: ContextVar[int | None] = ContextVar("draft_revision", default=None)
SESSION_SECONDS = 30 * 60
MAX_TEXT = MAX_VISITOR_TEXT
STATUS_LABELS = {
    "held": "Заполнение заявки", "pending": "На рассмотрении",
    "awaiting_link": "Ожидает ссылки", "creating_calendar": "Подтверждается",
    "confirmed": "Подтверждена", "rejected": "Отклонена", "expired": "Срок истёк",
    "cancelled_by_visitor": "Отменена", "cancelled_by_admin": "Отменена",
    "attention": "Требует проверки администратора",
}


@dataclass(frozen=True)
class Button:
    label: str
    action: str


@dataclass(frozen=True)
class Reply:
    text: str
    rows: tuple[tuple[Button, ...], ...] = ()
    draft_revision: int | None = None


def _row(*items: tuple[str, str]) -> tuple[Button, ...]:
    return tuple(Button(label, action) for label, action in items)


class VisitorFlow:
    def __init__(self, service: BookingService, calendar: GoogleCalendar,
                 *, policy_url: str, policy_version: str):
        parsed = urlparse(policy_url)
        if parsed.scheme != "https" or not parsed.netloc or not policy_version.strip():
            raise ValueError("A public HTTPS policy URL and version are required")
        self.service = service
        self.calendar = calendar
        self.policy_url = policy_url
        self.policy_version = policy_version

    def _one(self, sql: str, params: tuple = ()):
        connection = self.service._connect()
        try:
            return connection.execute(sql, params).fetchone()
        finally:
            connection.close()

    def _many(self, sql: str, params: tuple = ()):
        connection = self.service._connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def _dialog(self, visitor_id: int, now: int):
        snapshot = DraftStore(self.service).read(visitor_id, visitor_id, now)
        expected = _draft_revision_context.get()
        if expected is not None and snapshot.revision != expected:
            raise StateConflict("Запись уже изменена. Обновите данные и повторите действие.")
        return snapshot.values

    def _save(self, visitor_id: int, step: str, now: int, **fields) -> None:
        snapshot = DraftStore(self.service).save(
            visitor_id, visitor_id, step, now,
            expected_revision=_draft_revision_context.get(), **fields)
        _draft_revision_context.set(snapshot.revision)
        log_event(self.service.logger, "visitor_dialog_step", code=step)

    def _clear(self, visitor_id: int, now: int) -> None:
        snapshot = DraftStore(self.service).reset(
            visitor_id, visitor_id, now, expected_revision=_draft_revision_context.get())
        _draft_revision_context.set(snapshot.revision)

    def _status(self, visitor_id: int):
        row = self._one("SELECT access_status FROM visitors WHERE telegram_id = ?",
                        (visitor_id,))
        return row[0] if row else None

    def _local(self, epoch: int) -> str:
        zone = ZoneInfo(self.service.review_schedule().time_zone)
        return datetime.fromtimestamp(epoch, zone).strftime("%d.%m.%Y %H:%M")

    def _menu(self, visitor_id: int, username: str | None, now: int) -> Reply:
        status = self._status(visitor_id)
        if status is None or status == "rejected":
            return Reply(
                "Здравствуйте! Для записи на онлайн-встречу запросите доступ. "
                f"Политика обработки данных: {self.policy_url}",
                (_row(("Запросить доступ", "access")),))
        if status == "pending":
            return Reply("Запрос доступа отправлен. Ожидайте решения администратора.",
                         (_row(("Обновить статус", "menu")),))
        if status == "blocked":
            return Reply("Доступ к записи заблокирован.")
        self.service.touch_visitor(visitor_id, visitor_id, username, now)
        return Reply("Выберите действие:",
                     (_row(("Записаться", "book")),
                      _row(("Мои заявки", "mine"), ("Мои данные", "profile"))))

    def _require_allowed(self, visitor_id: int) -> None:
        if self._status(visitor_id) != "allowed":
            raise AccessDenied("Сначала требуется разрешение на запись.")

    def _slots(self, visitor_id: int, duration: int, video: str, now: int) -> list[int]:
        busy = self.calendar.freebusy(now, now + 7 * 86400 + 90 * 60 + GAP_SECONDS)
        return self.service.list_slots(visitor_id, now, duration, video, busy)

    def _templates(self, visitor_id: int, now: int) -> Reply:
        if not self._many("SELECT id FROM meeting_templates WHERE is_active = 1"):
            return Reply("Пока нет доступных видов встреч.", (_row(("В меню", "menu")),))
        self._save(visitor_id, "template", now, template_id=None, duration_minutes=None,
                   video_service=None, selected_day=None, booking_id=None,
                   contact_name=None, contact_email=None, contact_phone=None, purpose=None)
        rows = tuple(_row((name, f"t:{ident}")) for ident, name in self._many(
            "SELECT id, name FROM meeting_templates WHERE is_active = 1 ORDER BY id"))
        return Reply("Выберите вид встречи:", rows + (_row(("Отмена", "stop")),))

    def _durations(self, visitor_id: int, template_id: int, now: int) -> Reply:
        self._save(visitor_id, "duration", now, template_id=template_id)
        return Reply("Выберите длительность:",
                     tuple(_row((f"{minutes} минут", f"d:{minutes}")) for minutes in
                           (15, 30, 45, 60, 90)) + (_row(("Назад", "back")),))

    def _videos(self, visitor_id: int, duration: int, now: int) -> Reply:
        self._save(visitor_id, "video", now, duration_minutes=duration)
        options = [("Яндекс Телемост", "v:telemost")]
        if duration in allowed_durations("zoom"):
            options.append(("Zoom Basic", "v:zoom"))
        return Reply("Выберите сервис видеосвязи:",
                     tuple(_row(option) for option in options) + (_row(("Назад", "back")),))

    def _days(self, visitor_id: int, duration: int, video: str, now: int) -> Reply:
        slots = self._slots(visitor_id, duration, video, now)
        self._save(visitor_id, "day", now, video_service=video, selected_day=None)
        zone = ZoneInfo(self.service.review_schedule().time_zone)
        days = sorted({datetime.fromtimestamp(slot, zone).strftime("%Y-%m-%d") for slot in slots})
        if not days:
            return Reply("Свободных интервалов на ближайшие 7 дней нет.",
                         (_row(("Назад", "back"), ("В меню", "stop")),))
        return Reply("Выберите день (время московское):",
                     tuple(_row((datetime.fromisoformat(day).strftime("%d.%m.%Y"), f"day:{day}"))
                           for day in days) + (_row(("Назад", "back")),))

    def _times(self, visitor_id: int, dialog, day: str, now: int) -> Reply:
        slots = self._slots(visitor_id, dialog[2], dialog[3], now)
        zone = ZoneInfo(self.service.review_schedule().time_zone)
        selected = [slot for slot in slots if
                    datetime.fromtimestamp(slot, zone).strftime("%Y-%m-%d") == day]
        if not selected:
            refreshed = self._days(visitor_id, dialog[2], dialog[3], now)
            return Reply("Время этого дня уже занято.\n" + refreshed.text, refreshed.rows)
        self._save(visitor_id, "time", now, selected_day=day)
        return Reply("Выберите время (МСК):",
                     tuple(_row((datetime.fromtimestamp(slot, zone).strftime("%H:%M"),
                                 f"time:{slot}")) for slot in selected) +
                     (_row(("Назад", "back")),))

    def _contacts(self, visitor_id: int, booking_id: int, now: int) -> Reply:
        saved = self.service.saved_contacts(visitor_id, visitor_id)
        if saved:
            self._save(visitor_id, "reuse", now, booking_id=booking_id,
                       used_saved_contacts=0)
            return Reply(f"Использовать сохранённые данные: {saved.name}, {saved.email}"
                         + (f", {saved.phone}" if saved.phone else "") + "?",
                         (_row(("Да, использовать", "reuse")),
                          _row(("Изменить данные", "edit"), ("Отмена", "stop"))))
        self._save(visitor_id, "name", now, booking_id=booking_id,
                   used_saved_contacts=0)
        return Reply("Напишите, как к вам обращаться.", (_row(("Отмена", "stop")),))

    def _review(self, visitor_id: int, dialog, now: int) -> Reply:
        booking = self._one(
            "SELECT starts_at, ends_at, template_id, video_service, hold_expires_at "
            "FROM bookings WHERE id = ? AND visitor_id = ? AND status = 'held'",
            (dialog[5], visitor_id))
        if not booking or booking[4] <= now:
            self._clear(visitor_id, now)
            return Reply("Время удержания истекло. Начните запись заново.",
                         (_row(("Записаться", "book")),))
        template = self._one("SELECT name FROM meeting_templates WHERE id = ?", (booking[2],))
        deadline = decision_deadline(now, self.service.review_schedule())
        return Reply(
            f"Проверьте заявку:\n{template[0] if template else 'Онлайн-встреча'}\n"
            f"{self._local(booking[0])}–{self._local(booking[1])[-5:]} МСК\n"
            f"{booking[3]}, {dialog[6]}, {dialog[7]}\n"
            f"Цель: {dialog[9]}\n"
            f"Решение ожидается до {self._local(deadline)} МСК. "
            f"Время удерживается до {self._local(booking[4])} МСК.",
            (_row(("Отправить заявку", "send")),
             _row(("Отмена", "stop")),))

    def _consent(self) -> Reply:
        return Reply(
            "Отдельное согласие на обработку персональных данных для записи на встречу. "
            "Данные: Telegram ID и username, имя, email, необязательный телефон, "
            "цель, время и статус встречи. Действия: получение, хранение, использование, "
            "передача email в Google Календарь для приглашения, удаление по сроку. "
            "Сведения в собственной базе бота хранятся по срокам политики. "
            f"Оператор и порядок отзыва указаны в политике: {self.policy_url}\n"
            "Дать согласие можно отдельной кнопкой ниже.",
            (_row(("Даю согласие на обработку", "consent:yes")),
             _row(("Не согласен", "stop")),))

    def _mine(self, visitor_id: int) -> Reply:
        bookings = [row[:3] for row in Queries(self.service).own_bookings(visitor_id, limit=10)]
        if not bookings:
            return Reply("У вас пока нет заявок.", (_row(("В меню", "menu")),))
        lines = [f"№{ident} — {self._local(start)} МСК — {STATUS_LABELS.get(status, status)}"
                 for ident, start, status in bookings]
        buttons = tuple(_row((f"Отменить №{ident}", f"cancel:{ident}"))
                        for ident, _, status in bookings if status in
                        ("pending", "awaiting_link", "confirmed"))
        return Reply("Мои заявки:\n" + "\n".join(lines),
                     buttons + (_row(("В меню", "menu")),))

    def _profile(self, visitor_id: int) -> Reply:
        saved = self.service.saved_contacts(visitor_id, visitor_id)
        if not saved:
            return Reply("Сохранённых контактных данных нет.",
                         (_row(("Отозвать согласие", "profile:withdraw")),
                          _row(("В меню", "menu"))))
        return Reply(f"Ваши данные: {saved.name}, {saved.email}"
                     + (f", {saved.phone}" if saved.phone else ""),
                     (_row(("Исправить", "profile:edit"), ("Удалить", "profile:delete")),
                      _row(("Отозвать согласие", "profile:withdraw")),
                      _row(("В меню", "menu"))))

    def handle(self, visitor_id: int, username: str | None, action: str,
               now: int, *, text: str | None = None) -> Reply:
        """Handle one private-chat action. Never log visitor-entered text."""
        token = _draft_revision_context.set(None)
        try:
            expected = None
            if action.startswith("r:"):
                try:
                    _, raw_revision, action = action.split(":", 2)
                    expected = int(raw_revision)
                    if expected < 0:
                        raise ValueError
                except ValueError:
                    return self._stale()
            if self._status(visitor_id) == "allowed":
                snapshot = DraftStore(self.service).read(visitor_id, visitor_id, now)
                if expected is not None and expected != snapshot.revision:
                    raise StateConflict("Запись уже изменена. Обновите данные и повторите действие.")
                _draft_revision_context.set(snapshot.revision)
                if snapshot.expired and action == "send":
                    return Reply("Удержание времени истекло. Выберите время заново.",
                                 (_row(("Записаться", "book")),))
            reply = self._handle(visitor_id, username, action, now, text=text)
            return Reply(reply.text, reply.rows, _draft_revision_context.get())
        except (AccessDenied, RuleError, SlotUnavailable) as error:
            return Reply(str(error), (_row(("В меню", "menu")),))
        except (CalendarError, StorageUnavailable):
            log_event(self.service.logger, "visitor_action_failed", code="dependency")
            return Reply("Сервис временно недоступен. Попробуйте позже.",
                         (_row(("В меню", "menu")),))
        finally:
            _draft_revision_context.reset(token)

    def _resume(self, visitor_id: int, now: int) -> Reply:
        dialog = self._dialog(visitor_id, now)
        if dialog is None:
            return self._templates(visitor_id, now)
        step = dialog[0]
        if step == "template":
            return self._templates(visitor_id, now)
        if step == "duration":
            return self._durations(visitor_id, dialog[1], now)
        if step == "video":
            return self._videos(visitor_id, dialog[2], now)
        if step == "day":
            return self._days(visitor_id, dialog[2], dialog[3], now)
        if step == "time":
            return self._times(visitor_id, dialog, dialog[4], now)
        if step in ("contacts", "reuse"):
            return self._contacts(visitor_id, dialog[5], now)
        if step == "review":
            return self._review(visitor_id, dialog, now)
        if step == "consent":
            return self._consent()
        prompts = {"name": "Напишите, как к вам обращаться.",
                   "email": "Напишите email для приглашения.",
                   "phone": "Напишите телефон или нажмите «Пропустить».",
                   "purpose": "Напишите цель встречи.",
                   "profile_name": "Напишите новое имя.",
                   "profile_email": "Напишите email для приглашения.",
                   "profile_phone": "Напишите телефон или нажмите «Пропустить»."}
        if step in ("phone", "profile_phone"):
            return Reply(prompts[step], (_row(("Пропустить", "phone:skip"), ("Отмена", "stop")),))
        return Reply(prompts.get(step, "Продолжите заполнение заявки."),
                     (_row(("Отмена", "stop")),))

    def _handle(self, visitor_id: int, username: str | None, action: str,
                now: int, *, text: str | None) -> Reply:
        if action == "start":
            return self._menu(visitor_id, username, now)
        if action == "access":
            return Reply(
                "Для запроса доступа бот сохранит ваш Telegram ID и username, "
                "а затем данные заявки, если вы решите записаться. "
                f"Политика обработки данных: {self.policy_url}\n"
                "Согласие на обработку данных даётся отдельной кнопкой.",
                (_row(("Даю согласие", "access:yes")),
                 _row(("Не согласен", "start")),))
        if action == "access:yes":
            self.service.request_access(visitor_id, username, now)
            self.service.record_consent(visitor_id, self.policy_version, now)
            return self._menu(visitor_id, username, now)
        if action == "menu":
            return self._menu(visitor_id, username, now)
        self._require_allowed(visitor_id)
        self.service.touch_visitor(visitor_id, visitor_id, username, now)
        if action == "stop":
            self._clear(visitor_id, now)
            return Reply("Заполнение заявки отменено.", (_row(("В меню", "menu")),))
        if action == "mine":
            return self._mine(visitor_id)
        if action == "profile":
            return self._profile(visitor_id)
        if action == "profile:delete":
            return Reply("Удалить сохранённые контактные данные? История заявок останется.",
                         (_row(("Да, удалить", "profile:delete:yes"), ("Назад", "profile")),))
        if action == "profile:delete:yes":
            self.service.clear_saved_contacts(visitor_id, visitor_id, now,
                                              expected_draft_revision=_draft_revision_context.get())
            _draft_revision_context.set(_draft_revision_context.get() + 1)
            return self._profile(visitor_id)
        if action == "profile:withdraw":
            return Reply("Отозвать согласие на обработку данных для новых заявок? "
                         "Это не отменит уже созданные встречи. Для запроса на удаление "
                         "действующей заявки обратитесь к оператору по адресу в политике.",
                         (_row(("Да, отозвать", "profile:withdraw:yes"),
                               ("Назад", "profile")),))
        if action == "profile:withdraw:yes":
            self.service.withdraw_consent(visitor_id, visitor_id, self.policy_version, now,
                                           expected_draft_revision=_draft_revision_context.get())
            _draft_revision_context.set(_draft_revision_context.get() + 1)
            return Reply("Согласие отозвано. Для новой заявки потребуется новое согласие.",
                         (_row(("В меню", "menu")),))
        if action == "profile:edit":
            self._save(visitor_id, "profile_name", now)
            return Reply("Напишите новое имя.", (_row(("Отмена", "stop")),))
        if action.startswith("cancel:"):
            try:
                booking_id = int(action.split(":", 1)[1])
            except ValueError:
                return self._stale()
            row = self._one("SELECT status, starts_at FROM bookings WHERE id = ? AND visitor_id = ?",
                            (booking_id, visitor_id))
            if not row or row[0] not in ("pending", "awaiting_link", "confirmed"):
                return self._stale()
            return Reply(f"Отменить заявку №{booking_id} на {self._local(row[1])} МСК?",
                         (_row(("Да, отменить", f"cancel_yes:{booking_id}"),
                               ("Нет", "mine")),))
        if action.startswith("cancel_yes:"):
            try:
                booking_id = int(action.split(":", 1)[1])
            except ValueError:
                return self._stale()
            status = self.service.cancel_by_visitor(visitor_id, booking_id, now)
            if status == "attention":
                return Reply("Запрос на отмену принят. Подтверждение придёт после проверки календаря.",
                             (_row(("Мои заявки", "mine")),))
            return Reply("Заявка отменена.", (_row(("Мои заявки", "mine")),))
        if action == "draft:continue":
            return self._resume(visitor_id, now)
        if action == "draft:reset":
            return Reply("Сбросить незавершённую запись и освободить удерживаемое время?",
                         (_row(("Да, сбросить", "draft:reset:yes"),
                               ("Продолжить запись", "draft:continue")),))
        if action == "draft:reset:yes":
            self._clear(visitor_id, now)
            return self._templates(visitor_id, now)
        if action == "book":
            if not username:
                return Reply("Для записи установите username в настройках Telegram, затем вернитесь.",
                             (_row(("Проверить снова", "book")),))
            if self._dialog(visitor_id, now) is not None:
                return Reply("У вас есть незавершённая запись. Продолжить её?",
                             (_row(("Продолжить", "draft:continue"),
                                   ("Сбросить и начать заново", "draft:reset")),))
            return self._templates(visitor_id, now)
        dialog = self._dialog(visitor_id, now)
        if dialog is None:
            return self._stale()
        step = dialog[0]
        if action == "back":
            if step == "duration":
                return self._templates(visitor_id, now)
            if step == "video":
                return self._durations(visitor_id, dialog[1], now)
            if step == "day":
                return self._videos(visitor_id, dialog[2], now)
            if step == "time":
                return self._days(visitor_id, dialog[2], dialog[3], now)
            return self._stale()
        if step == "template" and action.startswith("t:"):
            try:
                template_id = int(action[2:])
            except ValueError:
                return self._stale()
            if not self._one("SELECT 1 FROM meeting_templates WHERE id = ? AND is_active = 1",
                             (template_id,)):
                return self._stale()
            return self._durations(visitor_id, template_id, now)
        if step == "duration" and action.startswith("d:"):
            try:
                duration = int(action[2:])
            except ValueError:
                return self._stale()
            if duration not in allowed_durations("telemost"):
                return self._stale()
            return self._videos(visitor_id, duration, now)
        if step == "video" and action.startswith("v:"):
            video = action[2:]
            if video not in ("zoom", "telemost") or dialog[2] not in allowed_durations(video):
                return self._stale()
            return self._days(visitor_id, dialog[2], video, now)
        if step == "day" and action.startswith("day:"):
            day = action[4:]
            try:
                datetime.fromisoformat(day)
            except ValueError:
                return self._stale()
            return self._times(visitor_id, dialog, day, now)
        if step == "time" and action.startswith("time:"):
            try:
                slot = int(action[5:])
            except ValueError:
                return self._stale()
            zone = ZoneInfo(self.service.review_schedule().time_zone)
            if datetime.fromtimestamp(slot, zone).strftime("%Y-%m-%d") != dialog[4]:
                return self._stale()
            if slot not in self._slots(visitor_id, dialog[2], dialog[3], now):
                return Reply("Это время уже занято. Выберите другое.",
                             (_row(("Дни", "back")),))
            row = self._one("SELECT id FROM availability_intervals WHERE is_published = 1 "
                            "AND starts_at <= ? AND ends_at >= ? ORDER BY id LIMIT 1",
                            (slot, slot + dialog[2] * 60))
            if not row:
                return self._stale()
            busy = self.calendar.freebusy(slot - GAP_SECONDS,
                                          slot + dialog[2] * 60 + GAP_SECONDS)
            booking_id = self.service.create_hold(
                visitor_id, dialog[1], row[0], slot, dialog[2], dialog[3], now, busy,
                expected_draft_revision=_draft_revision_context.get())
            if _draft_revision_context.get() is not None:
                _draft_revision_context.set(_draft_revision_context.get() + 1)
            return self._contacts(visitor_id, booking_id, now)
        if step == "reuse" and action in ("reuse", "edit"):
            if action == "edit":
                self._save(visitor_id, "name", now, used_saved_contacts=0)
                return Reply("Напишите, как к вам обращаться.", (_row(("Отмена", "stop")),))
            saved = self.service.saved_contacts(visitor_id, visitor_id)
            if not saved:
                return self._stale()
            self._save(visitor_id, "purpose", now, contact_name=saved.name,
                       contact_email=saved.email, contact_phone=saved.phone,
                       used_saved_contacts=1)
            return Reply("Напишите цель встречи.", (_row(("Отмена", "stop")),))
        if action == "phone:skip" and step in ("phone", "profile_phone"):
            next_step = "purpose" if step == "phone" else "profile_done"
            self._save(visitor_id, next_step, now, contact_phone=None)
            if step == "phone":
                return Reply("Напишите цель встречи.", (_row(("Отмена", "stop")),))
            return self._save_profile(visitor_id, now)
        if step == "consent" and action == "consent:yes":
            self.service.record_consent(visitor_id, self.policy_version, now,
                                        expected_draft_revision=_draft_revision_context.get())
            self._save(visitor_id, "review", now)
            return self._review(visitor_id, self._dialog(visitor_id, now), now)
        if step == "review" and action == "send":
            hold = self._one("SELECT status, hold_expires_at FROM bookings "
                             "WHERE id = ? AND visitor_id = ?", (dialog[5], visitor_id))
            if not hold or hold[0] != "held" or hold[1] <= now:
                self._clear(visitor_id, now)
                return Reply("Время удержания истекло. Начните запись заново.",
                             (_row(("Записаться", "book")),))
            if not username:
                return Reply("Установите username в Telegram перед отправкой заявки.",
                             (_row(("Отмена", "stop")),))
            booking_id = submit_with_google(
                self.service, self.calendar, visitor_id, dialog[5], name=dialog[6],
                username=username, email=dialog[7], phone=dialog[8], purpose=dialog[9],
                policy_version=self.policy_version, now=now,
                used_saved_contacts=bool(dialog[11]),
                expected_draft_revision=_draft_revision_context.get())
            if _draft_revision_context.get() is not None:
                _draft_revision_context.set(_draft_revision_context.get() + 1)
            self._clear(visitor_id, now)
            log_event(self.service.logger, "visitor_request_sent", record_id=booking_id)
            return Reply(f"Заявка №{booking_id} отправлена администратору. "
                         "Статус можно посмотреть в «Мои заявки».",
                         (_row(("Мои заявки", "mine")),))
        if action == "text" and text is not None:
            value = text.strip()
            if not valid_visitor_text(value):
                log_event(self.service.logger, "visitor_input_rejected", code="length")
                return Reply("Напишите текст длиной от 1 до 500 символов.")
            if step in ("name", "profile_name"):
                self._save(visitor_id, "email" if step == "name" else "profile_email", now,
                           contact_name=value)
                return Reply("Напишите email для приглашения.", (_row(("Отмена", "stop")),))
            if step in ("email", "profile_email"):
                if not valid_email(value):
                    log_event(self.service.logger, "visitor_input_rejected", code="email")
                    return Reply("Укажите корректный email.")
                self._save(visitor_id, "phone" if step == "email" else "profile_phone", now,
                           contact_email=value)
                return Reply("Напишите телефон или нажмите «Пропустить».",
                             (_row(("Пропустить", "phone:skip"), ("Отмена", "stop")),))
            if step in ("phone", "profile_phone"):
                if not valid_phone(value):
                    log_event(self.service.logger, "visitor_input_rejected", code="phone")
                    return Reply("Укажите телефон в международном формате или пропустите.")
                self._save(visitor_id, "purpose" if step == "phone" else "profile_done", now,
                           contact_phone=value)
                if step == "phone":
                    return Reply("Напишите цель встречи.", (_row(("Отмена", "stop")),))
                return self._save_profile(visitor_id, now)
            if step == "purpose":
                self._save(visitor_id, "consent", now, purpose=value)
                return self._consent()
        return self._stale()

    def _save_profile(self, visitor_id: int, now: int) -> Reply:
        dialog = self._dialog(visitor_id, now)
        self.service.correct_saved_contacts(visitor_id, visitor_id,
                                            name=dialog[6], email=dialog[7],
                                            phone=dialog[8], now=now,
                                            expected_draft_revision=_draft_revision_context.get())
        _draft_revision_context.set(_draft_revision_context.get() + 1)
        if dialog[5]:
            self._save(visitor_id, "purpose", now)
        else:
            self._clear(visitor_id, now)
        return Reply("Данные сохранены.", (_row(("Мои данные", "profile")),))

    @staticmethod
    def _stale() -> Reply:
        return Reply("Кнопка устарела. Начните действие заново.",
                     (_row(("В меню", "menu")),))
