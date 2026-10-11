"""MA-5: actual HTTP/flow journeys, offline recovery and migration rehearsals."""
import multiprocessing as mp
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from meeting_bot import database
from meeting_bot.admin_flow import AdminFlow
from meeting_bot.api.app import create_app
from meeting_bot.api.config import ApiConfig
from meeting_bot.database import connect
from meeting_bot.service import BookingService
from meeting_bot.visitor_flow import VisitorFlow
from meeting_bot.worker import run_due_jobs
from meeting_bot.notification_delivery import run_due_notifications
from test_mini_app_api import h, P, ADMIN, VISITOR, OTHER, TOKEN, ORIGIN, signed, at
from test_stage6_journey import FakeCalendar, button


def visit(flow, user, action, now, text=None):
    return flow.handle(user, "verified_user", action, now, text=text)


def submit_in_chat(h, flow):
    reply=visit(flow,VISITOR,"book",h.now)
    reply=visit(flow,VISITOR,button(reply,"t:"),h.now)
    visit(flow,VISITOR,"d:30",h.now)
    reply=visit(flow,VISITOR,"v:telemost",h.now)
    reply=visit(flow,VISITOR,button(reply,"day:"),h.now)
    visit(flow,VISITOR,button(reply,"time:"),h.now)
    visit(flow,VISITOR,"text",h.now,"Guest")
    visit(flow,VISITOR,"text",h.now,"guest@example.test")
    visit(flow,VISITOR,"phone:skip",h.now)
    visit(flow,VISITOR,"text",h.now,"Acceptance journey")
    visit(flow,VISITOR,"consent:yes",h.now)
    assert "отправлена" in visit(flow,VISITOR,"send",h.now).text
    with connect(h.path) as c:
        return c.execute("SELECT id FROM bookings WHERE visitor_id=?",(VISITOR,)).fetchone()[0]


@pytest.mark.parametrize("created_in",["api","chat"])
def test_two_interfaces_share_decision_confirmation_cancel_and_notifications(h,created_in):
    h.calendar=FakeCalendar();h.client=h.restart()
    h.login();h.login(ADMIN)
    flow=VisitorFlow(h.service,h.calendar,policy_url=h.config.policy_url,policy_version="policy-v1")
    admin=AdminFlow(h.service,h.calendar)
    if created_in=="api":
        data,_=h.ready_hold();bid=data["booking"]["id"]
        result=h.command(f"/holds/{bid}/draft",dict(revision=data["current"]["revision"],contact_name="Guest",contact_email="guest@example.test",purpose="Acceptance journey"),method="PATCH",key="journey-draft-key-001")
        assert result.status_code==200,result.text
        body=dict(revision=result.json()["data"]["revision"],policy_version="policy-v1")
        assert h.command(f"/holds/{bid}/submit",body,key="journey-submit-key-01").status_code==200
        assert "Заявка согласована" in admin.handle(ADMIN,f"adm:approve:{bid}",h.now).text
        assert "подтверждена" in admin.handle(ADMIN,"text",h.now,text="https://telemost.yandex.ru/j/acceptance").text
    else:
        h.service.create_template(ADMIN,"Meeting","Description","Instruction",h.now)
        h.service.publish_interval(ADMIN,at(5,16),at(5,18),"Europe/Moscow",h.now)
        bid=submit_in_chat(h,flow)
        row=h.client.get(P+f"/admin/bookings/{bid}",headers=h.headers[ADMIN]).json()["data"]
        result=h.command(f"/admin/bookings/{bid}/decision",dict(revision=row["revision"],approve=True),user=ADMIN,key="journey-decision-key1")
        assert result.status_code==200,result.text
        result=h.command(f"/admin/bookings/{bid}/video-link",dict(revision=result.json()["data"]["revision"],video_url="https://telemost.yandex.ru/j/acceptance"),user=ADMIN,key="journey-video-link-01")
        assert result.status_code==200,result.text
    assert h.calendar.created==1
    h.client=h.restart()  # Persistent sessions and shared booking survive API restart.
    row=h.client.get(P+f"/me/bookings/{bid}",headers=h.headers[VISITOR]).json()["data"]
    assert row["status"]=="confirmed"
    assert "№"+str(bid) in visit(flow,VISITOR,"mine",h.now).text
    if created_in=="api":
        result=h.command(f"/me/bookings/{bid}/cancel",dict(revision=row["revision"],confirmed=True),key="journey-cancel-key-01")
        assert result.status_code==200,result.text
    else:
        visit(flow,VISITOR,f"cancel:{bid}",h.now)
        assert "после проверки календаря" in visit(flow,VISITOR,f"cancel_yes:{bid}",h.now).text
    assert h.service.booking_status(bid)=="attention"
    assert run_due_jobs(h.path,h.now+1,calendar=h.calendar,administrator_id=ADMIN)==1
    assert run_due_jobs(h.path,h.now+2,calendar=h.calendar,administrator_id=ADMIN)==0
    row=h.client.get(P+f"/me/bookings/{bid}",headers=h.headers[VISITOR]).json()["data"]
    assert row["status"]=="cancelled_by_visitor" and h.calendar.deleted==1
    messages=[]
    run_due_notifications(h.path,h.now+2,{"BOT_ADMIN_ID":str(ADMIN)},telegram_send=lambda uid,text:messages.append((uid,text)))
    before=len(messages)
    run_due_notifications(h.path,h.now+3,{"BOT_ADMIN_ID":str(ADMIN)},telegram_send=lambda uid,text:messages.append((uid,text)))
    assert len(messages)==before
    assert any(uid==VISITOR and "подтверждена" in text for uid,text in messages)
    assert any(uid==ADMIN and "отменил" in text for uid,text in messages)
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]==1
        assert c.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]==1
    for private in ("guest@example.test","Acceptance journey","https://telemost.yandex.ru/j/acceptance"):
        assert private not in h.log.read_text(encoding="utf8")


def test_disable_api_preserves_chat_and_pending_worker_jobs(h):
    h.calendar=FakeCalendar();h.client=h.restart();h.login();h.login(ADMIN)
    data,_=h.ready_hold();bid=data["booking"]["id"]
    h.service.submit_request(VISITOR,bid,name="Guest",username="verified",email="guest@example.test",phone=None,purpose="Goal",policy_version="policy-v1",now=h.now)
    disabled=create_app(h.service,h.calendar,replace(h.config,enabled=False),clock=lambda:h.now)
    client=TestClient(disabled)
    for result in (client.post(P+"/session",json={"init_data":signed(VISITOR,h.now)}),client.get(P+"/bootstrap",headers=h.headers[VISITOR]),client.post(P+f"/admin/bookings/{bid}/decision",json={"revision":0,"approve":True},headers=h.headers[ADMIN])):
        assert result.status_code==503,result.text
        assert result.json()["error"]["code"]=="MINI_APP_DISABLED"
    admin=AdminFlow(h.service,h.calendar)
    assert "Заявка согласована" in admin.handle(ADMIN,f"adm:approve:{bid}",h.now).text
    assert "подтверждена" in admin.handle(ADMIN,"text",h.now,text="https://telemost.yandex.ru/j/disabled").text
    flow=VisitorFlow(h.service,h.calendar,policy_url=h.config.policy_url,policy_version="policy-v1")
    visit(flow,VISITOR,f"cancel:{bid}",h.now)
    visit(flow,VISITOR,f"cancel_yes:{bid}",h.now)
    assert run_due_jobs(h.path,h.now+1,calendar=h.calendar,administrator_id=ADMIN)==1
    assert h.service.booking_status(bid)=="cancelled_by_visitor"
    # No toggle endpoint; process config/operator state only. Re-enabling keeps data/session.
    disabled.state.mini_app_enabled=True
    assert client.get(P+f"/me/bookings/{bid}",headers=h.headers[VISITOR]).json()["data"]["status"]=="cancelled_by_visitor"


def test_lock_returns_safe_503_without_partial_command_and_retry_succeeds(h):
    h.login(ADMIN)
    lock=connect(h.path)
    try:
        lock.execute("BEGIN IMMEDIATE")
        result=h.command("/admin/templates",dict(name="After lock",description="",instructions=""),user=ADMIN,key="storage-lock-key-001")
        assert result.status_code==503,result.text
        assert result.json()["error"]["code"]=="STORAGE_BUSY"
        assert result.json()["error"]["retryable"] is True
        assert result.headers["Retry-After"]=="2"
    finally:
        lock.execute("ROLLBACK");lock.close()
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM meeting_templates").fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM api_operations").fetchone()[0]==0
    result=h.command("/admin/templates",dict(name="After lock",description="",instructions=""),user=ADMIN,key="storage-lock-key-001")
    assert result.status_code==200,result.text


def test_v8_to_v11_backup_restore_and_old_code_rejection(tmp_path,monkeypatch):
    path=tmp_path/"rehearsal.sqlite";backup=tmp_path/"snapshot.sqlite"
    monkeypatch.setattr(database,"SCHEMA_VERSION",8)
    database.initialize_database(path)
    with connect(path) as c:
        c.execute("INSERT INTO visitors(telegram_id,created_at,last_used_at) VALUES (101,1,1)")
        c.execute("INSERT INTO bookings(visitor_id,starts_at,ends_at,time_zone,status,contact_name_snapshot,created_at,updated_at) VALUES (101,2000000000,2000001800,'Europe/Moscow','confirmed','Snapshot',1,1)")
        before=c.execute("SELECT * FROM bookings").fetchall()
        with sqlite3.connect(backup) as target:c.backup(target)
    monkeypatch.setattr(database,"SCHEMA_VERSION",11)
    database.initialize_database(path);database.initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==11
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert c.execute("PRAGMA foreign_key_check").fetchall()==[]
        after=c.execute("SELECT * FROM bookings").fetchall()
        assert after[0][:len(before[0])]==before[0]
    monkeypatch.setattr(database,"SCHEMA_VERSION",8)
    with pytest.raises(RuntimeError,match="newer"):database.initialize_database(path)
    with sqlite3.connect(backup) as source,connect(path) as target:source.backup(target)
    database.initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==8
        assert c.execute("SELECT * FROM bookings").fetchall()==before
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"


def hold_process(path,headers,template,interval,gate,queue,mode):
    try:
        service=BookingService(Path(path),administrator_id=ADMIN)
        calendar=FakeCalendar()
        gate.wait(10)
        if mode=="api":
            app=create_app(service,calendar,ApiConfig(TOKEN,"policy-v1","https://example.org/privacy",(ORIGIN,)),clock=lambda:at(5,10))
            result=TestClient(app).post(P+"/holds",json=dict(revision=0,template_id=template,interval_id=interval,starts_at=at(5,16),duration_minutes=30,video_service="telemost"),headers={**headers,"Idempotency-Key":"race-hold-key-000001"})
            queue.put((mode,result.status_code))
        else:
            flow=VisitorFlow(service,calendar,policy_url="https://example.org/privacy",policy_version="policy-v1")
            reply=visit(flow,OTHER,"time:"+str(at(5,16)),at(5,10))
            queue.put((mode,reply.text))
    except Exception as error:queue.put(("error",type(error).__name__))


def test_api_and_chat_processes_compete_for_one_slot(h):
    h.login()
    template=h.service.create_template(ADMIN,"Race","","",h.now)
    interval=h.service.publish_interval(ADMIN,at(5,16),at(5,18),"Europe/Moscow",h.now)
    flow=VisitorFlow(h.service,FakeCalendar(),policy_url=h.config.policy_url,policy_version="policy-v1")
    visit(flow,OTHER,"book",h.now);visit(flow,OTHER,f"t:{template}",h.now)
    visit(flow,OTHER,"d:30",h.now);visit(flow,OTHER,"v:telemost",h.now)
    visit(flow,OTHER,"day:2026-10-05",h.now)
    ctx=mp.get_context("spawn");gate=ctx.Event();queue=ctx.Queue()
    processes=[ctx.Process(target=hold_process,args=(str(h.path),h.headers[VISITOR],template,interval,gate,queue,mode)) for mode in ("api","chat")]
    try:
        for process in processes:process.start()
        gate.set()
        results=[queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(30);assert process.exitcode==0
        assert all(row[0]!="error" for row in results),results
        api_result=next(row[1] for row in results if row[0]=="api")
        assert api_result in (200,409),results
        with connect(h.path) as c:
            rows=c.execute("SELECT visitor_id,status FROM bookings WHERE status='held'").fetchall()
            assert len(rows)==1,results
            assert rows[0][0]==(VISITOR if api_result==200 else OTHER),results
    finally:
        for process in processes:
            if process.is_alive():process.terminate();process.join(5)
        queue.close()


def test_every_admin_route_requires_admin_before_business_or_google(h):
    import re
    h.login()
    bodies={
        "AccessDecisionInput":dict(revision=0,status="allowed"),
        "DecisionInput":dict(revision=0,approve=True),
        "VideoInput":dict(revision=0,video_url="https://telemost.yandex.ru/j/forbidden"),
        "VersionInput":dict(revision=0),
        "ConfirmInput":dict(revision=0,confirmed=True),
        "AvailabilityInput":dict(starts_at=at(5,16),ends_at=at(5,18),time_zone="Europe/Moscow"),
        "ImpactInput":dict(kind="interval",interval_id=1),
        "DayInput":dict(local_date="2026-10-05",time_zone="Europe/Moscow",confirmed=True),
        "BlockInput":dict(starts_at=at(5,16),ends_at=at(5,18),confirmed=True),
        "TemplateInput":dict(name="Forbidden",description="",instructions=""),
        "TemplateEditInput":dict(name="Forbidden",description="",instructions="",revision=0,is_active=True),
        "ScheduleInput":dict(revision=0,time_zone="Europe/Moscow",windows=[dict(weekday=0,starts_minute=600,ends_minute=1080)]),
        "StartOptionsInput":dict(revision=0,minutes=[630]),
        "Input":{},
    }
    checked=[]
    for route in h.app.routes:
        if not getattr(route,"path","").startswith(P+"/admin/"):continue
        path=re.sub(r"\{[^}]+\}","1",route.path)
        for method in sorted(route.methods-{"HEAD","OPTIONS"}):
            body=None
            if route.body_field:
                model=route.body_field.field_info.annotation
                body=bodies[model.__name__]
                model.model_validate(body)  # Malformed input must not mask a missing guard.
            for headers,code in ((h.headers[VISITOR],403),({},401)):
                result=h.client.request(method,path,json=body,headers={**headers,"Idempotency-Key":"admin-forbidden-key-001"})
                assert result.status_code==code,(method,path,result.text)
            checked.append((method,path))
    assert len(checked)>=29
    assert h.calendar.calls==0
    with connect(h.path) as c:
        for table in ("bookings","meeting_templates","availability_intervals","api_operations"):
            assert c.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]==0


def test_switch_off_during_preparation_rechecks_before_write(h):
    h.login()
    template=h.service.create_template(ADMIN,"Guard","","",h.now)
    interval=h.service.publish_interval(ADMIN,at(5,16),at(5,18),"Europe/Moscow",h.now)
    original=h.calendar.freebusy
    def disable(*args):
        result=original(*args)
        h.app.state.mini_app_enabled=False
        return result
    h.calendar.freebusy=disable
    body=dict(revision=0,template_id=template,interval_id=interval,starts_at=at(5,16),duration_minutes=30,video_service="telemost")
    result=h.command("/holds",body,key="disabled-during-prepare")
    assert result.status_code==503 and result.json()["error"]["code"]=="MINI_APP_DISABLED"
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM api_operations").fetchone()[0]==0
    h.app.state.mini_app_enabled=True;h.calendar.freebusy=original
    assert h.command("/holds",body,key="disabled-during-prepare").status_code==200


def test_test_launcher_reads_strict_switch_without_reading_working_env(tmp_path):
    from meeting_bot.api.__main__ import test_settings
    env=tmp_path/".env-miniapp-test"
    base="MINI_APP_MODE=test\nTEST_BOT_USERNAME=IsolatedMeetTest_bot\nTELEGRAM_BOT_TOKEN=123456789:synthetic-local-test-secret\nBOT_ADMIN_ID=9001\nMINI_APP_ORIGIN=https://mini-test.example.org\nPERSONAL_DATA_POLICY_VERSION=policy-v1\nPERSONAL_DATA_POLICY_URL=https://example.org/privacy\n"
    for value,expected in (("true",True),("false",False)):
        env.write_text(base+"MINI_APP_ENABLED="+value+"\n",encoding="utf8")
        assert test_settings(env)[-1].enabled is expected
    env.write_text(base+"MINI_APP_ENABLED=maybe\n",encoding="utf8")
    with pytest.raises(ValueError,match="MINI_APP_ENABLED"):test_settings(env)


def test_v11_failure_keeps_v10_then_can_retry(tmp_path,monkeypatch):
    path=tmp_path/"failed-upgrade.sqlite"
    monkeypatch.setattr(database,"SCHEMA_VERSION",10);database.initialize_database(path)
    original=database._statements
    def broken(source):
        yield from original(source)
        yield "INSERT INTO missing_table VALUES (1);"
    monkeypatch.setattr(database,"SCHEMA_VERSION",11)
    monkeypatch.setattr(database,"_statements",broken)
    with pytest.raises(sqlite3.OperationalError):database.initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==10
        assert c.execute("SELECT name FROM sqlite_master WHERE name='booking_start_options'").fetchall()==[]
    monkeypatch.setattr(database,"_statements",original);database.initialize_database(path)
    with connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0]==11
        assert c.execute("PRAGMA integrity_check").fetchone()[0]=="ok"


def test_https_test_frontend_serves_only_build_with_no_identity_bypass(h,tmp_path):
    from meeting_bot.api.frontend import mount_frontend
    assets=tmp_path/"dist";assets.mkdir()
    (assets/"index.html").write_text('<html><script src="/app/assets/test.js"></script></html>',encoding="utf8")
    (assets/"assets").mkdir();(assets/"assets"/"test.js").write_text("/* synthetic build */",encoding="utf8")
    (tmp_path/".env").write_text("NEVER_PUBLIC",encoding="utf8")
    mount_frontend(h.app,assets)
    page=h.client.get("/app/")
    assert page.status_code==200
    assert "script-src 'self' https://telegram.org" in page.headers["Content-Security-Policy"]
    assert page.headers["cache-control"]=="no-store"
    assert h.client.get("/app/assets/test.js").status_code==200
    for path in ("/app/.env","/app/%2e%2e/.env","/app/db.sqlite","/app/db.sqlite-wal","/app/src/App.tsx"):
        result=h.client.get(path)
        assert result.status_code==404,(path,result.text)
        assert "NEVER_PUBLIC" not in result.text
    assert h.client.post("/local-preview/session",json={"persona":"admin"}).status_code==404
    assert h.client.get(P+"/bootstrap").status_code==401
    h.app.state.mini_app_enabled=False
    assert h.client.get("/app/").status_code==200  # Page explains the switch via API error.
    assert h.client.get(P+"/bootstrap").status_code==503


def test_frontend_mount_rejects_missing_build_or_accidental_secrets(h,tmp_path):
    from meeting_bot.api.frontend import mount_frontend
    with pytest.raises(ValueError,match="Build"):mount_frontend(h.app,tmp_path/"missing")
    (tmp_path/"index.html").write_text("test",encoding="utf8")
    (tmp_path/".env").write_text("never-serve",encoding="utf8")
    with pytest.raises(ValueError,match="Unexpected"):mount_frontend(h.app,tmp_path)


def ready_pending(h):
    h.login();h.login(ADMIN)
    data,_=h.ready_hold();bid=data["booking"]["id"]
    response=h.command(f"/holds/{bid}/draft",dict(revision=data["current"]["revision"],contact_name="Guest",contact_email="guest@example.test",purpose="Race"),method="PATCH",key="race-valid-draft-001")
    assert response.status_code==200,response.text
    return bid,response.json()["data"]["revision"]


def test_approve_and_cancel_with_same_revision_have_one_winner(h):
    from concurrent.futures import ThreadPoolExecutor
    bid,revision=ready_pending(h)
    assert h.command(f"/holds/{bid}/submit",dict(revision=revision,policy_version="policy-v1"),key="race-valid-submit-01").status_code==200
    row=h.client.get(P+f"/me/bookings/{bid}",headers=h.headers[VISITOR]).json()["data"]
    def command(mode):
        if mode=="approve":return h.command(f"/admin/bookings/{bid}/decision",dict(revision=row["revision"],approve=True),user=ADMIN,key="race-approve-key-001")
        return h.command(f"/me/bookings/{bid}/cancel",dict(revision=row["revision"],confirmed=True),key="race-cancel-key-0001")
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(command,("approve","cancel")))
    assert sorted(r.status_code for r in results)==[200,409],[r.text for r in results]
    assert h.service.booking_status(bid) in ("awaiting_link","cancelled_by_visitor")
    assert not h.calendar.events


def test_two_different_video_links_have_one_effect(h):
    from concurrent.futures import ThreadPoolExecutor
    h.calendar=FakeCalendar();h.client=h.restart()
    bid,revision=ready_pending(h)
    assert h.command(f"/holds/{bid}/submit",dict(revision=revision,policy_version="policy-v1"),key="race-link-submit-001").status_code==200
    h.service.admin_decision(ADMIN,bid,True,h.now)
    row=h.client.get(P+f"/admin/bookings/{bid}",headers=h.headers[ADMIN]).json()["data"]
    def command(index):return h.command(f"/admin/bookings/{bid}/video-link",dict(revision=row["revision"],video_url="https://telemost.yandex.ru/j/choice"+str(index)),user=ADMIN,key="race-link-key-00000"+str(index))
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(command,(1,2)))
    assert sorted(r.status_code for r in results)==[200,409],[r.text for r in results]
    assert h.calendar.created==1 and h.service.booking_status(bid)=="confirmed"
    with connect(h.path) as c:assert c.execute("SELECT COUNT(*) FROM meetings WHERE booking_id=?",(bid,)).fetchone()[0]==1


@pytest.mark.parametrize("change",["access","interval"])
def test_submit_rechecks_changes_after_freebusy_before_commit(h,change):
    bid,revision=ready_pending(h)
    original=h.calendar.freebusy
    def change_state(*args):
        busy=original(*args)
        if change=="access":h.service.decide_access(ADMIN,VISITOR,"blocked",h.now)
        else:
            with connect(h.path) as c:interval=c.execute("SELECT id FROM availability_intervals LIMIT 1").fetchone()[0]
            h.service.close_interval(ADMIN,interval,h.now)
        return busy
    h.calendar.freebusy=change_state
    result=h.command(f"/holds/{bid}/submit",dict(revision=revision,policy_version="policy-v1"),key="race-recheck-submit-01")
    assert result.status_code in (403,409,410,422),result.text
    assert h.service.booking_status(bid)!="pending"
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM api_operations WHERE route=?",(f"POST /api/v1/holds/{bid}/submit",)).fetchone()[0]==0


def test_submit_at_hold_boundary_competes_with_expiry_without_submission(h):
    from concurrent.futures import ThreadPoolExecutor
    bid,revision=ready_pending(h);h.now+=600
    def submit():return h.command(f"/holds/{bid}/submit",dict(revision=revision,policy_version="policy-v1"),key="race-expire-submit-01")
    def expire():return run_due_jobs(h.path,h.now,calendar=h.calendar,administrator_id=ADMIN)
    with ThreadPoolExecutor(max_workers=2) as pool:
        request=pool.submit(submit);worker=pool.submit(expire)
        result=request.result();worker.result()
    assert result.status_code in (409,410),result.text
    assert h.service.booking_status(bid)=="expired"
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM bookings WHERE status='pending'").fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM api_operations WHERE route=?",(f"POST /api/v1/holds/{bid}/submit",)).fetchone()[0]==0
