"""One local journey through visitor, administrator, Google and the worker."""

from datetime import datetime
from zoneinfo import ZoneInfo

from meeting_bot.admin_flow import AdminFlow
from meeting_bot.calendar_flow import confirm_with_google
from meeting_bot.database import connect
from meeting_bot.google_calendar import CalendarUnavailable
from meeting_bot.notification_delivery import run_due_notifications
from meeting_bot.service import BookingService
from meeting_bot.technical_log import configure_logging
from meeting_bot.visitor_flow import VisitorFlow
from meeting_bot.worker import run_due_jobs


ADMIN = 900
VISITOR = 101
ZONE = ZoneInfo("Europe/Moscow")
VIDEO_URL = "https://telemost.yandex.ru/j/test123"


def at(day: int, hour: int) -> int:
    return int(datetime(2026, 10, day, hour, tzinfo=ZONE).timestamp())


def button(reply, prefix: str) -> str:
    return next(item.action for row in reply.rows for item in row
                if item.action.startswith(prefix))


class FakeCalendar:
    def __init__(self):
        self.events = set()
        self.created = 0
        self.deleted = 0
        self.fail_after_create = False

    def freebusy(self, starts_at, ends_at):
        return []

    def find_matching(self, *, event_id, **kwargs):
        return event_id in self.events

    def create_or_find(self, *, event_id, **kwargs):
        self.events.add(event_id)
        self.created += 1
        if self.fail_after_create:
            raise CalendarUnavailable()
        return event_id

    def delete_or_find_absent(self, event_id, booking_id):
        self.events.discard(event_id)
        self.deleted += 1


def test_full_journey_survives_restart_and_delivers_telegram(tmp_path):
    log_path = tmp_path / "technical.jsonl"
    logger = configure_logging(log_path)
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=ADMIN,
                             logger=logger)
    service.initialize()
    now = at(3, 12)
    service.create_template(ADMIN, "Консультация", "", "", now)
    service.publish_interval(ADMIN, at(5, 16), at(5, 18), "Europe/Moscow", now)
    calendar = FakeCalendar()
    visitor = VisitorFlow(service, calendar, policy_url="https://example.test/policy",
                          policy_version="test-v1")
    admin = AdminFlow(service, calendar)

    def visit(action, text=None):
        return visitor.handle(VISITOR, "guest", action, now, text=text)

    assert "Политика" in visit("start").text
    visit("access")
    assert "Ожидайте" in visit("access:yes").text
    assert "allowed" in admin.handle(ADMIN, f"adm:permit:{VISITOR}", now).text
    reply = visit("book")
    reply = visit(button(reply, "t:"))
    reply = visit("d:30")
    reply = visit("v:telemost")
    reply = visit(button(reply, "day:"))
    reply = visit(button(reply, "time:"))
    assert "Напишите ваше имя" in reply.text
    visit("text", "Гость")
    visit("text", "guest@example.test")
    visit("phone:skip")
    visit("text", "Обсудить проект")
    assert "Проверьте заявку" in visit("consent:yes").text
    assert "отправлена" in visit("send").text
    assert "Заявка согласована" in admin.handle(ADMIN, "adm:approve:1", now).text
    assert "подтверждена" in admin.handle(ADMIN, "text", now, text=VIDEO_URL).text
    assert confirm_with_google(service, calendar, 1, VIDEO_URL, now) == "confirmed"
    assert calendar.created == 1

    restarted = BookingService(service.path, administrator_id=ADMIN, logger=logger)
    restarted.initialize()
    visitor = VisitorFlow(restarted, calendar, policy_url="https://example.test/policy",
                          policy_version="test-v1")
    mine = visit("mine")
    question = visit(button(mine, "cancel:"))
    assert "Отменить" in question.text
    assert "после проверки календаря" in visit(button(question, "cancel_yes:")).text
    assert restarted.booking_status(1) == "attention"
    assert run_due_jobs(restarted.path, now + 1, calendar=calendar,
                        administrator_id=ADMIN) == 1
    assert restarted.booking_status(1) == "cancelled_by_visitor"
    assert calendar.deleted == 1 and not calendar.events

    messages = []
    run_due_notifications(restarted.path, now + 1, {"BOT_ADMIN_ID": str(ADMIN)},
                          telegram_send=lambda recipient, text: messages.append(
                              (recipient, text)))
    assert any(recipient == ADMIN and "Новая заявка" in text
               for recipient, text in messages)
    assert any(recipient == VISITOR and "подтверждена" in text
               for recipient, text in messages)
    assert any(recipient == VISITOR and "отменена" in text
               for recipient, text in messages)
    assert any(recipient == ADMIN and "Посетитель отменил" in text
               for recipient, text in messages)
    with connect(restarted.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications "
                                  "WHERE status = 'sent'").fetchone()[0] == len(messages)
        assert connection.execute("SELECT COUNT(*) FROM notifications "
                                  "WHERE channel != 'telegram'").fetchone()[0] == 0
    logs = log_path.read_text(encoding="utf-8")
    for private in ("guest@example.test", "Обсудить проект", VIDEO_URL):
        assert private not in logs


def test_admin_recovers_after_google_timeout_without_second_event(tmp_path):
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=ADMIN)
    service.initialize()
    now = at(3, 12)
    service.request_access(VISITOR, "guest", now)
    service.decide_access(ADMIN, VISITOR, "allowed", now)
    service.record_consent(VISITOR, "test-v1", now)
    template = service.create_template(ADMIN, "Консультация", "", "", now)
    interval = service.publish_interval(ADMIN, at(5, 16), at(5, 18),
                                        "Europe/Moscow", now)
    booking = service.create_hold(VISITOR, template, interval, at(5, 16),
                                  30, "telemost", now)
    service.submit_request(VISITOR, booking, name="Гость", username="guest",
                           email="guest@example.test", phone=None,
                           purpose="Обсудить проект", policy_version="test-v1", now=now)
    calendar = FakeCalendar()
    admin = AdminFlow(service, calendar)
    admin.handle(ADMIN, f"adm:approve:{booking}", now)
    calendar.fail_after_create = True
    assert "недоступен" in admin.handle(ADMIN, "text", now, text=VIDEO_URL).text
    assert service.booking_status(booking) == "attention"
    assert calendar.created == 1
    details = admin.handle(ADMIN, f"adm:booking:{booking}", now)
    retry = button(details, "adm:retry_calendar:")
    calendar.fail_after_create = False
    assert "подтверждена" in admin.handle(ADMIN, retry, now + 60).text
    assert service.booking_status(booking) == "confirmed"
    assert calendar.created == 1
    with connect(service.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM meetings "
                                  "WHERE booking_id = ?", (booking,)).fetchone()[0] == 1

    second = service.create_hold(VISITOR, template, interval, at(5, 17),
                                 30, "telemost", now)
    service.submit_request(VISITOR, second, name="Гость", username="guest",
                           email="guest@example.test", phone=None,
                           purpose="Вторая тема", policy_version="test-v1", now=now)
    service.admin_decision(ADMIN, second, True, now)
    service.begin_calendar_creation(second, VIDEO_URL, now)
    assert service.booking_status(second) == "creating_calendar"
    restarted = BookingService(service.path, administrator_id=ADMIN)
    restarted.initialize()
    admin_after_restart = AdminFlow(restarted, calendar)
    details = admin_after_restart.handle(ADMIN, f"adm:booking:{second}", now)
    retry = button(details, "adm:retry_calendar:")
    assert "подтверждена" in admin_after_restart.handle(ADMIN, retry, now + 60).text
    assert restarted.booking_status(second) == "confirmed"
    assert calendar.created == 2
