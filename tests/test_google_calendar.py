import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from google.auth.exceptions import RefreshError

from meeting_bot.calendar_flow import (
    confirm_with_google, finish_calendar_cancellation, submit_with_google,
)
from meeting_bot.database import connect
from meeting_bot.google_calendar import (
    CalendarAuthExpired, CalendarPermissionDenied, CalendarUncertain, GoogleCalendar,
)
from meeting_bot.service import BookingService, SlotUnavailable
from meeting_bot.technical_log import configure_logging
from meeting_bot.worker import run_due_jobs


ADMIN = 9001
VISITOR = 101
ZONE = ZoneInfo("Europe/Moscow")
LINK = "https://meeting.example.test/private-link"
EMAIL = "private@example.test"


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp())


class Response:
    def __init__(self, status_code: int, data=None):
        self.status_code = status_code
        self.data = data

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.events = {}
        self.busy = []
        self.mode = None
        self.calls = []
        self.inserts = 0

    def request(self, method, url, *, timeout, **kwargs):
        assert timeout == 15
        self.calls.append((method, url, kwargs))
        if url.endswith("/freeBusy"):
            if self.mode == "denied":
                return Response(403)
            if self.mode == "expired":
                return Response(401)
            if self.mode == "freebusy_timeout":
                raise TimeoutError("token=SECRET private event details")
            if self.mode == "refresh_error":
                raise RefreshError("token=SECRET private event details")
            if self.mode == "primary_resolved":
                return Response(200, {"calendars": {"owner@example.test": {"busy": self.busy}}})
            return Response(200, {"calendars": {"primary": {"busy": self.busy}}})
        if method == "GET":
            event_id = url.rsplit("/", 1)[-1]
            return Response(200, self.events[event_id]) if event_id in self.events else Response(404)
        if method == "POST":
            if self.mode == "insert_denied":
                return Response(403)
            body = kwargs["json"]
            assert kwargs["params"] == {"sendUpdates": "all"}
            self.inserts += 1
            self.events[body["id"]] = dict(body)
            if self.mode == "insert_timeout_after_commit":
                raise TimeoutError("token=SECRET private event details")
            if self.mode == "invite_missing":
                self.events[body["id"]]["attendees"] = []
            return Response(200, self.events[body["id"]])
        if method == "DELETE":
            assert kwargs["params"] == {"sendUpdates": "all"}
            event_id = url.rsplit("/", 1)[-1]
            if self.mode == "delete_denied":
                return Response(403)
            if event_id not in self.events:
                return Response(404)
            if self.mode == "delete_tombstone_timeout_after_commit":
                self.events[event_id]["status"] = "cancelled"
                raise TimeoutError("token=SECRET private event details")
            del self.events[event_id]
            if self.mode == "delete_timeout_after_commit":
                raise TimeoutError("token=SECRET private event details")
            return Response(204)
        raise AssertionError((method, url))


@pytest.fixture
def prepared(tmp_path: Path):
    logger = configure_logging(tmp_path / "technical.log")
    service = BookingService(tmp_path / "db.sqlite", administrator_id=ADMIN, logger=logger)
    service.initialize()
    now = at(5, 10)
    service.request_access(VISITOR, "visitor", now)
    service.decide_access(ADMIN, VISITOR, "allowed", now)
    service.record_consent(VISITOR, "policy-v1", now)
    template = service.create_template(ADMIN, "Consultation", "", "", now)
    interval = service.publish_interval(ADMIN, at(5, 16), at(5, 18),
                                        "Europe/Moscow", now)
    booking_id = service.create_hold(VISITOR, template, interval, at(5, 16),
                                     30, "telemost", now)
    fake = FakeSession()
    calendar = GoogleCalendar(fake, "primary", logger=logger)
    return service, calendar, fake, booking_id, now, tmp_path / "technical.log"


def submit(service, calendar, booking_id, now):
    return submit_with_google(
        service, calendar, VISITOR, booking_id, name="Private name", username="visitor",
        email=EMAIL, phone="+70000000000", purpose="Private purpose",
        policy_version="policy-v1", now=now,
    )


def ready(service, calendar, booking_id, now):
    submit(service, calendar, booking_id, now)
    assert service.admin_decision(ADMIN, booking_id, True, now + 1) == "awaiting_link"


def status(service, booking_id):
    connection = connect(service.path)
    try:
        return connection.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()[0]
    finally:
        connection.close()


def test_freebusy_before_submission_and_access_failures(prepared):
    service, calendar, fake, booking_id, now, log_path = prepared
    fake.busy = [{"start": datetime.fromtimestamp(at(5, 15, 45), ZONE).isoformat(),
                  "end": datetime.fromtimestamp(at(5, 16), ZONE).isoformat()}]
    with pytest.raises(SlotUnavailable):
        submit(service, calendar, booking_id, now)
    assert status(service, booking_id) == "held"
    fake.busy = []
    fake.mode = "denied"
    with pytest.raises(CalendarPermissionDenied):
        submit(service, calendar, booking_id, now)
    fake.mode = "expired"
    with pytest.raises(CalendarAuthExpired):
        submit(service, calendar, booking_id, now)
    fake.mode = "refresh_error"
    with pytest.raises(CalendarAuthExpired):
        submit(service, calendar, booking_id, now)
    fake.mode = "freebusy_timeout"
    with pytest.raises(Exception):
        submit(service, calendar, booking_id, now)
    assert status(service, booking_id) == "held"
    fake.mode = None
    assert submit(service, calendar, booking_id, now) == booking_id
    assert "SECRET" not in log_path.read_text(encoding="utf-8")


def test_primary_alias_response_is_accepted(prepared):
    service, calendar, fake, booking_id, now, _ = prepared
    fake.mode = "primary_resolved"
    assert submit(service, calendar, booking_id, now) == booking_id


def test_create_reconcile_retry_cancel_and_safe_logs(prepared):
    service, calendar, fake, booking_id, now, log_path = prepared
    ready(service, calendar, booking_id, now)
    fake.mode = "insert_timeout_after_commit"
    assert confirm_with_google(service, calendar, booking_id, LINK, now + 2) == "confirmed"
    assert fake.inserts == 1
    event = next(iter(fake.events.values()))
    assert event["attendees"] == [{"email": EMAIL}]
    assert event["description"] == LINK
    assert "Private name" not in json.dumps(event)
    assert "Private purpose" not in json.dumps(event)
    assert "+70000000000" not in json.dumps(event)
    assert status(service, booking_id) == "confirmed"
    fake.mode = None
    assert confirm_with_google(service, calendar, booking_id, LINK, now + 2) == "confirmed"
    assert fake.inserts == 1
    event_id = service.calendar_booking(booking_id).google_event_id
    assert calendar.create_or_find(event_id=event_id, booking_id=booking_id,
                                   starts_at=at(5, 16), ends_at=at(5, 16, 30),
                                   email=EMAIL, video_url=LINK) == event_id
    assert fake.inserts == 1
    assert service.cancel_by_visitor(VISITOR, booking_id, now + 3) == "attention"
    fake.mode = "delete_timeout_after_commit"
    assert finish_calendar_cancellation(service, calendar, booking_id, now + 4) == "cancelled_by_visitor"
    assert not fake.events
    text = log_path.read_text(encoding="utf-8")
    for secret in (EMAIL, LINK, "Private name", "Private purpose", "SECRET", "+70000000000"):
        assert secret not in text
    assert "google_event_reconciled" in text
    assert "google_event_absent" in text


@pytest.mark.parametrize("mode,exception", [
    ("insert_denied", CalendarPermissionDenied),
    ("invite_missing", CalendarUncertain),
])
def test_no_false_confirmation_on_create_error(prepared, mode, exception):
    service, calendar, fake, booking_id, now, _ = prepared
    ready(service, calendar, booking_id, now)
    fake.mode = mode
    with pytest.raises(exception):
        confirm_with_google(service, calendar, booking_id, LINK, now + 2)
    assert status(service, booking_id) == "attention"
    connection = connect(service.path)
    try:
        kinds = {row[0] for row in connection.execute(
            "SELECT kind FROM notifications WHERE booking_id = ?", (booking_id,))}
        assert "meeting_confirmed" not in kinds
        assert "calendar_attention" in kinds
    finally:
        connection.close()
    assert len(fake.events) <= 1


def test_busy_before_confirmation_and_retry_after_attention(prepared):
    service, calendar, fake, booking_id, now, _ = prepared
    ready(service, calendar, booking_id, now)
    fake.busy = [{"start": datetime.fromtimestamp(at(5, 15, 45), ZONE).isoformat(),
                  "end": datetime.fromtimestamp(at(5, 16), ZONE).isoformat()}]
    with pytest.raises(SlotUnavailable):
        confirm_with_google(service, calendar, booking_id, LINK, now + 2)
    assert status(service, booking_id) == "attention"
    assert not fake.events
    reserved = service.calendar_booking(booking_id).google_event_id
    fake.busy = []
    assert confirm_with_google(service, calendar, booking_id, LINK, now + 3) == "confirmed"
    assert service.calendar_booking(booking_id).google_event_id == reserved
    assert fake.inserts == 1


def test_expired_google_access_does_not_confirm_and_can_resume(prepared):
    service, calendar, fake, booking_id, now, log_path = prepared
    ready(service, calendar, booking_id, now)
    fake.mode = "expired"
    with pytest.raises(CalendarAuthExpired):
        confirm_with_google(service, calendar, booking_id, LINK, now + 2)
    assert status(service, booking_id) == "attention"
    assert not fake.events
    with connect(service.path) as connection:
        kinds = {row[0] for row in connection.execute(
            "SELECT kind FROM notifications WHERE booking_id = ?", (booking_id,))}
    assert "meeting_confirmed" not in kinds
    assert "calendar_attention" in kinds
    assert "SECRET" not in log_path.read_text(encoding="utf-8")
    fake.mode = None
    assert confirm_with_google(service, calendar, booking_id, LINK, now + 3) == "confirmed"
    assert fake.inserts == 1


def test_cancellation_failure_remains_attention(prepared):
    service, calendar, fake, booking_id, now, _ = prepared
    ready(service, calendar, booking_id, now)
    confirm_with_google(service, calendar, booking_id, LINK, now + 2)
    service.cancel_by_admin(ADMIN, booking_id, now + 3)
    fake.mode = "delete_denied"
    with pytest.raises(CalendarUncertain):
        finish_calendar_cancellation(service, calendar, booking_id, now + 4)
    assert status(service, booking_id) == "attention"
    assert fake.events
    fake.mode = None
    assert finish_calendar_cancellation(service, calendar, booking_id, now + 5) == "cancelled_by_admin"


def test_cancelled_google_tombstone_recovers_after_lost_delete_reply(prepared):
    service, calendar, fake, booking_id, now, log_path = prepared
    ready(service, calendar, booking_id, now)
    confirm_with_google(service, calendar, booking_id, LINK, now + 2)
    booking = service.calendar_booking(booking_id)
    assert booking.google_event_id is not None
    service.cancel_by_visitor(VISITOR, booking_id, now + 3)
    fake.mode = "delete_tombstone_timeout_after_commit"
    assert finish_calendar_cancellation(service, calendar, booking_id, now + 4) == "cancelled_by_visitor"
    assert fake.events[booking.google_event_id]["status"] == "cancelled"
    assert not calendar.find_matching(event_id=booking.google_event_id,
                                      booking_id=booking_id,
                                      starts_at=booking.starts_at,
                                      ends_at=booking.ends_at,
                                      email=booking.email)
    assert "SECRET" not in log_path.read_text(encoding="utf-8")


def test_worker_retries_calendar_cancellation_after_restart(prepared):
    service, calendar, fake, booking_id, now, _ = prepared
    ready(service, calendar, booking_id, now)
    confirm_with_google(service, calendar, booking_id, LINK, now + 2)
    service.cancel_by_visitor(VISITOR, booking_id, now + 3)
    fake.mode = "delete_denied"
    assert run_due_jobs(service.path, now + 4, calendar=calendar,
                        administrator_id=ADMIN) == 0
    assert status(service, booking_id) == "attention"
    connection = connect(service.path)
    try:
        retry = connection.execute(
            "SELECT status, run_at FROM jobs WHERE booking_id = ? AND kind = 'calendar_cancel'",
            (booking_id,),
        ).fetchone()
        assert retry[0] == "queued" and retry[1] > now + 4
    finally:
        connection.close()
    fake.mode = None
    assert run_due_jobs(service.path, retry[1], calendar=calendar,
                        administrator_id=ADMIN) >= 1
    assert status(service, booking_id) == "cancelled_by_visitor"
    assert not fake.events
