"""Visitor dialog checks use temporary SQLite and a fake free/busy boundary."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from meeting_bot.database import connect
from meeting_bot.rules import BusyInterval
from meeting_bot.service import BookingService, RuleError
from meeting_bot.technical_log import configure_logging
from meeting_bot.telegram_app import _markup
from meeting_bot import telegram_app
from meeting_bot.visitor_flow import VisitorFlow


ZONE = ZoneInfo("Europe/Moscow")


def epoch(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp())


class FakeCalendar:
    def __init__(self):
        self.busy = []
        self.calls = 0

    def freebusy(self, starts_at, ends_at):
        self.calls += 1
        return [part for part in self.busy
                if part.starts_at < ends_at and starts_at < part.ends_at]


@pytest.fixture
def setup_flow(tmp_path):
    service = BookingService(tmp_path / "bot.sqlite", administrator_id=900)
    service.initialize()
    now = epoch(3, 12)
    service.create_template(900, "Консультация", "", "", now)
    service.publish_interval(900, epoch(5, 16), epoch(5, 18), "Europe/Moscow", now)
    calendar = FakeCalendar()
    flow = VisitorFlow(service, calendar, policy_url="https://example.org/privacy",
                       policy_version="2026-10-03")
    return service, calendar, flow, now


def action(flow, visitor, username, name, now, text=None):
    return flow.handle(visitor, username, name, now, text=text)


def button(reply, prefix):
    return next(item.action for row in reply.rows for item in row
                if item.action.startswith(prefix))


def allow(service, flow, visitor, now, username="guest"):
    reply = action(flow, visitor, username, "start", now)
    assert "Политика" in reply.text
    reply = action(flow, visitor, username, "access", now)
    assert "Даю согласие" in str(reply.rows)
    reply = action(flow, visitor, username, "access:yes", now)
    assert "Ожидайте" in reply.text
    service.decide_access(900, visitor, "allowed", now)
    assert "Записаться" in action(flow, visitor, username, "menu", now).text + str(
        action(flow, visitor, username, "menu", now).rows)


def to_contact_form(flow, visitor, now, username="guest", video="telemost"):
    reply = action(flow, visitor, username, "book", now)
    reply = action(flow, visitor, username, button(reply, "t:"), now)
    reply = action(flow, visitor, username, "d:30", now)
    reply = action(flow, visitor, username, f"v:{video}", now)
    reply = action(flow, visitor, username, button(reply, "day:"), now)
    reply = action(flow, visitor, username, button(reply, "time:"), now)
    return reply


def finish_form(flow, visitor, now, username="guest", email="guest@example.org"):
    reply = action(flow, visitor, username, "text", now, "Гость")
    assert "email" in reply.text
    reply = action(flow, visitor, username, "text", now, email)
    reply = action(flow, visitor, username, "phone:skip", now)
    reply = action(flow, visitor, username, "text", now, "Обсудить проект")
    assert "согласие" in reply.text and "https://example.org/privacy" in reply.text
    reply = action(flow, visitor, username, "consent:yes", now)
    assert "Проверьте заявку" in reply.text
    return reply


def test_first_request_and_status_and_cancel(setup_flow):
    service, calendar, flow, now = setup_flow
    allow(service, flow, 101, now)
    assert "Напишите, как к вам обращаться" in to_contact_form(flow, 101, now).text
    review = finish_form(flow, 101, now)
    assert "МСК" in review.text and "Обсудить проект" in review.text
    sent = action(flow, 101, "guest", "send", now)
    assert "отправлена" in sent.text
    assert "На рассмотрении" in action(flow, 101, "guest", "mine", now).text
    stale = action(flow, 101, "guest", "send", now)
    assert "устарела" in stale.text
    mine = action(flow, 101, "guest", "mine", now)
    confirm = action(flow, 101, "guest", button(mine, "cancel:"), now)
    assert "Отменить заявку" in confirm.text
    cancelled = action(flow, 101, "guest", button(confirm, "cancel_yes:"), now)
    assert "отменена" in cancelled.text
    assert calendar.calls >= 3


def test_repeat_saved_contacts_explicit_choice_and_restart(setup_flow):
    service, calendar, flow, now = setup_flow
    allow(service, flow, 102, now)
    to_contact_form(flow, 102, now)
    finish_form(flow, 102, now)
    action(flow, 102, "guest", "send", now)
    service.cancel_by_visitor(102, 1, now)
    repeated = to_contact_form(flow, 102, now)
    assert "Использовать сохранённые" in repeated.text
    restarted = VisitorFlow(service, calendar, policy_url="https://example.org/privacy",
                            policy_version="2026-10-03")
    reply = action(restarted, 102, "guest", "reuse", now)
    assert "цель" in reply.text
    reply = action(restarted, 102, "guest", "text", now, "Повторная встреча")
    reply = action(restarted, 102, "guest", "consent:yes", now)
    assert "Повторная встреча" in reply.text
    assert "отправлена" in action(restarted, 102, "guest", "send", now).text
    with connect(service.path) as connection:
        assert connection.execute(
            "SELECT used_saved_contacts FROM bookings WHERE visitor_id = ? "
            "AND status = 'pending' ORDER BY id DESC LIMIT 1", (102,)
        ).fetchone()[0] == 1


def test_username_email_stale_button_and_hold_release(setup_flow):
    service, _, flow, now = setup_flow
    allow(service, flow, 103, now, username=None)
    assert "username" in action(flow, 103, None, "book", now).text
    reply = action(flow, 103, "now_has_name", "book", now)
    chosen = button(reply, "t:")
    action(flow, 103, "now_has_name", chosen, now)
    assert "устарела" in action(flow, 103, "now_has_name", chosen, now).text
    action(flow, 103, "now_has_name", "d:30", now)
    reply = action(flow, 103, "now_has_name", "v:telemost", now)
    reply = action(flow, 103, "now_has_name", button(reply, "day:"), now)
    action(flow, 103, "now_has_name", button(reply, "time:"), now)
    action(flow, 103, "now_has_name", "text", now, "Гость")
    assert "корректный email" in action(flow, 103, "now_has_name", "text", now,
                                         "not-an-email").text
    assert "отменено" in action(flow, 103, "now_has_name", "stop", now).text
    assert service.booking_status(1) == "expired"
    assert service.list_slots(103, now, 30, "telemost")


def test_google_busy_zoom_and_no_private_details(setup_flow):
    service, calendar, flow, now = setup_flow
    allow(service, flow, 104, now)
    reply = action(flow, 104, "guest", "book", now)
    action(flow, 104, "guest", button(reply, "t:"), now)
    reply = action(flow, 104, "guest", "d:45", now)
    assert not any("zoom" in item.action for row in reply.rows for item in row)
    assert "устарела" in action(flow, 104, "guest", "v:zoom", now).text
    reply = action(flow, 104, "guest", "v:telemost", now)
    calendar.busy = [BusyInterval(epoch(5, 15), epoch(5, 19))]
    reply = action(flow, 104, "guest", "back", now)
    reply = action(flow, 104, "guest", "v:telemost", now)
    assert "нет" in reply.text
    assert "Гость" not in reply.text and "занято" not in reply.text


def test_closed_wednesday_requires_explicit_exception(setup_flow):
    service, _, flow, now = setup_flow
    with pytest.raises(RuleError):
        service.publish_interval(900, epoch(7, 16), epoch(7, 18),
                                 "Europe/Moscow", now)
    allow(service, flow, 110, now)
    reply = action(flow, 110, "guest", "book", now)
    action(flow, 110, "guest", button(reply, "t:"), now)
    action(flow, 110, "guest", "d:30", now)
    days = action(flow, 110, "guest", "v:telemost", now)
    assert all("07.10.2026" not in item.label for row in days.rows for item in row)


def test_other_visitors_booking_inaccessible(setup_flow):
    service, _, flow, now = setup_flow
    allow(service, flow, 105, now)
    allow(service, flow, 106, now)
    to_contact_form(flow, 105, now)
    finish_form(flow, 105, now)
    action(flow, 105, "guest", "send", now)
    assert "У вас пока нет" in action(flow, 106, "guest", "mine", now).text
    reply = action(flow, 106, "guest", "cancel_yes:1", now)
    assert "Заявка не найдена" in reply.text
    assert service.booking_status(1) == "pending"


def test_changed_username_is_current_snapshot(setup_flow):
    service, _, flow, now = setup_flow
    allow(service, flow, 107, now, username="old_name")
    to_contact_form(flow, 107, now, username="new_name")
    finish_form(flow, 107, now, username="new_name")
    assert "отправлена" in action(flow, 107, "new_name", "send", now).text
    connection = service._connect()
    try:
        assert connection.execute("SELECT username_snapshot FROM bookings WHERE id = 1"
                                  ).fetchone()[0] == "new_name"
    finally:
        connection.close()


def test_slot_becomes_busy_before_click(setup_flow):
    service, calendar, flow, now = setup_flow
    allow(service, flow, 108, now)
    reply = action(flow, 108, "guest", "book", now)
    action(flow, 108, "guest", button(reply, "t:"), now)
    action(flow, 108, "guest", "d:30", now)
    reply = action(flow, 108, "guest", "v:telemost", now)
    reply = action(flow, 108, "guest", button(reply, "day:"), now)
    chosen = button(reply, "time:")
    calendar.busy = [BusyInterval(epoch(5, 15), epoch(5, 19))]
    reply = action(flow, 108, "guest", chosen, now)
    assert "уже занято" in reply.text
    connection = service._connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0
    finally:
        connection.close()


def test_expired_hold_and_safe_technical_log(setup_flow, tmp_path):
    service, _, flow, now = setup_flow
    service.logger = configure_logging(tmp_path / "technical.jsonl")
    allow(service, flow, 109, now)
    to_contact_form(flow, 109, now)
    action(flow, 109, "guest", "text", now, "Private Name")
    assert "корректный email" in action(flow, 109, "guest", "text", now,
                                         "private-invalid-email").text
    action(flow, 109, "guest", "text", now, "private@example.org")
    action(flow, 109, "guest", "phone:skip", now)
    action(flow, 109, "guest", "text", now, "Private purpose")
    action(flow, 109, "guest", "consent:yes", now)
    assert "истекло" in action(flow, 109, "guest", "send", now + 601).text
    assert service.booking_status(1) == "expired"
    log = (tmp_path / "technical.jsonl").read_text(encoding="utf-8")
    assert "visitor_input_rejected" in log and '"code":"email"' in log
    assert "visitor_dialog_step" in log and "holds_expired" in log
    for secret in ("Private Name", "private@example.org", "Private purpose",
                   "private-invalid-email"):
        assert secret not in log


def test_telegram_keyboard_callbacks_fit_protocol(setup_flow):
    service, _, flow, now = setup_flow
    allow(service, flow, 111, now)
    reply = action(flow, 111, "guest", "book", now)
    keyboard = _markup(reply)
    assert keyboard is not None
    assert all(len(button.callback_data.encode("utf-8")) <= 64
               for row in keyboard.inline_keyboard for button in row)


def test_consent_is_separate_from_submission(setup_flow):
    service, _, flow, now = setup_flow
    allow(service, flow, 112, now)
    to_contact_form(flow, 112, now)
    action(flow, 112, "guest", "text", now, "Гость")
    action(flow, 112, "guest", "text", now, "guest@example.org")
    action(flow, 112, "guest", "phone:skip", now)
    consent = action(flow, 112, "guest", "text", now, "Вопрос")
    assert "Даю согласие" in str(consent.rows)
    assert "устарела" in action(flow, 112, "guest", "send", now).text
    connection = service._connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 1
    finally:
        connection.close()
    action(flow, 112, "guest", "consent:yes", now)
    assert "отправлена" in action(flow, 112, "guest", "send", now).text


def test_access_refusal_stores_no_visitor(setup_flow):
    service, _, flow, now = setup_flow
    assert "Даю согласие" in str(action(flow, 113, "guest", "access", now).rows)
    action(flow, 113, "guest", "start", now)
    connection = service._connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM visitors WHERE telegram_id = 113"
                                  ).fetchone()[0] == 0
    finally:
        connection.close()


def test_live_bot_refuses_unverified_storage(monkeypatch):
    settings = {
        "TELEGRAM_BOT_TOKEN": "synthetic-token", "BOT_ADMIN_ID": "900",
        "POLICY_URL": "https://example.org/privacy", "POLICY_VERSION": "test",
        "GOOGLE_CLIENT_ID": "synthetic-id", "GOOGLE_CLIENT_SECRET": "synthetic-secret",
        "GOOGLE_REFRESH_TOKEN": "synthetic-refresh", "BOT_STORAGE_IN_RUSSIA": "false",
    }
    monkeypatch.setattr(telegram_app, "read_env", lambda _: settings)
    with pytest.raises(RuntimeError, match="verified database location"):
        asyncio.run(telegram_app.run())


def test_privacy_action_preserves_hold_and_returns_current_button_revision(setup_flow):
    from meeting_bot.drafts import DraftStore
    service, _, flow, now = setup_flow
    allow(service, flow, 120, now)
    to_contact_form(flow, 120, now)
    old = DraftStore(service).read(120, 120, now)
    reply = action(flow, 120, "guest", "profile:delete:yes", now)
    current = DraftStore(service).read(120, 120, now)
    assert current.values[5] == old.values[5]
    assert current.values[0] == "name"
    assert current.values[6:9] == (None, None, None)
    assert reply.draft_revision == current.revision > old.revision
    assert service.booking_status(old.values[5]) == "held"
    stale = action(flow, 120, "guest", f"r:{old.revision}:phone:skip", now)
    assert "измен" in stale.text or "устар" in stale.text
