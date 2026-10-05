import json
import multiprocessing as mp
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from meeting_bot.database import connect
from meeting_bot.rules import BusyInterval, ReviewSchedule, ReviewWindow
from meeting_bot.service import (
    AccessDenied, BookingService, RuleError, SlotUnavailable, StorageUnavailable,
)
from meeting_bot.technical_log import configure_logging
from meeting_bot.worker import claim_due_job, run_due_jobs, schedule_cleanup


ZONE = ZoneInfo("Europe/Moscow")
ADMIN = 9001
VISITOR = 101
OTHER = 102


def at(day: int, hour: int, minute: int = 0, *, month: int = 10) -> int:
    return int(datetime(2026, month, day, hour, minute, tzinfo=ZONE).timestamp())


def setup_user(service: BookingService, visitor_id: int, now: int) -> None:
    first = service.request_access(visitor_id, "old_name", now)
    assert service.request_access(visitor_id, "old_name", now) == first
    service.decide_access(ADMIN, visitor_id, "allowed", now)
    service.record_consent(visitor_id, "policy-v1", now)


def setup_booking(service: BookingService, now: int, *, day: int = 5,
                  hour: int = 16, visitor_id: int = VISITOR) -> tuple[int, int, int]:
    setup_user(service, visitor_id, now)
    template = service.create_template(ADMIN, "Консультация", "Описание", "Инструкция", now)
    interval = service.publish_interval(
        ADMIN, at(day, hour), at(day, hour + 2), "Europe/Moscow", now
    )
    booking = service.create_hold(visitor_id, template, interval, at(day, hour),
                                  30, "telemost", now)
    return template, interval, booking


def submit(service: BookingService, booking_id: int, now: int, visitor_id: int = VISITOR,
           username: str = "new_name", email: str = "private@example.test") -> int:
    return service.submit_request(
        visitor_id, booking_id, name="Имя", username=username, email=email,
        phone="+70000000000", purpose="Личная цель", policy_version="policy-v1", now=now,
    )


def hold_worker(path: str, visitor: int, template: int, interval: int,
                now: int, starts_at: int, start: mp.Event, results: mp.Queue) -> None:
    service = BookingService(Path(path), administrator_id=ADMIN)
    start.wait(10)
    try:
        booking = service.create_hold(visitor, template, interval, starts_at,
                                      30, "telemost", now)
        results.put(("created", booking))
    except SlotUnavailable:
        results.put(("conflict", None))
    except Exception as error:
        results.put(("error", type(error).__name__))


def locked_writer(path: str, ready: mp.Event, seconds: float) -> None:
    connection = connect(Path(path))
    try:
        connection.execute("BEGIN IMMEDIATE")
        ready.set()
        time.sleep(seconds)
        connection.execute("COMMIT")
    finally:
        connection.close()


def due_worker(path: str, now: int, start: mp.Event, results: mp.Queue) -> None:
    start.wait(10)
    try:
        results.put(run_due_jobs(Path(path), now))
    except Exception as error:
        results.put(type(error).__name__)


@pytest.fixture
def service(tmp_path: Path) -> BookingService:
    result = BookingService(tmp_path / "db" / "bot.sqlite", administrator_id=ADMIN)
    result.initialize()
    return result


def test_access_schedule_slots_consent_and_contact_snapshots(service: BookingService) -> None:
    now = at(5, 10)
    with pytest.raises(AccessDenied):
        service.list_slots(VISITOR, now, 30, "telemost")
    with pytest.raises(AccessDenied):
        service.create_template(OTHER, "X", "", "", now)
    with pytest.raises(RuleError):
        service.publish_interval(ADMIN, at(7, 15), at(7, 17), "Europe/Moscow", now)
    setup_user(service, VISITOR, now)
    template = service.create_template(ADMIN, "Первый", "Старое", "Инструкция", now)
    interval = service.publish_interval(ADMIN, at(5, 15), at(5, 17), "Europe/Moscow", now)
    assert at(5, 15) in service.list_slots(VISITOR, now, 30, "telemost")
    assert at(5, 15) not in service.list_slots(
        VISITOR, now, 30, "telemost", [BusyInterval(at(5, 15, 45), at(5, 16))]
    )
    booking = service.create_hold(VISITOR, template, interval, at(5, 15), 30,
                                  "telemost", now)
    with pytest.raises(SlotUnavailable):
        service.create_hold(VISITOR, template, interval, at(5, 15, 15), 30,
                            "telemost", now)
    with pytest.raises(AccessDenied):
        service.saved_contacts(OTHER, VISITOR)
    submit(service, booking, now)
    assert submit(service, booking, now) == booking
    assert service.saved_contacts(VISITOR, VISITOR).email == "private@example.test"
    service.update_template(ADMIN, template, name="Новый", description="Другое",
                            instructions="Другая инструкция", is_active=True, now=now + 1)
    service.correct_saved_contacts(VISITOR, VISITOR, name="Исправлено",
                                   email="corrected@example.test", phone=None, now=now + 2)
    service.touch_visitor(VISITOR, VISITOR, "changed_name", now + 3)
    assert service.saved_contacts(VISITOR, VISITOR).email == "corrected@example.test"
    connection = connect(service.path)
    try:
        row = connection.execute(
            "SELECT username_snapshot, email_snapshot, template_name_snapshot, "
            "template_description_snapshot, status, decision_deadline_at "
            "FROM bookings WHERE id = ?", (booking,),
        ).fetchone()
        assert row == ("new_name", "private@example.test", "Первый", "Старое", "pending", at(5, 13))
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 3
    finally:
        connection.close()
    affected = service.close_interval(ADMIN, interval, now + 4)
    assert affected == [booking]
    assert service.list_slots(VISITOR, now + 4, 30, "telemost") == []
    service.clear_saved_contacts(VISITOR, VISITOR, now + 5)
    assert service.saved_contacts(VISITOR, VISITOR) is None


def test_hold_expiry_and_request_deadline_survive_worker_restart(service: BookingService) -> None:
    now = at(5, 10)
    _, interval, held = setup_booking(service, now)
    assert run_due_jobs(service.path, now + 599) == 0
    assert run_due_jobs(service.path, now + 600) == 1
    connection = connect(service.path)
    assert connection.execute("SELECT status FROM bookings WHERE id = ?", (held,)).fetchone()[0] == "expired"
    connection.close()
    next_hold = service.create_hold(VISITOR, 1, interval, at(5, 16), 30,
                                    "telemost", now + 601)
    submit(service, next_hold, now + 601)
    due = at(5, 13, 10) + 1
    job_id = claim_due_job(service.path, due, lease_seconds=5)
    assert job_id is not None
    # Simulate a process crash after claim: the lease is recovered on restart.
    assert run_due_jobs(service.path, due + 4) == 0
    assert run_due_jobs(service.path, due + 5) == 1
    assert run_due_jobs(service.path, due + 5) == 0
    connection = connect(service.path)
    try:
        assert connection.execute(
            "SELECT status FROM bookings WHERE id = ?", (next_hold,)
        ).fetchone()[0] == "expired"
        assert connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE kind = 'request_expired'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_decisions_cancellation_and_google_boundary(service: BookingService) -> None:
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    submit(service, booking, now)
    with pytest.raises(AccessDenied):
        service.admin_decision(OTHER, booking, True, now)
    assert service.admin_decision(ADMIN, booking, True, now + 60) == "awaiting_link"
    assert service.admin_decision(ADMIN, booking, True, now + 60) == "awaiting_link"
    with pytest.raises(ValueError):
        service.mark_calendar_created(booking, "", "https://telemost.yandex.ru/j/abc", now)
    with pytest.raises(RuleError):
        service.mark_calendar_created(booking, "google-event-1", "https://telemost.yandex.ru/j/abc", now)
    assert service.begin_calendar_creation(
        booking, "https://telemost.yandex.ru/j/abc", now + 90
    ) == "creating_calendar"
    assert service.mark_calendar_created(
        booking, "google-event-1", "https://telemost.yandex.ru/j/abc", now + 120
    ) == "confirmed"
    assert service.mark_calendar_created(
        booking, "google-event-1", "https://telemost.yandex.ru/j/abc", now + 120
    ) == "confirmed"
    connection = connect(service.path)
    assert connection.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1
    connection.close()
    with pytest.raises(RuleError):
        service.cancel_by_visitor(VISITOR, booking, at(5, 15, 1))
    assert service.cancel_by_visitor(VISITOR, booking, at(5, 15)) == "attention"
    assert service.cancel_by_visitor(VISITOR, booking, at(5, 15)) == "attention"
    connection = connect(service.path)
    assert connection.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'calendar_cancel'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT status FROM bookings WHERE id = ?", (booking,)
    ).fetchone()[0] == "attention"
    connection.close()
    assert service.mark_calendar_cancelled(booking, at(5, 15, 1)) == "cancelled_by_visitor"
    assert service.mark_calendar_cancelled(booking, at(5, 15, 1)) == "cancelled_by_visitor"


def test_pending_cancel_idempotence_and_block_does_not_cancel_confirmed(
        service: BookingService) -> None:
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    submit(service, booking, now)
    assert service.cancel_by_visitor(VISITOR, booking, now + 1) == "cancelled_by_visitor"
    assert service.cancel_by_visitor(VISITOR, booking, now + 1) == "cancelled_by_visitor"
    connection = connect(service.path)
    assert connection.execute(
        "SELECT COUNT(*) FROM notifications WHERE kind = 'visitor_cancelled'"
    ).fetchone()[0] == 1
    connection.close()
    with pytest.raises(AccessDenied):
        service.cancel_by_visitor(OTHER, booking, now + 1)

    template = service.create_template(ADMIN, "Вторая", "", "", now)
    interval = service.publish_interval(ADMIN, at(6, 16), at(6, 18), "Europe/Moscow", now)
    second = service.create_hold(VISITOR, template, interval, at(6, 16), 30,
                                 "telemost", now)
    submit(service, second, now)
    service.admin_decision(ADMIN, second, True, now + 1)
    service.begin_calendar_creation(second, "https://telemost.yandex.ru/j/def", now + 1)
    service.mark_calendar_created(second, "google-event-2", "https://telemost.yandex.ru/j/def", now + 2)
    service.decide_access(ADMIN, VISITOR, "blocked", now + 3)
    connection = connect(service.path)
    assert connection.execute("SELECT status FROM bookings WHERE id = ?", (second,)).fetchone()[0] == "confirmed"
    connection.close()
    with pytest.raises(AccessDenied):
        service.create_hold(VISITOR, template, interval, at(6, 17), 30,
                            "telemost", now + 3)


def test_two_processes_and_busy_file_friendly_error(service: BookingService,
                                                    tmp_path: Path) -> None:
    now = at(5, 10)
    setup_user(service, VISITOR, now)
    setup_user(service, OTHER, now)
    template = service.create_template(ADMIN, "Встреча", "", "", now)
    interval = service.publish_interval(ADMIN, at(5, 16), at(5, 18), "Europe/Moscow", now)
    context = mp.get_context("spawn")
    for second_start in (at(5, 16), at(5, 16, 45)):
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(target=hold_worker,
                            args=(str(service.path), VISITOR, template, interval,
                                  now, at(5, 16), start, results)),
            context.Process(target=hold_worker,
                            args=(str(service.path), OTHER, template, interval,
                                  now, second_start, start, results)),
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=15)[0] for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert sorted(outcomes) == ["conflict", "created"]
        connection = connect(service.path)
        connection.execute("DELETE FROM drafts")
        connection.execute("DELETE FROM bookings")
        connection.close()

    ready = context.Event()
    lock = context.Process(target=locked_writer, args=(str(service.path), ready, 0.45))
    lock.start()
    assert ready.wait(10)
    log_path = tmp_path / "technical.jsonl"
    busy_service = BookingService(service.path, administrator_id=ADMIN,
                                  logger=configure_logging(log_path), busy_timeout_ms=50)
    with pytest.raises(StorageUnavailable, match="Попробуйте ещё раз"):
        busy_service.create_hold(VISITOR, template, interval, at(5, 16), 30,
                                 "telemost", now)
    lock.join(timeout=10)
    assert lock.exitcode == 0
    assert service.create_hold(VISITOR, template, interval, at(5, 16), 30,
                               "telemost", now) > 0
    assert "storage_unavailable" in log_path.read_text(encoding="utf-8")


def test_cleanup_two_calendar_months_keeps_active_future_meeting(service: BookingService) -> None:
    now = at(5, 10)
    old = at(1, 10, month=7)
    connection = connect(service.path)
    try:
        for visitor in (VISITOR, OTHER):
            connection.execute(
                "INSERT INTO visitors (telegram_id, access_status, created_at, last_used_at) "
                "VALUES (?, 'allowed', ?, ?)", (visitor, old, old),
            )
        connection.execute(
            "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
            "created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, 'Europe/Moscow', 'rejected', ?, ?, ?)",
            (VISITOR, old, old + 1800, old, old, old),
        )
        connection.execute(
            "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, 'Europe/Moscow', 'confirmed', ?, ?)",
            (OTHER, at(6, 16), at(6, 16, 30), old, old),
        )
    finally:
        connection.close()
    schedule_cleanup(service.path, now)
    assert run_due_jobs(service.path, now) == 1
    connection = connect(service.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM visitors WHERE telegram_id = ?", (VISITOR,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM visitors WHERE telegram_id = ?", (OTHER,)
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_safe_logs_do_not_contain_contact_or_purpose(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "technical.jsonl"
    service = BookingService(tmp_path / "db" / "bot.sqlite", administrator_id=ADMIN,
                             logger=configure_logging(log_path))
    service.initialize()
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    submit(service, booking, now)
    content = log_path.read_text(encoding="utf-8")
    assert "private@example.test" not in content
    assert "+70000000000" not in content
    assert "Личная цель" not in content
    for line in content.splitlines():
        event = json.loads(line)
        assert set(event) <= {"at", "level", "event", "code", "record_id"}


def test_late_link_invalid_contacts_and_editable_review_hours(service: BookingService) -> None:
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    with pytest.raises(RuleError):
        submit(service, booking, now, username="")
    with pytest.raises(RuleError):
        submit(service, booking, now, email="wrong-address")
    submit(service, booking, now)
    service.admin_decision(ADMIN, booking, True, now + 1)
    with pytest.raises(RuleError):
        service.begin_calendar_creation(booking, "https://telemost.yandex.ru/j/a", at(5, 13))
    assert run_due_jobs(service.path, at(5, 13)) == 1
    connection = connect(service.path)
    assert connection.execute(
        "SELECT status FROM bookings WHERE id = ?", (booking,)
    ).fetchone()[0] == "expired"
    connection.close()

    changed = ReviewSchedule("Europe/Moscow", tuple(
        ReviewWindow(day, 660, 1200) for day in range(5)
    ))
    with pytest.raises(AccessDenied):
        service.set_review_schedule(OTHER, changed, now)
    service.set_review_schedule(ADMIN, changed, now)
    assert service.review_schedule() == changed
    assert at(5, 16) in service.list_slots(VISITOR, now, 30, "telemost")


def test_reminders_and_duplicate_worker_claims(service: BookingService) -> None:
    now = at(5, 10)
    _, _, booking = setup_booking(service, now, day=8)
    submit(service, booking, now)
    service.admin_decision(ADMIN, booking, True, now + 1)
    link = "https://telemost.yandex.ru/j/reminder"
    service.begin_calendar_creation(booking, link, now + 2)
    service.mark_calendar_created(booking, "google-event-reminder", link, now + 3)
    context = mp.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    due = at(7, 16)
    workers = [context.Process(target=due_worker,
                               args=(str(service.path), due, start, results)) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0
    assert sorted(outcomes) == [0, 1]
    assert run_due_jobs(service.path, at(8, 14)) == 1
    connection = connect(service.path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM notifications WHERE kind IN ('reminder_24h', 'reminder_2h')"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_admin_cancellation_waits_for_calendar_and_disk_error(tmp_path: Path) -> None:
    service = BookingService(tmp_path / "db" / "bot.sqlite", administrator_id=ADMIN)
    service.initialize()
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    submit(service, booking, now)
    service.admin_decision(ADMIN, booking, True, now + 1)
    link = "https://telemost.yandex.ru/j/admin"
    service.begin_calendar_creation(booking, link, now + 2)
    service.mark_calendar_created(booking, "google-event-admin", link, now + 3)
    assert service.cancel_by_admin(ADMIN, booking, now + 4) == "attention"
    connection = connect(service.path)
    assert connection.execute(
        "SELECT status FROM bookings WHERE id = ?", (booking,)
    ).fetchone()[0] == "attention"
    connection.close()
    assert service.mark_calendar_cancelled(booking, now + 5) == "cancelled_by_admin"

    bad_parent = tmp_path / "not_a_directory"
    bad_parent.write_text("file", encoding="utf-8")
    log_path = tmp_path / "disk_error.jsonl"
    broken = BookingService(bad_parent / "bot.sqlite", administrator_id=ADMIN,
                            logger=configure_logging(log_path))
    with pytest.raises(StorageUnavailable, match="Попробуйте ещё раз"):
        broken.create_hold(VISITOR, 1, 1, at(5, 16), 30, "telemost", now)
    assert "storage_unavailable" in log_path.read_text(encoding="utf-8")


def test_profile_expires_while_recent_completed_booking_snapshot_remains(
        service: BookingService) -> None:
    now = at(5, 10)
    old = at(1, 10, month=7)
    recent = at(20, 10, month=9)
    connection = connect(service.path)
    try:
        connection.execute(
            "INSERT INTO visitors (telegram_id, email, access_status, created_at, last_used_at) "
            "VALUES (?, 'private@example.test', 'allowed', ?, ?)",
            (VISITOR, old, old),
        )
        connection.execute(
            "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
            "email_snapshot, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, 'Europe/Moscow', 'rejected', 'private@example.test', ?, ?, ?)",
            (VISITOR, recent, recent + 1800, old, recent, recent),
        )
    finally:
        connection.close()
    schedule_cleanup(service.path, now)
    assert run_due_jobs(service.path, now) == 1
    connection = connect(service.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM visitors").fetchone()[0] == 0
        assert connection.execute(
            "SELECT visitor_id, email_snapshot FROM bookings"
        ).fetchone() == (None, "private@example.test")
    finally:
        connection.close()


def test_manual_closed_day_exception_and_withdrawn_consent(service: BookingService) -> None:
    now = at(5, 10)
    setup_user(service, VISITOR, now)
    template = service.create_template(ADMIN, "Исключение", "", "", now)
    interval = service.publish_interval(
        ADMIN, at(7, 16), at(7, 18), "Europe/Moscow", now,
        manual_exception=True,
    )
    assert at(7, 16) in service.list_slots(VISITOR, now, 30, "telemost")
    booking = service.create_hold(VISITOR, template, interval, at(7, 16),
                                  30, "telemost", now)
    service.withdraw_consent(VISITOR, VISITOR, "policy-v1", now + 1)
    with pytest.raises(RuleError, match="согласие"):
        submit(service, booking, now + 2)
    service.record_consent(VISITOR, "policy-v1", now + 3)
    assert submit(service, booking, now + 4) == booking


def test_contact_deletion_scrubs_live_draft_without_cancelling_hold(
        service: BookingService) -> None:
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    connection = connect(service.path)
    connection.execute(
        "UPDATE drafts SET contact_name = 'Secret', contact_email = 'secret@example.test', "
        "contact_phone = '+70000000000' WHERE booking_id = ?", (booking,)
    )
    connection.close()
    service.clear_saved_contacts(VISITOR, VISITOR, now + 1)
    connection = connect(service.path)
    try:
        assert connection.execute(
            "SELECT contact_name, contact_email, contact_phone FROM drafts WHERE booking_id = ?",
            (booking,),
        ).fetchone() == (None, None, None)
        assert connection.execute(
            "SELECT status FROM bookings WHERE id = ?", (booking,)
        ).fetchone()[0] == "held"
    finally:
        connection.close()


def test_close_whole_day_preserves_confirmed_meeting_and_status_history(
        service: BookingService) -> None:
    now = at(5, 10)
    _, _, booking = setup_booking(service, now)
    submit(service, booking, now)
    service.admin_decision(ADMIN, booking, True, now + 1)
    link = "https://telemost.yandex.ru/j/history"
    service.begin_calendar_creation(booking, link, now + 2)
    service.mark_calendar_created(booking, "google-event-history", link, now + 3)
    affected = service.close_day(ADMIN, date(2026, 10, 5), "Europe/Moscow", now + 4)
    assert affected == [booking]
    connection = connect(service.path)
    try:
        assert connection.execute(
            "SELECT status FROM bookings WHERE id = ?", (booking,)
        ).fetchone()[0] == "confirmed"
        actions = [row[0] for row in connection.execute(
            "SELECT action FROM audit_actions WHERE booking_id = ? ORDER BY id", (booking,)
        )]
        assert actions == [
            "hold_created", "request_submitted", "awaiting_link",
            "calendar_creation_started", "meeting_confirmed",
        ]
    finally:
        connection.close()
    assert service.list_slots(VISITOR, now + 4, 30, "telemost") == []
