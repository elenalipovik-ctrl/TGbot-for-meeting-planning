"""Stage 5 administrator actions against temporary SQLite and a fake Google boundary."""

from datetime import datetime
from zoneinfo import ZoneInfo

from meeting_bot.admin_flow import AdminFlow, valid_video_url
from meeting_bot.database import connect
from meeting_bot.google_calendar import CalendarAuthExpired, CalendarUnavailable
from meeting_bot.service import BookingService
from meeting_bot.technical_log import configure_logging


ADMIN = 900
VISITOR = 101
ZONE = ZoneInfo("Europe/Moscow")


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp())


class FakeCalendar:
    def __init__(self):
        self.created = 0
        self.fail = False
        self.expired = False

    def freebusy(self, starts_at, ends_at):
        if self.expired:
            raise CalendarAuthExpired()
        if self.fail:
            raise CalendarUnavailable()
        return []

    def find_matching(self, **kwargs):
        return self.created > 0

    def create_or_find(self, **kwargs):
        self.created += 1


def setup(tmp_path):
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=ADMIN)
    service.initialize()
    calendar = FakeCalendar()
    return service, calendar, AdminFlow(service, calendar), at(3, 12)


def request(service, now):
    service.request_access(VISITOR, "guest", now)
    service.decide_access(ADMIN, VISITOR, "allowed", now)
    service.record_consent(VISITOR, "v1", now)
    template = service.create_template(ADMIN, "Консультация", "Описание", "Инструкция", now)
    interval = service.publish_interval(ADMIN, at(5, 16), at(5, 18), "Europe/Moscow", now)
    booking = service.create_hold(VISITOR, template, interval, at(5, 16), 30,
                                  "telemost", now)
    service.submit_request(VISITOR, booking, name="Гость", username="guest",
                           email="guest@example.test", phone=None, purpose="Обсудить проект",
                           policy_version="v1", now=now)
    return booking


def test_admin_only_and_access_decision(tmp_path):
    service, _, flow, now = setup(tmp_path)
    assert "только администратору" in flow.handle(VISITOR, "start", now).text
    assert flow._one("SELECT COUNT(*) FROM admin_dialogs")[0] == 0
    service.request_access(VISITOR, "guest", now)
    assert "101" in str(flow.handle(ADMIN, "adm:access", now).rows)
    assert "allowed" in flow.handle(ADMIN, "adm:permit:101", now).text
    assert flow._one("SELECT access_status FROM visitors WHERE telegram_id = 101")[0] == "allowed"
    assert "blocked" in flow.handle(ADMIN, "adm:block:101", now).text


def test_schedule_interval_template_and_stats(tmp_path):
    service, _, flow, now = setup(tmp_path)
    flow.handle(ADMIN, "adm:schedule_edit", now)
    assert "Проверьте дни" in flow.handle(ADMIN, "text", now,
                                         text="5-1 10:00-19:00").text
    assert "обновлены" in flow.handle(ADMIN, "text", now,
                                      text="1-5 10:00-19:00").text
    assert len(service.review_schedule().windows) == 5
    flow.handle(ADMIN, "adm:template_new", now)
    flow.handle(ADMIN, "text", now, text="Консультация")
    flow.handle(ADMIN, "text", now, text="Описание")
    assert "сохранён" in flow.handle(ADMIN, "text", now, text="Инструкция").text
    flow.handle(ADMIN, "adm:publish", now)
    assert "опубликован" in flow.handle(ADMIN, "text", now,
                                        text="05.10.2026 16:00-18:00").text
    assert any(button.action == "adm:close:1"
               for row in flow.handle(ADMIN, "adm:intervals", now).rows
               for button in row)
    assert "без контактов" in flow.handle(ADMIN, "adm:stats:7", now).text
    service.request_access(VISITOR, "guest", now)
    service.decide_access(ADMIN, VISITOR, "allowed", now)
    assert at(5, 16, 30) in service.list_slots(VISITOR, now, 30, "telemost")
    flow.handle(ADMIN, "adm:block_time", now)
    assert "Время закрыто" in flow.handle(ADMIN, "text", now,
                                          text="05.10.2026 16:30-17:00").text
    assert at(5, 16, 30) not in service.list_slots(VISITOR, now, 30, "telemost")
    assert at(5, 16) in service.list_slots(VISITOR, now, 30, "telemost")
    flow.handle(ADMIN, "adm:unblock:1", now)
    assert at(5, 16, 30) in service.list_slots(VISITOR, now, 30, "telemost")
    assert "Интервал закрыт" in flow.handle(ADMIN, "adm:close:1", now).text


def test_approval_needs_valid_link_and_google_once(tmp_path):
    service, calendar, flow, now = setup(tmp_path)
    booking = request(service, now)
    assert "Согласовать" in str(flow.handle(ADMIN, f"adm:booking:{booking}", now).rows)
    assert "Заявка согласована" in flow.handle(ADMIN, f"adm:approve:{booking}", now).text
    assert service.booking_status(booking) == "awaiting_link"
    assert "Нужна HTTPS" in flow.handle(ADMIN, "text", now,
                                         text="https://telemost.yandex.ru.evil.test/j/123").text
    assert service.booking_status(booking) == "awaiting_link"
    assert "подтверждена" in flow.handle(ADMIN, "text", now,
                                         text="https://telemost.yandex.ru/j/123").text
    assert service.booking_status(booking) == "confirmed"
    assert calendar.created == 1
    assert "Кнопка" in flow.handle(ADMIN, f"adm:link:{booking}", now).text
    assert "Google Календаря" in flow.handle(ADMIN, f"adm:cancel:{booking}", now).text
    assert "Запрос на отмену" in flow.handle(ADMIN, f"adm:cancel_yes:{booking}", now).text
    assert service.booking_status(booking) == "attention"
    with connect(service.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM meetings WHERE booking_id = ?",
                                  (booking,)).fetchone()[0] == 1


def test_video_host_validation():
    assert valid_video_url("https://telemost.yandex.ru/j/abc", "telemost")
    assert valid_video_url("https://us05web.zoom.us/j/123", "zoom")
    assert not valid_video_url("http://telemost.yandex.ru/j/abc", "telemost")
    assert not valid_video_url("https://telemost.yandex.ru.evil.test/j/abc", "telemost")
    assert not valid_video_url("https://zoom.us.evil.test/j/123", "zoom")
    assert not valid_video_url("https://us05web.zoom.us/j/123", "telemost")


def test_google_failure_never_confirms_and_logs_omit_contact_data(tmp_path):
    log_path = tmp_path / "technical.jsonl"
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=ADMIN,
                             logger=configure_logging(log_path))
    service.initialize()
    calendar = FakeCalendar()
    flow = AdminFlow(service, calendar)
    now = at(3, 12)
    booking = request(service, now)
    flow.handle(ADMIN, f"adm:approve:{booking}", now)
    calendar.fail = True
    reply = flow.handle(ADMIN, "text", now,
                        text="https://telemost.yandex.ru/j/private123")
    assert "недоступен" in reply.text
    assert service.booking_status(booking) == "attention"
    with connect(service.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE booking_id = ? "
            "AND kind = 'calendar_attention'", (booking,)).fetchone()[0] == 1
    logs = log_path.read_text(encoding="utf-8")
    assert "guest@example.test" not in logs
    assert "Личная цель" not in logs
    assert "private123" not in logs


def test_expired_google_access_explains_recovery_to_owner(tmp_path):
    service, calendar, flow, now = setup(tmp_path)
    booking = request(service, now)
    flow.handle(ADMIN, f"adm:approve:{booking}", now)
    calendar.expired = True
    reply = flow.handle(ADMIN, "text", now,
                        text="https://telemost.yandex.ru/j/123")
    assert "Доступ Google Календаря истёк" in reply.text
    assert "Встреча не подтверждена" in reply.text
    assert service.booking_status(booking) == "attention"
    calendar.expired = False
    retry = flow.handle(ADMIN, f"adm:booking:{booking}", now)
    action = next(button.action for row in retry.rows for button in row
                  if button.action.startswith("adm:retry_calendar:"))
    assert "подтверждена" in flow.handle(ADMIN, action, now + 60).text


def test_statistics_and_warnings_are_aggregated(tmp_path):
    service, _, flow, now = setup(tmp_path)
    booking = request(service, now)
    summary = flow.handle(ADMIN, "adm:stats:7", now).text
    assert "заявки: 1" in summary
    assert "Телемост: 1" in summary
    assert "guest@example.test" not in summary
    monthly = flow.handle(ADMIN, "adm:stats:30", now).text
    assert "заявки: 1" in monthly
    assert "guest@example.test" not in monthly
    service.admin_decision(ADMIN, booking, True, now + 60)
    assert "без видеоссылки — 1" in flow.handle(ADMIN, "adm:alerts", now).text
