"""Persistent private-chat controls for the single Telegram administrator."""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .queries import Queries
from .commands import Commands
from .calendar_flow import confirm_with_google
from .google_calendar import CalendarAuthExpired, CalendarError, GoogleCalendar
from .rules import ReviewSchedule, ReviewWindow
from .service import AccessDenied, BookingService, RuleError, SlotUnavailable, StorageUnavailable
from .technical_log import log_event
from .visitor_flow import Reply, _row
from .validation import MAX_ADMIN_TEXT, SERVICE_HOSTS, valid_video_url


ADMIN_SESSION_SECONDS = 30 * 60
INTERVAL_PATTERN = re.compile(r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})-(\d{2}:\d{2})$")
REVIEW_PATTERN = re.compile(r"^([1-7](?:[-,][1-7])*)\s+(\d{2}:\d{2})-(\d{2}:\d{2})$")
class AdminFlow:
    def __init__(self, service: BookingService, calendar: GoogleCalendar):
        self.service = service
        self.calendar = calendar

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

    def _dialog(self, now: int):
        row = self._one("SELECT step, object_id, payload, expires_at FROM admin_dialogs "
                        "WHERE admin_id = ?", (self.service.administrator_id,))
        if row and row[3] <= now:
            self._clear()
            return None
        return row

    def _save(self, step: str, now: int, *, object_id: int | None = None,
              payload: dict | None = None) -> None:
        connection = self.service._connect()
        try:
            connection.execute(
                "INSERT INTO admin_dialogs (admin_id, step, object_id, payload, expires_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(admin_id) DO UPDATE SET "
                "step=excluded.step, object_id=excluded.object_id, payload=excluded.payload, "
                "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
                (self.service.administrator_id, step, object_id,
                 json.dumps(payload or {}, ensure_ascii=False),
                 now + ADMIN_SESSION_SECONDS, now),
            )
            log_event(self.service.logger, "admin_dialog_step", code=step)
        finally:
            connection.close()

    def _clear(self) -> None:
        connection = self.service._connect()
        try:
            connection.execute("DELETE FROM admin_dialogs WHERE admin_id = ?",
                               (self.service.administrator_id,))
        finally:
            connection.close()

    def _local(self, timestamp: int) -> str:
        zone = ZoneInfo(self.service.review_schedule().time_zone)
        return datetime.fromtimestamp(timestamp, zone).strftime("%d.%m.%Y %H:%M")

    def _menu(self) -> Reply:
        return Reply("Управление ботом. Выберите раздел:",
                     (_row(("Запросы доступа", "adm:access"),
                           ("Заявки", "adm:bookings")),
                      _row(("Интервалы", "adm:intervals"),
                           ("Шаблоны", "adm:templates")),
                      _row(("Часы рассмотрения", "adm:schedule"),
                           ("Сводка", "adm:stats")),
                      _row(("Предупреждения", "adm:alerts")),))

    def handle(self, actor_id: int, action: str, now: int,
               *, text: str | None = None) -> Reply:
        if actor_id != self.service.administrator_id:
            log_event(self.service.logger, "access_denied", code="admin_only")
            return Reply("Действие доступно только администратору.")
        try:
            return self._handle(action, now, text=text)
        except (AccessDenied, RuleError, SlotUnavailable) as error:
            return Reply(str(error), (_row(("В админ-меню", "adm:menu")),))
        except CalendarAuthExpired:
            log_event(self.service.logger, "admin_action_failed", code="google_auth_expired")
            return Reply("Доступ Google Календаря истёк. Встреча не подтверждена. "
                         "Сообщите Codex номер заявки: он восстановит подключение. "
                         "После этого откройте заявку и нажмите «Проверить и повторить Google».",
                         (_row(("Заявки", "adm:bookings")),))
        except (CalendarError, StorageUnavailable):
            log_event(self.service.logger, "admin_action_failed", code="dependency")
            return Reply("Сервис временно недоступен. Проверьте предупреждения и повторите позже.",
                         (_row(("Предупреждения", "adm:alerts")),))

    def _handle(self, action: str, now: int, *, text: str | None) -> Reply:
        if action in ("start", "adm:menu"):
            self._clear()
            return self._menu()
        if action == "adm:access":
            rows = self._many(
                "SELECT ar.visitor_id, COALESCE(v.username, '') FROM access_requests ar "
                "JOIN visitors v ON v.telegram_id = ar.visitor_id "
                "WHERE ar.status = 'pending' ORDER BY ar.requested_at, ar.id LIMIT 10")
            buttons = tuple(_row((f"{username or 'ID'} · {visitor_id}",
                                  f"adm:access:{visitor_id}"))
                            for visitor_id, username in rows)
            return Reply("Ожидают доступа:" if rows else "Новых запросов доступа нет.",
                         buttons + (_row(("Заблокировать по ID", "adm:block_input"),
                                         ("В меню", "adm:menu")),))
        if action == "adm:block_input":
            self._save("block_id", now)
            return Reply("Пришлите числовой Telegram ID пользователя для блокировки.")
        if action.startswith("adm:access:"):
            try:
                visitor_id = int(action.split(":")[2])
            except (ValueError, IndexError):
                return self._stale()
            row = self._one("SELECT username, access_status FROM visitors WHERE telegram_id = ?",
                            (visitor_id,))
            if row is None:
                return self._stale()
            return Reply(f"Доступ пользователя ID {visitor_id}, @{row[0] or 'без username'}: {row[1]}.",
                         (_row(("Разрешить", f"adm:permit:{visitor_id}"),
                               ("Отклонить", f"adm:reject:{visitor_id}")),
                          _row(("Заблокировать", f"adm:block:{visitor_id}"),
                               ("К запросам", "adm:access"))))
        for prefix, status in (("adm:permit:", "allowed"),
                               ("adm:reject:", "rejected"),
                               ("adm:block:", "blocked")):
            if action.startswith(prefix):
                try:
                    visitor_id = int(action[len(prefix):])
                except ValueError:
                    return self._stale()
                self.service.decide_access(self.service.administrator_id, visitor_id,
                                           status, now)
                return Reply(f"Статус доступа обновлён: {status}.",
                             (_row(("Запросы доступа", "adm:access")),))
        if action == "adm:bookings":
            rows = [row[:3] for row in Queries(self.service).pending_queue(self.service.administrator_id)]
            buttons = tuple(_row((f"№{ident} · {self._local(starts)} · {status}",
                                  f"adm:booking:{ident}"))
                            for ident, starts, status in rows)
            return Reply("Заявки для решения:" if rows else "Заявок для решения нет.",
                         buttons + (_row(("В меню", "adm:menu")),))
        if action.startswith("adm:booking:"):
            try:
                booking_id = int(action.removeprefix("adm:booking:"))
            except ValueError:
                return self._stale()
            row = self._one(
                "SELECT status, starts_at, ends_at, contact_name_snapshot, "
                "username_snapshot, email_snapshot, purpose, video_service, "
                "template_name_snapshot, decision_deadline_at FROM bookings WHERE id = ?",
                (booking_id,))
            if row is None:
                return self._stale()
            status, starts, ends, name, username, email, purpose, video, template, deadline = row
            details = (f"Заявка №{booking_id}: {status}\n{self._local(starts)}–"
                       f"{datetime.fromtimestamp(ends, ZoneInfo(self.service.review_schedule().time_zone)).strftime('%H:%M')} "
                       f"МСК, {template or 'без шаблона'}, {video or 'без сервиса'}\n"
                       f"Контакт: {name or '—'}, @{username or '—'}, {email or '—'}\n"
                       f"Цель: {purpose or '—'}\n"
                       f"Решение до: {self._local(deadline) if deadline else '—'}")
            if status == "pending":
                buttons = (_row(("Согласовать", f"adm:approve:{booking_id}"),
                                ("Отклонить", f"adm:deny:{booking_id}")),)
            elif status == "awaiting_link":
                buttons = (_row(("Добавить видеоссылку", f"adm:link:{booking_id}")),)
            elif status in ("creating_calendar", "attention"):
                current = self.service.calendar_booking(booking_id)
                buttons = (_row(("Проверить и повторить Google",
                                 f"adm:retry_calendar:{booking_id}")),) \
                    if current.cancel_requested_at is None else ()
            else:
                buttons = ()
            if status in ("pending", "awaiting_link", "confirmed"):
                buttons += (_row(("Отменить заявку", f"adm:cancel:{booking_id}")),)
            return Reply(details, buttons + (_row(("К заявкам", "adm:bookings")),))
        if action.startswith("adm:cancel:"):
            try:
                booking_id = int(action.removeprefix("adm:cancel:"))
            except ValueError:
                return self._stale()
            if self.service.booking_status(booking_id) not in (
                    "pending", "awaiting_link", "confirmed"):
                return self._stale()
            return Reply(f"Отменить заявку №{booking_id}? Подтверждённая встреча будет "
                         "отменена только после ответа Google Календаря.",
                         (_row(("Да, отменить", f"adm:cancel_yes:{booking_id}"),
                               ("Нет", f"adm:booking:{booking_id}")),))
        if action.startswith("adm:cancel_yes:"):
            try:
                booking_id = int(action.removeprefix("adm:cancel_yes:"))
            except ValueError:
                return self._stale()
            status = self.service.cancel_by_admin(self.service.administrator_id,
                                                  booking_id, now)
            if status == "attention":
                return Reply("Запрос на отмену принят. Подтверждение придёт после "
                             "проверки Google Календаря.",
                             (_row(("К заявкам", "adm:bookings")),))
            return Reply("Заявка отменена.", (_row(("К заявкам", "adm:bookings")),))
        for prefix, approve in (("adm:approve:", True), ("adm:deny:", False)):
            if action.startswith(prefix):
                try:
                    booking_id = int(action[len(prefix):])
                except ValueError:
                    return self._stale()
                result = self.service.admin_decision(self.service.administrator_id,
                                                     booking_id, approve, now)
                if result == "awaiting_link":
                    self._save("video_link", now, object_id=booking_id,
                               payload={"revision": self._one("SELECT revision FROM bookings WHERE id=?", (booking_id,))[0]})
                    return Reply("Заявка согласована. Создайте встречу в выбранном сервисе и "
                                 "пришлите её HTTPS-ссылку. До успешного создания события "
                                 "Google посетитель не получит подтверждение.")
                return Reply("Решение по заявке: " + result + ".",
                             (_row(("К заявкам", "adm:bookings")),))
        if action.startswith("adm:link:"):
            try:
                booking_id = int(action.removeprefix("adm:link:"))
            except ValueError:
                return self._stale()
            if self.service.booking_status(booking_id) != "awaiting_link":
                return self._stale()
            self._save("video_link", now, object_id=booking_id,
                       payload={"revision": self._one("SELECT revision FROM bookings WHERE id=?", (booking_id,))[0]})
            return Reply("Пришлите HTTPS-ссылку встречи в выбранном сервисе.")
        if action.startswith("adm:retry_calendar:"):
            try:
                booking_id = int(action.removeprefix("adm:retry_calendar:"))
            except ValueError:
                return self._stale()
            current = self.service.calendar_booking(booking_id)
            if current.status not in ("creating_calendar", "attention") or \
                    current.cancel_requested_at is not None:
                return self._stale()
            result = confirm_with_google(self.service, self.calendar, booking_id,
                                         current.video_url, now)
            return Reply("Встреча подтверждена в Google Календаре." if result == "confirmed"
                         else "Статус заявки: " + result,
                         (_row(("К заявкам", "adm:bookings")),))
        if action == "adm:intervals":
            rows = self._many(
                "SELECT id, starts_at, ends_at, is_manual_exception "
                "FROM availability_intervals WHERE is_published = 1 AND ends_at > ? "
                "ORDER BY starts_at LIMIT 12", (now,))
            buttons = tuple(_row((f"Закрыть №{ident}: {self._local(starts)}", f"adm:close:{ident}"))
                            for ident, starts, _, _ in rows)
            blocks = self._many(
                "SELECT id, starts_at FROM manual_blocks WHERE ends_at > ? "
                "ORDER BY starts_at LIMIT 12", (now,))
            buttons += tuple(_row((f"Снова открыть время №{ident}: {self._local(starts)}",
                                   f"adm:unblock:{ident}")) for ident, starts in blocks)
            return Reply("Опубликованные интервалы:" if rows else "Интервалов пока нет.",
                         buttons + (_row(("Добавить интервал", "adm:publish"),
                                         ("Открыть закрытый день", "adm:exception")),
                                    _row(("Закрыть выбранное время", "adm:block_time")),
                                    _row(("В меню", "adm:menu")),))
        if action in ("adm:publish", "adm:exception"):
            self._save("interval_exception" if action == "adm:exception" else "interval", now)
            return Reply("Пришлите дату и время по Москве: ДД.ММ.ГГГГ ЧЧ:ММ-ЧЧ:ММ. "
                         "Например: 05.10.2026 16:00-18:00.")
        if action == "adm:block_time":
            self._save("block_time", now)
            return Reply("Какое время закрыть? Пришлите ДД.ММ.ГГГГ ЧЧ:ММ-ЧЧ:ММ "
                         "по Москве, например: 05.10.2026 16:30-17:00.")
        if action.startswith("adm:unblock:"):
            try:
                block_id = int(action.removeprefix("adm:unblock:"))
            except ValueError:
                return self._stale()
            self.service.unblock_time(self.service.administrator_id, block_id, now)
            return Reply("Время снова открыто, если исходный интервал опубликован.",
                         (_row(("К интервалам", "adm:intervals")),))
        if action.startswith("adm:close:"):
            try:
                interval_id = int(action.removeprefix("adm:close:"))
            except ValueError:
                return self._stale()
            affected = self.service.close_interval(self.service.administrator_id,
                                                   interval_id, now)
            return Reply(f"Интервал закрыт. Затронутых заявок: {len(affected)}. "
                         "Уже подтверждённые встречи не отменены.",
                         (_row(("К интервалам", "adm:intervals")),))
        if action == "adm:schedule":
            schedule = self.service.review_schedule()
            days = "Пн Вт Ср Чт Пт Сб Вс".split()
            lines = [f"{days[w.weekday]} {w.starts_minute // 60:02d}:{w.starts_minute % 60:02d}-"
                     f"{w.ends_minute // 60:02d}:{w.ends_minute % 60:02d}"
                     for w in schedule.windows]
            return Reply("Часы рассмотрения (" + schedule.time_zone + "):\n" +
                         "\n".join(lines),
                         (_row(("Изменить", "adm:schedule_edit"),
                               ("В меню", "adm:menu")),))
        if action == "adm:schedule_edit":
            self._save("schedule", now)
            return Reply("Пришлите дни и часы по Москве, по одной строке. "
                         "Дни: 1=Пн … 7=Вс. Пример: 1-5 10:00-19:00. "
                         "Можно указать несколько строк и отдельные дни через запятую.")
        if action == "adm:templates":
            rows = self._many("SELECT id, name, is_active FROM meeting_templates ORDER BY id")
            buttons = tuple(_row((f"№{ident} · {name} {'✓' if active else 'скрыт'}",
                                  f"adm:template:{ident}")) for ident, name, active in rows)
            return Reply("Шаблоны:" if rows else "Шаблонов пока нет.",
                         buttons + (_row(("Создать шаблон", "adm:template_new"),
                                         ("В меню", "adm:menu")),))
        if action == "adm:template_new":
            self._save("template_name", now)
            return Reply("Напишите название шаблона (до 100 символов).")
        if action.startswith("adm:template:"):
            try:
                template_id = int(action.removeprefix("adm:template:"))
            except ValueError:
                return self._stale()
            row = self._one("SELECT name, description, instructions, is_active "
                            "FROM meeting_templates WHERE id = ?", (template_id,))
            if row is None:
                return self._stale()
            return Reply(f"Шаблон №{template_id}: {row[0]}\n{row[1]}\nИнструкция: {row[2]}",
                         (_row(("Изменить", f"adm:template_edit:{template_id}"),
                               (("Скрыть" if row[3] else "Показать"),
                                f"adm:template_toggle:{template_id}")),
                          _row(("К шаблонам", "adm:templates")),))
        for prefix in ("adm:template_edit:", "adm:template_toggle:"):
            if action.startswith(prefix):
                try:
                    template_id = int(action[len(prefix):])
                except ValueError:
                    return self._stale()
                row = self._one("SELECT name, description, instructions, is_active, revision "
                                "FROM meeting_templates WHERE id = ?", (template_id,))
                if row is None:
                    return self._stale()
                if prefix.endswith("edit:"):
                    self._save("template_name", now, object_id=template_id,
                               payload={"active": bool(row[3]), "revision": row[4]})
                    return Reply("Напишите новое название шаблона (до 100 символов).")
                self.service.update_template(self.service.administrator_id, template_id,
                                             name=row[0], description=row[1],
                                             instructions=row[2], is_active=not bool(row[3]),
                                             now=now)
                return Reply("Видимость шаблона изменена.",
                             (_row(("К шаблонам", "adm:templates")),))
        if action in ("adm:stats", "adm:stats:7", "adm:stats:30"):
            days = 30 if action == "adm:stats:30" else 7
            return self._stats(days, now)
        if action == "adm:alerts":
            attention = self._one("SELECT COUNT(*) FROM bookings WHERE status = 'attention'")[0]
            failed = self._one("SELECT COUNT(*) FROM notifications WHERE status = 'failed'")[0]
            no_link = self._one("SELECT COUNT(*) FROM bookings WHERE status = 'awaiting_link'")[0]
            return Reply(f"Предупреждения: календарь/отмена — {attention}; "
                         f"уведомления — {failed}; без видеоссылки — {no_link}.",
                         (_row(("Заявки", "adm:bookings"), ("В меню", "adm:menu")),))
        if action == "text":
            return self._text(now, text)
        return self._stale()

    def _text(self, now: int, text: str | None) -> Reply:
        dialog = self._dialog(now)
        value = (text or "").strip()
        if dialog is None:
            return self._stale()
        if not value or len(value) > MAX_ADMIN_TEXT:
            return Reply("Пришлите текст длиной от 1 до 1000 символов.")
        step, object_id, payload_text, _ = dialog
        payload = json.loads(payload_text)
        if step == "block_id":
            if not value.isdecimal():
                return Reply("Нужен только числовой Telegram ID.")
            self._clear()
            self.service.decide_access(self.service.administrator_id, int(value), "blocked", now)
            return Reply("Пользователь заблокирован.",
                         (_row(("Запросы доступа", "adm:access")),))
        if step == "video_link":
            row = self._one("SELECT video_service FROM bookings WHERE id = ?", (object_id,))
            if row is None or not valid_video_url(value, row[0]):
                log_event(self.service.logger, "video_url_rejected", code="format")
                return Reply("Нужна HTTPS-ссылка выбранного сервиса: Яндекс Телемост или Zoom.")
            result = Commands(self.service, self.calendar, policy_version="").confirm(
                self.service.administrator_id, object_id, value, now, revision=payload.get("revision"))
            self._clear()
            return Reply("Встреча подтверждена в Google Календаре." if result == "confirmed"
                         else "Статус заявки: " + result,
                         (_row(("К заявкам", "adm:bookings")),))
        if step in ("interval", "interval_exception", "block_time"):
            match = INTERVAL_PATTERN.fullmatch(value)
            if not match:
                return Reply("Формат: ДД.ММ.ГГГГ ЧЧ:ММ-ЧЧ:ММ.")
            try:
                zone = ZoneInfo(self.service.review_schedule().time_zone)
                start = datetime.strptime(match[1] + " " + match[2], "%d.%m.%Y %H:%M").replace(tzinfo=zone)
                end = datetime.strptime(match[1] + " " + match[3], "%d.%m.%Y %H:%M").replace(tzinfo=zone)
            except ValueError:
                return Reply("Проверьте дату и время. Пример: 05.10.2026 16:00-18:00.")
            if step == "block_time":
                block_id, affected = self.service.block_time(
                    self.service.administrator_id, int(start.timestamp()),
                    int(end.timestamp()), now)
                self._clear()
                return Reply(f"Время закрыто, блок №{block_id}. "
                             f"Затронутых заявок: {len(affected)}. "
                             "Они не отменены автоматически.",
                             (_row(("К интервалам", "adm:intervals")),))
            interval_id = self.service.publish_interval(
                self.service.administrator_id, int(start.timestamp()), int(end.timestamp()),
                str(zone), now, manual_exception=step == "interval_exception")
            self._clear()
            return Reply(f"Интервал №{interval_id} опубликован.",
                         (_row(("К интервалам", "adm:intervals")),))
        if step == "schedule":
            windows: list[ReviewWindow] = []
            for line in value.splitlines():
                match = REVIEW_PATTERN.fullmatch(line.strip())
                if not match:
                    return Reply("Проверьте строки. Пример: 1-5 10:00-19:00.")
                days_part, start_part, end_part = match.groups()
                try:
                    start = datetime.strptime(start_part, "%H:%M")
                    end = datetime.strptime(end_part, "%H:%M")
                    starts_minute = start.hour * 60 + start.minute
                    ends_minute = end.hour * 60 + end.minute
                    if "-" in days_part:
                        lo, hi = (int(item) for item in days_part.split("-"))
                        if lo > hi:
                            raise ValueError("Day range is reversed")
                        days = range(lo, hi + 1)
                    else:
                        days = (int(item) for item in days_part.split(","))
                    windows.extend(ReviewWindow(day - 1, starts_minute, ends_minute)
                                   for day in days)
                except ValueError:
                    return Reply("Проверьте дни и часы. Пример: 1-5 10:00-19:00.")
            try:
                schedule = ReviewSchedule("Europe/Moscow", tuple(windows))
            except ValueError:
                return Reply("Часы пересекаются или заданы неверно. Проверьте расписание.")
            self.service.set_review_schedule(self.service.administrator_id, schedule, now)
            self._clear()
            return Reply("Часы рассмотрения обновлены.",
                         (_row(("Показать часы", "adm:schedule")),))
        if step == "template_name":
            if len(value) > 100:
                return Reply("Название — не более 100 символов.")
            payload["name"] = value
            self._save("template_description", now, object_id=object_id, payload=payload)
            return Reply("Напишите описание шаблона (до 500 символов).")
        if step == "template_description":
            if len(value) > 500:
                return Reply("Описание — не более 500 символов.")
            payload["description"] = value
            self._save("template_instructions", now, object_id=object_id, payload=payload)
            return Reply("Напишите инструкцию для встречи (до 500 символов).")
        if step == "template_instructions":
            if len(value) > 500:
                return Reply("Инструкция — не более 500 символов.")
            if object_id is None:
                template_id = self.service.create_template(
                    self.service.administrator_id, payload["name"],
                    payload["description"], value, now)
            else:
                template_id = object_id
                self.service.update_template(self.service.administrator_id, template_id,
                                             name=payload["name"],
                                             description=payload["description"],
                                             instructions=value,
                                             is_active=payload["active"], now=now,
                                             expected_revision=payload["revision"])
            self._clear()
            return Reply(f"Шаблон №{template_id} сохранён.",
                         (_row(("К шаблонам", "adm:templates")),))
        return self._stale()

    def _stats(self, days: int, now: int) -> Reply:
        stats = Queries(self.service).stats(self.service.administrator_id, days, now)
        return Reply(
            f"Сводка за {days} дней (без контактов):\n"
            f"Запросы доступа: {stats['access']}; заявки: {stats['total']}; подтверждены: {stats['confirmed']}; "
            f"отклонены: {stats['rejected']}; истекли: {stats['expired']}; "
            f"отменены: {stats['cancelled']}; повторные записи: {stats['repeat']}; "
            f"сохранённые контакты: {stats['reused']}; Телемост: {stats['telemost']}; Zoom: {stats['zoom']}; "
            f"среднее время решения: {stats['average_minutes']} мин.",
            (_row(("7 дней", "adm:stats:7"), ("30 дней", "adm:stats:30")),
             _row(("В меню", "adm:menu")),))

    @staticmethod
    def _stale() -> Reply:
        return Reply("Кнопка или шаг устарели. Откройте админ-меню заново.",
                     (_row(("В админ-меню", "adm:menu")),))
