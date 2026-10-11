import sqlite3
import multiprocessing as mp
from pathlib import Path

import pytest
from meeting_bot import database
from meeting_bot.database import connect
from meeting_bot.service import BookingService
from meeting_bot.api.state import StateStore
from meeting_bot.api.config import DEFAULT_POLICY


def test_v9_to_v10_preserves_business_data_and_backup(tmp_path,monkeypatch):
    path=tmp_path/"migration.sqlite"
    monkeypatch.setattr(database,"SCHEMA_VERSION",9)
    service=BookingService(path,administrator_id=9001)
    service.initialize()
    service.create_template(9001,"Keep snapshot","Description","Instructions",100)
    backup=tmp_path/"v9-backup.sqlite"
    with connect(path) as source,sqlite3.connect(backup) as target:
        source.backup(target)
    monkeypatch.setattr(database,"SCHEMA_VERSION",10)
    service.initialize();service.initialize()
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==10
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall()==[]
        assert c.execute("SELECT name FROM meeting_templates").fetchone()[0]=="Keep snapshot"
    monkeypatch.setattr(database,"SCHEMA_VERSION",9)
    with pytest.raises(RuntimeError,match="newer"):
        service.initialize()
    with sqlite3.connect(backup) as source,connect(path) as target:
        source.backup(target)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==9
        assert c.execute("SELECT name FROM meeting_templates").fetchone()[0]=="Keep snapshot"


def test_v10_migration_failure_is_atomic(tmp_path,monkeypatch):
    path=tmp_path/"migration.sqlite"
    monkeypatch.setattr(database,"SCHEMA_VERSION",9)
    database.initialize_database(path)
    original=database._statements
    def fail(source):
        yield from original(source)
        yield "INSERT INTO missing_table VALUES (1);"
    monkeypatch.setattr(database,"SCHEMA_VERSION",10)
    monkeypatch.setattr(database,"_statements",fail)
    with pytest.raises(sqlite3.OperationalError):
        database.initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==9
        assert c.execute("SELECT name FROM sqlite_master WHERE name='api_sessions'").fetchall()==[]
        assert "revision" not in {r[1] for r in c.execute("PRAGMA table_info(manual_blocks)")}


def process_command(path,event,queue):
    try:
        service=BookingService(Path(path),administrator_id=9001)
        store=StateStore(Path(path),b"test-state-secret",DEFAULT_POLICY)
        identity=store.operation_key(9001,"POST /templates","shared-process-key-01",dict(name="Once"))
        event.wait(10)
        def apply():
            identity=service.create_template(9001,"Once","","",100)
            return dict(kind="template",id=identity)
        result,_=store.execute(identity,100,lambda c:None,apply)
        queue.put(("ok",result["ref"]["id"],result["operation_id"]))
    except Exception as e:
        queue.put(("error",type(e).__name__))


def test_idempotency_shared_across_processes(tmp_path):
    path=tmp_path/"shared.sqlite"
    database.initialize_database(path)
    ctx=mp.get_context("spawn")
    event,queue=ctx.Event(),ctx.Queue()
    processes=[ctx.Process(target=process_command,args=(str(path),event,queue)) for _ in range(2)]
    for process in processes:process.start()
    event.set()
    results=[queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode==0
    assert results[0][0]==results[1][0]=="ok",results
    assert results[0][1:]==results[1][1:]
    with connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM meeting_templates").fetchone()[0]==1
