"""Telegram notifications are delivered, retried and never affect booking truth."""

from datetime import datetime
from zoneinfo import ZoneInfo

from meeting_bot.admin_flow import AdminFlow
from meeting_bot.database import connect
from meeting_bot import notification_delivery
from meeting_bot.notification_delivery import run_due_notifications
from meeting_bot.service import BookingService
from meeting_bot.technical_log import configure_logging


ADMIN = 900
VISITOR = 101
ZONE = ZoneInfo("Europe/Moscow")


def at(day: int, hour: int) -> int:
    return int(datetime(2026, 10, day, hour, tzinfo=ZONE).timestamp())


def pending_booking(tmp_path):
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=ADMIN)
    service.initialize()
    now = at(3, 12)
    service.request_access(VISITOR, "guest", now)
    service.decide_access(ADMIN, VISITOR, "allowed", now)
    service.record_consent(VISITOR, "v1", now)
    template = service.create_template(ADMIN, "Консультация", "", "", now)
    interval = service.publish_interval(ADMIN, at(5, 16), at(5, 18), "Europe/Moscow", now)
    booking = service.create_hold(VISITOR, template, interval, at(5, 16), 30,
                                  "telemost", now)
    service.submit_request(VISITOR, booking, name="Гость", username="guest",
                           email="guest@example.test", phone=None, purpose="Личная цель",
                           policy_version="v1", now=now)
    return service, booking, now


def test_telegram_render_from_persistent_queue(tmp_path):
    service, booking, now = pending_booking(tmp_path)
    telegram = []
    settings = {"BOT_ADMIN_ID": str(ADMIN)}
    sent = run_due_notifications(service.path, now, settings,
                                 telegram_send=lambda recipient, text: telegram.append(
                                     (recipient, text)))
    assert sent >= 3
    assert any(recipient == ADMIN and "Новая заявка" in text for recipient, text in telegram)
    assert run_due_notifications(service.path, now, settings,
                                 telegram_send=lambda *args: None) == 0
    with connect(service.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications WHERE status = 'sent'").fetchone()[0] == sent
        assert connection.execute("SELECT COUNT(*) FROM notifications "
                                  "WHERE channel != 'telegram'").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM bookings WHERE id = ?",
                                  (booking,)).fetchone()[0] == "pending"


def test_access_approval_notice_offers_booking_button(tmp_path, monkeypatch):
    service, _, now = pending_booking(tmp_path)
    sent = []

    class FakeSession:
        async def close(self):
            pass

    class FakeBot:
        def __init__(self, token):
            self.session = FakeSession()

        async def send_message(self, recipient, text, reply_markup=None):
            sent.append((recipient, text, reply_markup))

    monkeypatch.setattr(notification_delivery, "telegram_bot", FakeBot)
    run_due_notifications(
        service.path, now, {"BOT_ADMIN_ID": str(ADMIN), "TELEGRAM_BOT_TOKEN": "test"}
    )
    approval = [(text, markup) for recipient, text, markup in sent
                if recipient == VISITOR and "Доступ к записи открыт" in text]
    assert len(approval) == 1
    assert approval[0][1].inline_keyboard[0][0].callback_data == "book"


def test_telegram_failure_retries_after_restart_without_changing_booking(tmp_path):
    service, booking, now = pending_booking(tmp_path)
    settings = {"BOT_ADMIN_ID": str(ADMIN)}
    log_path = tmp_path / "technical.jsonl"
    logger = configure_logging(log_path)

    def fail_telegram(recipient, text):
        raise OSError("private-token-and-contact@example.test")

    assert run_due_notifications(service.path, now, settings,
                                 telegram_send=fail_telegram, logger=logger, limit=1) == 0
    log_text = log_path.read_text(encoding="utf-8")
    assert "notice_failed" in log_text
    assert "private-token-and-contact@example.test" not in log_text
    with connect(service.path) as connection:
        first = connection.execute("SELECT id, next_attempt_at FROM notifications "
                                   "WHERE status = 'failed'").fetchone()
        count = connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        assert first[1] == now + 120
        assert connection.execute("SELECT status FROM bookings WHERE id = ?",
                                  (booking,)).fetchone()[0] == "pending"
        assert connection.execute("SELECT COUNT(*) FROM notifications WHERE status = 'failed'").fetchone()[0] == 1
    assert "уведомления — 1" in AdminFlow(service, object()).handle(
        ADMIN, "adm:alerts", now).text
    delivered = []
    run_due_notifications(service.path, now + 120, settings,
                          telegram_send=lambda recipient, text: delivered.append(
                              (recipient, text)))
    assert any(recipient == ADMIN for recipient, _ in delivered)
    with connect(service.path) as connection:
        assert connection.execute("SELECT status, attempts FROM notifications "
                                  "WHERE id = ?", (first[0],)).fetchone() == ("sent", 2)
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == count
        assert connection.execute("SELECT status FROM bookings WHERE id = ?",
                                  (booking,)).fetchone()[0] == "pending"
    assert "уведомления — 0" in AdminFlow(service, object()).handle(
        ADMIN, "adm:alerts", now + 120).text


def test_missing_telegram_token_leaves_queue_untouched(tmp_path):
    service, _, now = pending_booking(tmp_path)
    settings = {"BOT_ADMIN_ID": str(ADMIN)}
    assert run_due_notifications(service.path, now, settings) == 0
    with connect(service.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications "
                                  "WHERE channel = 'telegram' AND status = 'queued'").fetchone()[0] >= 3


def test_local_test_never_delivers_to_unlisted_account(tmp_path):
    service, _, now = pending_booking(tmp_path)
    sent_to = []
    run_due_notifications(service.path, now, {"BOT_ADMIN_ID": str(ADMIN)},
                          telegram_send=lambda recipient, text: sent_to.append(recipient),
                          allowed_recipients=frozenset((ADMIN,)))
    assert sent_to and set(sent_to) == {ADMIN}
    with connect(service.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE status = 'failed'").fetchone()[0] >= 1


def test_telegram_confirms_cancellation_independent_of_google_email(tmp_path):
    service, booking, now = pending_booking(tmp_path)
    service.admin_decision(ADMIN, booking, True, now)
    video_url = "https://telemost.yandex.ru/j/test123"
    service.begin_calendar_creation(booking, video_url, now)
    event_id = service.reserve_google_event_id(booking, now)
    service.mark_calendar_created(booking, event_id, video_url, now)
    assert service.cancel_by_visitor(VISITOR, booking, now) == "attention"
    service.mark_calendar_cancelled(booking, now)
    messages = []
    settings = {"BOT_ADMIN_ID": str(ADMIN)}
    run_due_notifications(service.path, now, settings,
                          telegram_send=lambda recipient, text: messages.append(
                              (recipient, text)))
    assert any(recipient == VISITOR and "отменена" in text
               for recipient, text in messages)
    assert any(recipient == ADMIN and "Посетитель отменил" in text
               for recipient, text in messages)
    assert service.booking_status(booking) == "cancelled_by_visitor"


def test_blocked_visitor_is_notified_even_after_prior_access_approval(tmp_path):
    service, _, now = pending_booking(tmp_path)
    service.decide_access(ADMIN, VISITOR, "blocked", now + 60)
    messages = []
    settings = {"BOT_ADMIN_ID": str(ADMIN)}
    run_due_notifications(service.path, now + 60, settings,
                          telegram_send=lambda recipient, text: messages.append(
                              (recipient, text)))
    assert any(recipient == VISITOR and "заблокирован" in text
               for recipient, text in messages)
