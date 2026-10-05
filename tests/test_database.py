import json
import multiprocessing as mp
import sqlite3
import time
from importlib.resources import files
from pathlib import Path

import pytest

import meeting_bot.database as database_module
from meeting_bot.database import SlotConflictError, connect, initialize_database, reserve_slot
from meeting_bot.technical_log import configure_logging


START = 2_000_000_000


def add_visitor(path: Path, visitor_id: int) -> None:
    connection = connect(path)
    now = int(time.time())
    try:
        connection.execute(
            "INSERT INTO visitors (telegram_id, created_at, last_used_at) VALUES (?, ?, ?)",
            (visitor_id, now, now),
        )
    finally:
        connection.close()


def reserve_worker(path: str, visitor_id: int, start: mp.Event, results: mp.Queue,
                   starts_at: int = START) -> None:
    start.wait(10)
    try:
        reservation = reserve_slot(
            Path(path), visitor_id=visitor_id, starts_at=starts_at,
            ends_at=starts_at + 1800, time_zone="Europe/Moscow",
            hold_expires_at=START - 1,
        )
        results.put(("created", reservation))
    except SlotConflictError:
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


def read_after_restart(path: str, results: mp.Queue) -> None:
    connection = connect(Path(path))
    try:
        row = connection.execute("SELECT COUNT(*) FROM visitors").fetchone()
        results.put(row[0])
    finally:
        connection.close()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "persistent" / "bot.sqlite"
    initialize_database(path)
    return path


def test_schema_migration_integrity_and_restart(database: Path) -> None:
    initialize_database(database)
    connection = connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "visitors", "consents", "access_requests", "meeting_templates",
            "availability_intervals", "drafts", "bookings", "meetings",
            "notifications", "jobs", "audit_actions", "app_settings", "review_windows",
        } <= tables
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO consents (visitor_id, policy_version, accepted_at, action) "
                "VALUES (999, 'v1', 1, 'accepted')"
            )
    finally:
        connection.close()

    add_visitor(database, 1001)
    context = mp.get_context("spawn")
    results = context.Queue()
    process = context.Process(target=read_after_restart, args=(str(database), results))
    process.start()
    assert results.get(timeout=10) == 1
    process.join(timeout=10)
    assert process.exitcode == 0

    connection = connect(database)
    try:
        connection.execute("DELETE FROM visitors WHERE telegram_id = 1001")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_gap_repeat_and_database_triggers(database: Path) -> None:
    add_visitor(database, 1001)
    add_visitor(database, 1002)
    first = reserve_slot(
        database, visitor_id=1001, starts_at=START, ends_at=START + 1800,
        time_zone="Europe/Moscow", hold_expires_at=START - 1,
    )
    for offset in (0, 1800, 3599):
        with pytest.raises(SlotConflictError):
            reserve_slot(
                database, visitor_id=1002, starts_at=START + offset,
                ends_at=START + offset + 1800,
                time_zone="Europe/Moscow", hold_expires_at=START - 1,
            )
    second = reserve_slot(
        database, visitor_id=1002, starts_at=START + 3600,
        ends_at=START + 5400, time_zone="Europe/Moscow",
        hold_expires_at=START - 1,
    )
    assert first != second

    connection = connect(database)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="slot_conflict"):
            connection.execute(
                "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
                "created_at, updated_at) VALUES (1002, ?, ?, 'Europe/Moscow', 'held', 1, 1)",
                (START + 10, START + 1810),
            )
        connection.execute(
            "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
            "created_at, updated_at) VALUES (1002, ?, ?, 'Europe/Moscow', 'expired', 1, 1)",
            (START + 10, START + 1810),
        )
        expired_id = connection.execute("SELECT MAX(id) FROM bookings").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="slot_conflict"):
            connection.execute("UPDATE bookings SET status = 'held' WHERE id = ?", (expired_id,))
        connection.execute("DELETE FROM visitors WHERE telegram_id = 1001")
        assert connection.execute(
            "SELECT visitor_id FROM bookings WHERE id = ?", (first,)
        ).fetchone()[0] is None
    finally:
        connection.close()


def test_two_processes_cannot_book_same_or_neighboring_time(database: Path) -> None:
    add_visitor(database, 1001)
    add_visitor(database, 1002)
    context = mp.get_context("spawn")
    for second_start in (START, START + 3599):
        connection = connect(database)
        connection.execute("DELETE FROM bookings")
        connection.close()
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(target=reserve_worker, args=(str(database), 1001, start, results)),
            context.Process(target=reserve_worker,
                            args=(str(database), 1002, start, results, second_start)),
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [results.get(timeout=15)[0] for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert sorted(outcomes) == ["conflict", "created"]
        connection = connect(database)
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
        connection.close()


def test_busy_file_waits_then_succeeds_and_short_timeout_is_logged(database: Path,
                                                                   tmp_path: Path) -> None:
    add_visitor(database, 1001)
    context = mp.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=locked_writer, args=(str(database), ready, 0.55))
    process.start()
    assert ready.wait(10)

    logger = configure_logging(tmp_path / "logs" / "technical.jsonl")
    with pytest.raises(sqlite3.OperationalError):
        reserve_slot(
            database, visitor_id=1001, starts_at=START, ends_at=START + 1800,
            time_zone="Europe/Moscow", hold_expires_at=START - 1,
            busy_timeout_ms=50, logger=logger,
        )
    began = time.monotonic()
    reservation = reserve_slot(
        database, visitor_id=1001, starts_at=START, ends_at=START + 1800,
        time_zone="Europe/Moscow", hold_expires_at=START - 1,
        busy_timeout_ms=3000, logger=logger,
    )
    elapsed = time.monotonic() - began
    process.join(timeout=10)
    assert process.exitcode == 0
    assert reservation > 0
    assert elapsed >= 0.1
    events = [json.loads(line) for line in (tmp_path / "logs" / "technical.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert {"database_opened", "reservation_error", "reservation_created"} <= {
        event["event"] for event in events
    }
    assert any(event.get("code") == "OperationalError" for event in events)


def test_safe_success_conflict_and_open_error_logs(database: Path, tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "technical.jsonl"
    logger = configure_logging(log_path)
    initialize_database(database, logger=logger)
    add_visitor(database, 1001)
    connection = connect(database)
    connection.execute(
        "UPDATE visitors SET email = 'private@example.test', phone = '+70000000000' "
        "WHERE telegram_id = 1001"
    )
    connection.close()
    reserve_slot(database, visitor_id=1001, starts_at=START,
                 ends_at=START + 1800, time_zone="Europe/Moscow",
                 hold_expires_at=START - 1, logger=logger)
    with pytest.raises(SlotConflictError):
        reserve_slot(database, visitor_id=1001, starts_at=START,
                     ends_at=START + 1800, time_zone="Europe/Moscow",
                     hold_expires_at=START - 1, logger=logger)
    bad_parent = tmp_path / "not_a_directory"
    bad_parent.write_text("file", encoding="utf-8")
    with pytest.raises(sqlite3.OperationalError):
        connect(bad_parent / "bot.sqlite", logger=logger)
    content = log_path.read_text(encoding="utf-8")
    events = {json.loads(line)["event"] for line in content.splitlines()}
    assert {"schema_ready", "reservation_created", "reservation_conflict", "database_error"} <= events
    assert "private@example.test" not in content
    assert "+70000000000" not in content
    assert "not_a_directory" not in content


def test_upgrade_from_stage_one_preserves_booking_and_meeting(tmp_path: Path) -> None:
    path = tmp_path / "previous.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    source = files("meeting_bot").joinpath("migrations/001_initial.sql").read_text(
        encoding="utf-8")
    connection.executescript(source)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO visitors (telegram_id, created_at, last_used_at) VALUES (1001, 1, 1)"
    )
    connection.execute(
        "INSERT INTO bookings (visitor_id, starts_at, ends_at, time_zone, status, "
        "created_at, updated_at) VALUES (1001, ?, ?, 'Europe/Moscow', 'confirmed', 1, 1)",
        (START, START + 1800),
    )
    connection.execute(
        "INSERT INTO meetings (booking_id, google_event_id, sync_status) "
        "VALUES (1, 'old-google-event', 'synced')"
    )
    connection.commit()
    connection.close()

    initialize_database(path)
    initialize_database(path)
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT google_event_id FROM meetings WHERE booking_id = 1"
        ).fetchone()[0] == "old-google-event"
        connection.execute("DELETE FROM visitors WHERE telegram_id = 1001")
        assert connection.execute("SELECT visitor_id FROM bookings WHERE id = 1").fetchone()[0] is None
    finally:
        connection.close()


def test_upgrade_from_rules_schema_adds_cancel_actor(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "stage2_v2.sqlite"
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 2)
    initialize_database(path)
    connection = connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    connection.close()
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 3)
    initialize_database(path)
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        columns = {row[1] for row in connection.execute("PRAGMA table_info(bookings)")}
        assert "cancel_requested_by" in columns
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_upgrade_accepts_early_local_v2_with_cancel_actor(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "early_v2.sqlite"
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 2)
    initialize_database(path)
    connection = connect(path)
    connection.execute("ALTER TABLE bookings ADD COLUMN cancel_requested_by INTEGER")
    connection.close()
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 3)
    initialize_database(path)
    connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_upgrade_removes_old_email_queue_and_rejects_new_email_notices(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "telegram_only.sqlite"
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 7)
    initialize_database(path)
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO notifications (channel, kind, status, dedup_key) "
            "VALUES ('email', 'admin_new_request', 'queued', 'old:email')")
        connection.execute(
            "INSERT INTO notifications (channel, kind, status, dedup_key) "
            "VALUES ('telegram', 'admin_new_request', 'queued', 'old:telegram')")
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 8)
    initialize_database(path)
    with connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8
        assert connection.execute("SELECT channel FROM notifications").fetchall() == [
            ("telegram",)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO notifications (channel, kind, status, dedup_key) "
                "VALUES ('email', 'admin_new_request', 'queued', 'new:email')")
