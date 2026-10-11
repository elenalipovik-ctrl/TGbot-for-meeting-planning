import sqlite3

import pytest
from fastapi.testclient import TestClient

import meeting_bot.database as database
from meeting_bot.api import production
from meeting_bot.database import connect, initialize_database
from meeting_bot.release_database import migrate, require_schema, restore


def settings():
    return dict(TELEGRAM_BOT_TOKEN="123456:" + "x" * 30, BOT_ADMIN_ID="101",
                POLICY_URL="https://meetcalendar.fin-partner.pro/privacy/", POLICY_VERSION="v1",
                GOOGLE_CLIENT_ID="test", GOOGLE_CLIENT_SECRET="test", GOOGLE_REFRESH_TOKEN="test",
                BOT_STORAGE_IN_RUSSIA="true", BOT_LOCAL_TEST_MODE="false", BOT_SERVER_TEST_MODE="false",
                BOT_DB_PATH="data/bot.sqlite", MINI_APP_MODE="production", MINI_APP_ENABLED="true",
                MINI_APP_ORIGIN=production.ORIGIN, MINI_APP_RESTRICTED="false")


def test_production_audience_matches_existing_bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    values = settings()
    assert production.production_config(values).allowed_user_ids is None
    values.update(MINI_APP_RESTRICTED="true", BOT_SERVER_TEST_VISITOR_ID="102")
    assert production.production_config(values).allowed_user_ids == frozenset((101, 102))
    assert values["BOT_SERVER_TEST_MODE"] == "false"  # existing bot stays public
    values.update(BOT_SERVER_TEST_MODE="true", BOT_SERVER_TEST_VISITOR_ID="102")
    assert production.production_config(values).allowed_user_ids == frozenset((101, 102))
    values["BOT_SERVER_TEST_VISITOR_ID"] = "101"
    with pytest.raises(RuntimeError):
        production.production_config(values)


@pytest.mark.parametrize("key,value", [
    ("MINI_APP_MODE", "owner-preview"), ("MINI_APP_ORIGIN", "https://example.com"),
    ("MINI_APP_ENABLED", ""), ("MINI_APP_RESTRICTED", ""), ("BOT_LOCAL_TEST_MODE", "true"),
    ("BOT_STORAGE_IN_RUSSIA", "false"), ("BOT_DB_PATH", "data/synthetic.sqlite"),
    ("BOT_ADMIN_ID", "0"), ("GOOGLE_REFRESH_TOKEN", ""),
])
def test_production_rejects_incomplete_or_preview_settings(tmp_path, monkeypatch, key, value):
    monkeypatch.chdir(tmp_path)
    values = settings()
    values[key] = value
    with pytest.raises((ValueError, RuntimeError)):
        production.production_config(values)


def test_factory_uses_shared_calendar_no_migration_and_health_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "data" / "bot.sqlite"
    initialize_database(path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<html>Release</html>", encoding="utf-8")
    values = settings()
    values["BOT_CALENDAR_ID"] = "chosen-calendar"
    monkeypatch.setattr(production, "read_env", lambda _: values)
    calendar = object()
    calls = []
    def calendar_factory(*args):
        calls.append(args)
        return calendar
    monkeypatch.setattr(production, "calendar_from_env", calendar_factory)
    monkeypatch.setattr(production.BookingService, "initialize", lambda _: pytest.fail("No auto migration"))
    app = production.build_app(env_file=tmp_path / ".env", assets=assets)
    assert calls == [(tmp_path / ".env", "chosen-calendar", None)]
    with TestClient(app) as client:
        assert client.get("/app/").status_code == 200
        assert client.get("/health/ready").json() == {"ready": True}
        assert client.get("/api/v1/bootstrap").status_code == 401
        with sqlite3.connect(path) as c:
            c.execute("PRAGMA user_version=8")
        assert client.get("/health/ready").status_code == 503
    with pytest.raises(RuntimeError):
        production.build_app(env_file=tmp_path / ".env", assets=assets)
    assert len(calls) == 1


def test_offline_migration_preserves_booking_and_backup_and_restores(tmp_path, monkeypatch):
    path, backup = tmp_path / "bot.sqlite", tmp_path / "v8.sqlite"
    monkeypatch.setattr(database, "SCHEMA_VERSION", 8)
    initialize_database(path)
    with connect(path) as c:
        c.execute("INSERT INTO visitors(telegram_id,created_at,last_used_at) VALUES (101,1,1)")
        c.execute("INSERT INTO bookings(visitor_id,starts_at,ends_at,time_zone,status,contact_name_snapshot,created_at,updated_at) VALUES (101,2000000000,2000001800,'Europe/Moscow','confirmed','Immutable',1,1)")
    monkeypatch.setattr(database, "SCHEMA_VERSION", 11)
    with pytest.raises(ValueError):
        migrate(path, backup)
    assert not backup.exists()
    migrate(path, backup, maintenance_confirmed=True)
    require_schema(path, 11)
    require_schema(backup, 8)
    with connect(path) as c:
        assert c.execute("SELECT contact_name_snapshot,status FROM bookings").fetchone() == ("Immutable", "confirmed")
        assert c.execute("SELECT count(*) FROM booking_start_options").fetchone() == (0,)
    with pytest.raises(ValueError):
        restore(path, backup)
    restore(path, backup, maintenance_confirmed=True)
    require_schema(path, 8)
    with pytest.raises(FileExistsError):
        migrate(path, backup, maintenance_confirmed=True)
    require_schema(path, 8)


def test_rehearsal_failure_does_not_touch_source(tmp_path, monkeypatch):
    path, backup = tmp_path / "bot.sqlite", tmp_path / "backup.sqlite"
    monkeypatch.setattr(database, "SCHEMA_VERSION", 8)
    initialize_database(path)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 11)
    original = database._statements
    def broken(source):
        yield from original(source)
        yield "INSERT INTO absent_table VALUES(1);"
    monkeypatch.setattr(database, "_statements", broken)
    with pytest.raises(sqlite3.OperationalError):
        migrate(path, backup, maintenance_confirmed=True)
    require_schema(path, 8)
    require_schema(backup, 8)
