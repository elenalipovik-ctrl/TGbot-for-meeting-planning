import sqlite3

import pytest

import meeting_bot.database as database
from meeting_bot.database import connect, initialize_database, reserve_slot


def test_v8_duplicate_holds_mapping_and_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "v8.sqlite"
    monkeypatch.setattr(database, "SCHEMA_VERSION", 8)
    initialize_database(path)
    with connect(path) as c:
        for visitor in (101, 102, 103):
            c.execute("INSERT INTO visitors(telegram_id,created_at,last_used_at) VALUES (?,1,1)", (visitor,))
    held = []
    for visitor, start in [(101, 2000000000), (101, 2000007200), (102, 2000014400)]:
        held.append(reserve_slot(path, visitor_id=visitor, starts_at=start,
                    ends_at=start+1800, time_zone="Europe/Moscow", hold_expires_at=1999999999))
    with connect(path) as c:
        for booking, visitor in zip(held, (101, 101, 102)):
            c.execute("INSERT INTO drafts(visitor_id,step,booking_id,contact_name,expires_at,created_at,updated_at) "
                      "VALUES (?,'contacts',?,'Legacy',1999999999,1,1)", (visitor, booking))
        c.execute("INSERT INTO visitor_dialogs(visitor_id,step,booking_id,contact_name,expires_at,updated_at) "
                  "VALUES (101,'name',?,'Canonical',1999999999,1)", (held[0],))
        c.execute("INSERT INTO visitor_dialogs(visitor_id,step,contact_name,expires_at,updated_at) "
                  "VALUES (102,'name','Different hold',1999999999,1)")
        c.execute("INSERT INTO bookings(visitor_id,starts_at,ends_at,time_zone,status,contact_name_snapshot,created_at,updated_at) "
                  "VALUES (103,2000100000,2000101800,'Europe/Moscow','confirmed','Immutable',1,1)")
    monkeypatch.setattr(database, "SCHEMA_VERSION", 9)
    initialize_database(path)
    initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 9
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT id FROM bookings WHERE status='held' ORDER BY id").fetchall() == [(held[0],), (held[2],)]
        assert c.execute("SELECT booking_id,contact_name FROM visitor_dialogs ORDER BY visitor_id").fetchall() == [(held[0], "Canonical"), (held[2], "Legacy")]
        assert c.execute("SELECT contact_name FROM drafts").fetchall() == [(None,), (None,)]
        assert c.execute("SELECT contact_name_snapshot FROM bookings WHERE status='confirmed'").fetchone() == ("Immutable",)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO bookings(visitor_id,starts_at,ends_at,time_zone,status,created_at,updated_at) "
                      "VALUES (101,2000200000,2000201800,'Europe/Moscow','held',1,1)")
    monkeypatch.setattr(database, "SCHEMA_VERSION", 8)
    with pytest.raises(RuntimeError, match="newer"):
        initialize_database(path)


def test_failed_migration_rolls_back_and_backup_restores(tmp_path, monkeypatch):
    path, backup = tmp_path / "db.sqlite", tmp_path / "snapshot.sqlite"
    monkeypatch.setattr(database, "SCHEMA_VERSION", 8)
    initialize_database(path)
    with connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    monkeypatch.setattr(database, "SCHEMA_VERSION", 9)
    original = database._statements
    def broken(source):
        yield from original(source)
        yield "INSERT INTO table_does_not_exist VALUES (1);"
    monkeypatch.setattr(database, "_statements", broken)
    with pytest.raises(sqlite3.OperationalError):
        initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 8
        assert "revision" not in {r[1] for r in c.execute("PRAGMA table_info(bookings)")}
    monkeypatch.setattr(database, "_statements", original)
    initialize_database(path)
    with sqlite3.connect(backup) as source, connect(path) as target:
        source.backup(target)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 8
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
