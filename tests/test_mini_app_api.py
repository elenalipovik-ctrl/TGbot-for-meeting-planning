import hashlib
import hmac
import json
import sqlite3
from dataclasses import replace
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from meeting_bot.api.app import create_app
from meeting_bot.api.config import ApiConfig, DEFAULT_POLICY
from meeting_bot.api.errors import ApiError
from meeting_bot.api.telegram_auth import verify_init_data
from meeting_bot.database import connect
from meeting_bot.drafts import DraftStore
from meeting_bot.google_calendar import CalendarUnavailable
from meeting_bot.service import BookingService
from meeting_bot.technical_log import configure_logging
from test_service import ADMIN, VISITOR, OTHER, at, setup_user

TOKEN="123456789:synthetic-local-test-secret"
ORIGIN="https://mini-test.example.org"
P="/api/v1"


def signed(user=VISITOR,now=None,extra=None,token=TOKEN):
    values={"auth_date":str(now if now is not None else at(5,10)),"user":json.dumps({"id":user,"username":"verified_user"},separators=(",",":")),"query_id":"test-query-"+str(user)}
    values.update(extra or {})
    message="\n".join(k+"="+v for k,v in sorted(values.items()))
    secret=hmac.digest(b"WebAppData",token.encode(),"sha256")
    values["hash"]=hmac.digest(secret,message.encode(),"sha256").hex()
    return urlencode(values)


class Calendar:
    def __init__(self,path):
        self.path=path
        self.calls=0
        self.events={}
        self.fail=False
    def freebusy(self,start,end):
        with connect(self.path,busy_timeout_ms=10) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("ROLLBACK")
        self.calls+=1
        if self.fail:
            raise CalendarUnavailable("sensitive-upstream-body")
        return []
    def find_matching(self,**kw):
        return kw["event_id"] in self.events
    def create_or_find(self,**kw):
        self.events.setdefault(kw["event_id"],kw)
        return kw["event_id"]


class Harness:
    def __init__(self,tmp_path):
        self.now=at(5,10)
        self.path=tmp_path/"db.sqlite"
        self.log=tmp_path/"api.log"
        self.service=BookingService(self.path,administrator_id=ADMIN,logger=configure_logging(self.log))
        self.service.initialize()
        setup_user(self.service,VISITOR,self.now)
        setup_user(self.service,OTHER,self.now)
        self.calendar=Calendar(self.path)
        self.config=ApiConfig(TOKEN,"policy-v1","https://example.org/privacy",(ORIGIN,))
        self.client=self.restart()
        self.headers={}
    def restart(self):
        self.app=create_app(self.service,self.calendar,self.config,clock=lambda:self.now)
        return TestClient(self.app)
    def login(self,user=VISITOR,**kw):
        raw=signed(user,self.now,**kw)
        result=self.client.post(P+"/session",json={"init_data":raw})
        assert result.status_code==200,result.text
        headers={"Authorization":"Bearer "+result.json()["data"]["bearer"]}
        self.headers[user]=headers
        return headers
    def command(self,path,body,user=VISITOR,method="POST",key="test-command-key-0001"):
        return self.client.request(method,P+path,json=body,headers={**self.headers[user],"Idempotency-Key":key})
    def ready_hold(self):
        template=self.service.create_template(ADMIN,"Meeting","Description","Instruction",self.now)
        interval=self.service.publish_interval(ADMIN,at(5,16),at(5,18),"Europe/Moscow",self.now)
        revision=DraftStore(self.service).read(VISITOR,VISITOR,self.now).revision
        result=self.command("/holds",dict(revision=revision,template_id=template,interval_id=interval,starts_at=at(5,16),duration_minutes=30,video_service="telemost"))
        assert result.status_code==200,result.text
        return result.json()["data"],result.request


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


@pytest.mark.parametrize("age,code",[(0,None),(300,None),(301,"AUTH_INVALID"),(-30,None),(-31,"AUTH_INVALID")])
def test_auth_time_boundaries(age,code):
    now=at(5,10)
    raw=signed(now=now-age)
    if code:
        with pytest.raises(ApiError) as e:
            verify_init_data(raw,TOKEN,now=now,max_age=300,future_skew=30)
        assert e.value.code==code
    else:
        assert verify_init_data(raw,TOKEN,now=now,max_age=300,future_skew=30).telegram_id==VISITOR


@pytest.mark.parametrize("change",[lambda s:s+"&auth_date=1",lambda s:s+"&user=x",lambda s:s.replace("hash=","hash=00"),lambda s:s+"&bad=%XX",lambda s:s+"&x=%FF",lambda s:s+"&hash=x",lambda s:"",lambda s:s+"&user%00=x"])
def test_auth_rejects_malformed(change):
    with pytest.raises(ApiError):
        verify_init_data(change(signed()),TOKEN,now=at(5,10),max_age=300,future_skew=30)


def test_signed_signature_included_wrong_bot_and_wide_id(h):
    raw=signed(2**52+123,extra={"signature":"signed-extra-field"})
    assert verify_init_data(raw,TOKEN,now=h.now,max_age=300,future_skew=30).telegram_id==2**52+123
    with pytest.raises(ApiError):
        verify_init_data(raw,"other-bot-token",now=h.now,max_age=300,future_skew=30)
    result=h.client.post(P+"/session",json={"init_data":raw})
    headers={"Authorization":"Bearer "+result.json()["data"]["bearer"]}
    assert h.client.get(P+"/bootstrap",headers=headers).json()["data"]["telegram_id"]==str(2**52+123)


def test_replay_canonical_persistent_sessions_revocation(h):
    headers=h.login()
    raw="&".join(reversed(signed().split("&")))
    h.client=h.restart()
    replay=h.client.post(P+"/session",json={"init_data":raw})
    assert replay.status_code==401 and replay.json()["error"]["code"]=="AUTH_REPLAY"
    assert h.client.get(P+"/bootstrap",headers=headers).status_code==200
    token=headers["Authorization"][7:]
    with connect(h.path) as c:
        assert c.execute("SELECT token_hash FROM api_sessions").fetchone()[0]!=token
        assert token not in repr(c.execute("SELECT * FROM api_sessions").fetchall())
    assert h.client.delete(P+"/session",headers=headers).status_code==200
    assert h.client.get(P+"/bootstrap",headers=headers).status_code==401


def test_session_expiry_and_fresh_access(h):
    headers=h.login()
    assert h.client.get(P+"/me/profile",headers=headers).status_code==200
    h.service.decide_access(ADMIN,VISITOR,"blocked",h.now)
    result=h.client.get(P+"/me/profile",headers=headers)
    assert result.status_code==403 and result.json()["error"]["code"]=="ACCESS_BLOCKED"
    assert h.client.get(P+"/bootstrap",headers=headers).json()["data"]["access_status"]=="blocked"
    h.now+=1800
    assert h.client.get(P+"/bootstrap",headers=headers).status_code==401


def test_boundary_no_secrets_logs_errors_and_cors(h):
    headers=h.login()
    marker="SECRET_EMAIL@example.org"
    for result in [h.client.post(P+"/session",json={"init_data":marker}),
                   h.client.post(P+"/holds",json={marker:marker},headers=headers),
                   h.client.get(P+"/me/profile?"+marker+"="+marker,headers=headers),
                   h.client.get(P+"/"+marker,headers=headers)]:
        assert result.status_code in (400,401,404,422)
        assert marker not in result.text
        assert result.headers["cache-control"]=="no-store"
        assert "request_id" in result.json()
    assert h.client.get(P+"/bootstrap",headers={**headers,"Origin":"https://evil.example.org"}).status_code==403
    okay=h.client.get(P+"/bootstrap",headers={**headers,"Origin":ORIGIN})
    assert okay.headers["access-control-allow-origin"]==ORIGIN
    assert "access-control-allow-credentials" not in okay.headers
    assert h.client.post(P+"/session",content='{"init_data":"a","init_data":"b"}',headers={"Content-Type":"application/json"}).status_code==400
    assert h.client.post(P+"/session",content="x"*16385).status_code==413
    log=h.log.read_text(encoding="utf8")
    for value in [marker,TOKEN,headers["Authorization"],"init_data",signed()]:
        assert value not in log
    assert '"event":"api_request"' in log


def test_full_booking_chat_compatibility_idempotency_privacy(h):
    h.login();h.login(ADMIN);h.login(OTHER)
    data,_=h.ready_hold()
    bid=data["booking"]["id"]
    hold_revision=data["current"]["revision"]
    assert h.client.get(P+f"/me/bookings/{bid}",headers=h.headers[OTHER]).status_code==404
    assert h.client.get(P+"/admin/bookings",headers=h.headers[VISITOR]).status_code==403
    patch=dict(revision=hold_revision,contact_name="Canonical Name",contact_email="canonical@example.org",purpose="Discuss")
    result=h.command(f"/holds/{bid}/draft",patch,method="PATCH",key="patch-contacts-key-001")
    assert result.status_code==200,result.text
    revision=result.json()["data"]["revision"]
    body=dict(revision=revision,policy_version="policy-v1")
    first=h.command(f"/holds/{bid}/submit",body,key="submit-command-key-001")
    assert first.status_code==200,first.text
    calls=h.calendar.calls
    h.client=h.restart()
    replay=h.command(f"/holds/{bid}/submit",body,key="submit-command-key-001")
    assert replay.status_code==200 and replay.json()["data"]["id"]==bid
    assert replay.json()["data"]["operation_id"]==first.json()["data"]["operation_id"]
    assert h.calendar.calls==calls
    conflict=h.command(f"/holds/{bid}/submit",{**body,"revision":revision+1},key="submit-command-key-001")
    assert conflict.status_code==409 and conflict.json()["error"]["code"]=="IDEMPOTENCY_CONFLICT"
    # A decision made through the chat/common service is immediately visible to HTTP.
    h.service.admin_decision(ADMIN,bid,True,h.now)
    booking=h.client.get(P+f"/admin/bookings/{bid}",headers=h.headers[ADMIN]).json()["data"]
    url=dict(revision=booking["revision"],video_url="https://telemost.yandex.ru/j/123")
    result=h.command(f"/admin/bookings/{bid}/video-link",url,user=ADMIN,key="video-link-command-001")
    assert result.status_code==200,result.text
    assert result.json()["data"]["status"]=="confirmed" and len(h.calendar.events)==1
    assert h.command(f"/admin/bookings/{bid}/video-link",url,user=ADMIN,key="video-link-command-001").status_code==200
    assert len(h.calendar.events)==1
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]==1
        assert c.execute("SELECT email_snapshot FROM bookings WHERE id=?",(bid,)).fetchone()[0]=="canonical@example.org"
        assert "canonical@example.org" not in repr(c.execute("SELECT * FROM api_operations").fetchall())
    assert h.client.get(P+"/operations/"+first.json()["data"]["operation_id"],headers=h.headers[OTHER]).status_code==404


def test_atomic_rollback_after_business_effect(h,monkeypatch):
    h.login(ADMIN)
    body=dict(name="Rollback",description="",instructions="")
    store=h.app.state.store
    old=store.execute
    def broken(identity,now,authorize,apply,**kwargs):
        def crash():
            apply()
            raise RuntimeError("private-sensitive-exception")
        return old(identity,now,authorize,crash,**kwargs)
    monkeypatch.setattr(store,"execute",broken)
    result=h.command("/admin/templates",body,user=ADMIN,key="atomic-rollback-key-1")
    assert result.status_code==500 and "private-sensitive-exception" not in result.text
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM meeting_templates").fetchone()[0]==0
        assert c.execute("SELECT COUNT(*) FROM api_operations").fetchone()[0]==0
    assert '"event":"template_created"' not in h.log.read_text(encoding="utf8")
    monkeypatch.setattr(store,"execute",old)
    assert h.command("/admin/templates",body,user=ADMIN,key="atomic-rollback-key-1").status_code==200


def test_concurrent_same_key_one_effect(h):
    h.login(ADMIN)
    def create(_):
        return h.command("/admin/templates",dict(name="Once",description="",instructions=""),user=ADMIN,key="concurrent-key-0001")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(create,range(2)))
    assert [r.status_code for r in results]==[200,200]
    assert results[0].json()["data"]["id"]==results[1].json()["data"]["id"]
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM meeting_templates").fetchone()[0]==1


def test_google_status_real_check_failure_and_lease_recovery(h):
    h.login();h.login(ADMIN)
    data,_=h.ready_hold();bid=data["booking"]["id"]
    h.service.submit_request(VISITOR,bid,name="Name",username="verified",email="email@example.org",phone=None,purpose="Goal",policy_version="policy-v1",now=h.now)
    h.service.admin_decision(ADMIN,bid,True,h.now)
    booking=h.client.get(P+f"/admin/bookings/{bid}",headers=h.headers[ADMIN]).json()["data"]
    body=dict(revision=booking["revision"],video_url="https://telemost.yandex.ru/j/test")
    h.calendar.fail=True
    result=h.command(f"/admin/bookings/{bid}/video-link",body,user=ADMIN,key="google-recovery-key-1")
    assert result.status_code==503 and "sensitive" not in result.text
    assert h.service.booking_status(bid)=="attention"
    h.client=h.restart();h.calendar.fail=False
    result=h.command(f"/admin/bookings/{bid}/video-link",body,user=ADMIN,key="google-recovery-key-1")
    assert result.status_code==200,result.text
    assert result.json()["data"]["status"]=="confirmed" and len(h.calendar.events)==1
    before=h.calendar.calls
    status=h.client.get(P+"/admin/integrations/google",headers=h.headers[ADMIN])
    assert status.status_code==200 and h.calendar.calls==before
    h.calendar.fail=True
    status=h.command("/admin/integrations/google/check",{},user=ADMIN,key="google-check-key-0001")
    assert status.status_code==200,status.text
    assert status.json()["data"]["status"]=="unavailable"
    assert status.json()["data"]["checked_at"]==h.now


def test_rates_persist_and_replay_after_expiry(h):
    for _ in range(10):
        assert h.client.post(P+"/session",json={"init_data":"invalid"}).status_code==401
    h.client=h.restart()
    result=h.client.post(P+"/session",json={"init_data":"invalid"})
    assert result.status_code==429 and int(result.headers["retry-after"])==60
    h.now+=60
    assert h.client.post(P+"/session",json={"init_data":"invalid"}).status_code==401


def test_smoke_page_and_openapi(h):
    page=h.client.get("/auth-check")
    assert page.status_code==200
    assert "localStorage" not in page.text and "initDataUnsafe" not in page.text
    assert "Content-Security-Policy" in page.headers
    schema=h.app.openapi()
    assert P+"/admin/bookings/{booking_id}/decision" in schema["paths"]
    assert TOKEN not in json.dumps(schema)
    assert h.client.get("/openapi.json").status_code==404


def test_admin_catalog_schedule_availability_and_statistics(h):
    h.login(ADMIN);h.login()
    paths=["/admin/access-requests","/admin/visitors","/admin/bookings","/admin/availability","/admin/manual-blocks","/admin/templates","/admin/review-schedule","/admin/statistics?days=7","/admin/statistics?days=30","/admin/alerts","/admin/integrations/google"]
    for path in paths:
        result=h.client.get(P+path,headers=h.headers[ADMIN])
        assert result.status_code==200,(path,result.text)
        assert h.client.get(P+path,headers=h.headers[VISITOR]).status_code==403
    schedule=h.client.get(P+"/admin/review-schedule",headers=h.headers[ADMIN]).json()["data"]
    result=h.command("/admin/review-schedule",schedule,user=ADMIN,method="PUT",key="review-schedule-key-01")
    assert result.status_code==200,result.text
    old=h.command("/admin/review-schedule",schedule,user=ADMIN,method="PUT",key="review-schedule-key-02")
    assert old.status_code==409
    template=h.command("/admin/templates",dict(name="First",description="",instructions=""),user=ADMIN,key="create-template-key-01").json()["data"]
    changed={k:template[k] for k in ("name","description","instructions","is_active","revision")}
    changed["name"]="Changed"
    result=h.command(f"/admin/templates/{template['id']}",changed,user=ADMIN,method="PATCH",key="edit-template-key-001")
    assert result.status_code==200,result.text
    pub=dict(starts_at=at(5,16),ends_at=at(5,18),time_zone="Europe/Moscow")
    interval=h.command("/admin/availability",pub,user=ADMIN,key="publish-interval-key-01")
    assert interval.status_code==200,interval.text
    iid=interval.json()["data"]["interval_id"]
    slots=h.client.get(P+f"/slots?template_id={template['id']}&duration_minutes=30&video_service=telemost",headers=h.headers[VISITOR])
    assert slots.status_code==200 and slots.json()["data"]["slots"],slots.text
    block=h.command("/admin/manual-blocks",dict(starts_at=at(5,17),ends_at=at(5,18),confirmed=True),user=ADMIN,key="manual-block-key-001")
    assert block.status_code==200,block.text
    block_id=block.json()["data"]["block_id"]
    result=h.command(f"/admin/manual-blocks/{block_id}",dict(revision=0,confirmed=True),user=ADMIN,method="DELETE",key="unblock-time-key-001")
    assert result.status_code==200,result.text
    preview=h.command("/admin/availability/impact",dict(kind="interval",interval_id=iid),user=ADMIN,key="preview-impact-key-01")
    assert preview.status_code==200 and preview.json()["data"]["preview"] is True
    result=h.command(f"/admin/availability/{iid}/close",dict(revision=0,confirmed=True),user=ADMIN,key="close-interval-key-01")
    assert result.status_code==200,result.text
    result=h.command("/admin/availability/close-day",dict(local_date="2026-10-05",time_zone="Europe/Moscow",confirmed=True),user=ADMIN,key="close-day-key-00001")
    assert result.status_code==200,result.text
    assert h.client.get(P+"/admin/statistics?days=8",headers=h.headers[ADMIN]).status_code==422


def test_new_access_request_atomic_consent_and_blocked_request(h):
    newcomer=777
    h.login(newcomer);h.login(ADMIN)
    body=dict(policy_version="policy-v1",accepted=True)
    result=h.command("/me/access-requests",body,user=newcomer,key="new-access-request-01")
    assert result.status_code==200,result.text
    assert result.json()["data"]["access_status"]=="pending"
    assert h.command("/me/access-requests",body,user=newcomer,key="new-access-request-01").status_code==200
    requests=h.client.get(P+"/admin/access-requests",headers=h.headers[ADMIN]).json()["data"]
    row=next(r for r in requests if r["telegram_id"]==str(newcomer))
    result=h.command(f"/admin/visitors/{newcomer}/access",dict(revision=row["revision"],status="allowed"),user=ADMIN,key="access-decision-key-01")
    assert result.status_code==200,result.text
    assert h.client.get(P+"/templates",headers=h.headers[newcomer]).status_code==200
    h.service.decide_access(ADMIN,newcomer,"blocked",h.now)
    result=h.command("/me/access-requests",body,user=newcomer,key="new-access-request-02")
    assert result.status_code==403 and result.json()["error"]["code"]=="ACCESS_BLOCKED"
    with connect(h.path) as c:
        assert c.execute("SELECT COUNT(*) FROM access_requests WHERE visitor_id=?",(newcomer,)).fetchone()[0]==1


def test_reuse_privacy_and_stale_form(h):
    h.login()
    h.service.correct_saved_contacts(VISITOR,VISITOR,name="Saved",email="saved@example.org",phone=None,now=h.now)
    data,_=h.ready_hold();bid=data["booking"]["id"]
    result=h.command(f"/holds/{bid}/use-saved-contacts",dict(revision=data["current"]["revision"],confirmed=True),key="reuse-contacts-key-01")
    assert result.status_code==200,result.text
    form=result.json()["data"]
    assert form["draft"]["used_saved_contacts"] is True
    profile=h.client.get(P+"/me/profile",headers=h.headers[VISITOR]).json()["data"]
    body=dict(revision=profile["revision"],confirmed=True)
    removed=h.command("/me/profile/contacts",body,method="DELETE",key="clear-contacts-key-01")
    assert removed.status_code==200,removed.text
    assert removed.json()["data"]["email"] is None
    old=h.command(f"/holds/{bid}/draft",dict(revision=form["revision"],contact_email="saved@example.org"),method="PATCH",key="stale-contacts-key-01")
    assert old.status_code==409
    fresh=h.client.get(P+"/me/draft",headers=h.headers[VISITOR]).json()["data"]
    assert fresh["draft"]["contact_email"] is None
    assert h.service.booking_status(bid)=="held"
    repeated=h.command(f"/holds/{bid}/use-saved-contacts",dict(revision=data["current"]["revision"],confirmed=True),key="reuse-contacts-key-01")
    assert repeated.status_code==200 and repeated.json()["data"]["draft"]["contact_email"] is None
    released=h.command(f"/holds/{bid}",dict(revision=fresh["revision"],confirmed=True),method="DELETE",key="release-draft-key-001")
    assert released.status_code==200,released.text
    assert released.json()["data"]["draft"] is None


def test_pagination_bound_to_actor_filter_and_unknown_client_fields(h):
    h.login(ADMIN);h.login()
    for i in range(3):
        h.service.create_template(ADMIN,str(i),"","",h.now)
    first=h.client.get(P+"/templates?limit=1",headers=h.headers[VISITOR]).json()
    assert len(first["data"])==1 and first["next_cursor"]
    second=h.client.get(P+"/templates?limit=1&cursor="+first["next_cursor"],headers=h.headers[VISITOR])
    assert second.status_code==200 and second.json()["data"][0]["id"]!=first["data"][0]["id"]
    assert h.client.get(P+"/admin/templates?cursor="+first["next_cursor"],headers=h.headers[ADMIN]).status_code==400
    assert h.client.get(P+"/templates?limit=101",headers=h.headers[VISITOR]).status_code==422
    result=h.command("/admin/templates",dict(name="X",description="",instructions="",actor_id=ADMIN),user=ADMIN,key="unknown-field-key-01")
    assert result.status_code==400
    for bad in (1,"true"):
        result=h.command("/me/access-requests",dict(policy_version="policy-v1",accepted=bad),key="strict-bool-key-0001")
        assert result.status_code==422


def test_external_accepted_lease_recovers_after_process_restart(h):
    h.login(ADMIN)
    store=h.app.state.store
    principal=store.authenticate(h.headers[ADMIN]["Authorization"],h.now)
    identity=store.operation_key(ADMIN,"POST /api/v1/admin/integrations/google/check","lease-recovery-key-01",{})
    result,lease=store.execute(identity,h.now,lambda c:None,lambda:dict(kind="google"),external=True)
    # Simulate process termination before network result, leaving a durable lease.
    h.client=h.restart()
    response=h.command("/admin/integrations/google/check",{},user=ADMIN,key="lease-recovery-key-01")
    assert response.status_code==202 and h.calendar.calls==0
    h.now+=121
    response=h.command("/admin/integrations/google/check",{},user=ADMIN,key="lease-recovery-key-01")
    assert response.status_code==200 and response.json()["data"]["status"]=="working"
    assert response.json()["data"]["operation_id"]==result["operation_id"]


def test_launcher_rejects_working_files_and_bot(tmp_path,monkeypatch):
    from meeting_bot.api import __main__ as launcher
    working=tmp_path/".env"
    working.write_text("TELEGRAM_BOT_TOKEN=never-read",encoding="utf8")
    with pytest.raises(ValueError):
        launcher.test_settings(working)
    testenv=tmp_path/".env-miniapp-test"
    values="MINI_APP_MODE=test\nTEST_BOT_USERNAME=MyMeetPlanning_bot\nTELEGRAM_BOT_TOKEN=123456789:synthetic-local-test-secret\nBOT_ADMIN_ID=9001\nMINI_APP_ORIGIN=https://mini-test.example.org\nPERSONAL_DATA_POLICY_VERSION=policy-v1\nPERSONAL_DATA_POLICY_URL=https://example.org/privacy\n"
    testenv.write_text(values,encoding="utf8")
    with pytest.raises(ValueError):
        launcher.test_settings(testenv)
    testenv.write_text(values.replace("MyMeetPlanning_bot","IsolatedMeetTest_bot"),encoding="utf8")
    settings=launcher.test_settings(testenv)
    assert settings[3]==tmp_path/".secrets"/"miniapp-test"/"test.sqlite"
    class FakeResponse:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return b'{"ok":true,"result":{"username":"MyMeetPlanning_bot"}}'
    monkeypatch.setattr(launcher,"urlopen",lambda *a,**kw:FakeResponse())
    with pytest.raises(ValueError):
        launcher.verify_test_bot(TOKEN,"IsolatedMeetTest_bot")


def test_google_attention_notice_is_queued_and_renderable(h):
    from meeting_bot.google_calendar import CalendarAuthExpired
    from meeting_bot.integration_status import check_google
    def fail(*a):raise CalendarAuthExpired("private-google-error")
    h.calendar.freebusy=fail
    first=check_google(h.service,h.calendar,ADMIN,h.now)
    second=check_google(h.service,h.calendar,ADMIN,h.now)
    assert first["status"]==second["status"]=="reconnect_required"
    with connect(h.path) as c:
        rows=c.execute("SELECT kind,dedup_key FROM notifications WHERE kind='google_connection_attention'").fetchall()
        assert len(rows)==1
        assert "private" not in repr(rows)


def test_notification_worker_delivers_google_alert_with_injected_transport(h):
    from meeting_bot.google_calendar import CalendarPermissionDenied
    from meeting_bot.integration_status import check_google
    from meeting_bot.notification_delivery import run_due_notifications
    def fail(*args):raise CalendarPermissionDenied("private")
    h.calendar.freebusy=fail
    check_google(h.service,h.calendar,ADMIN,h.now)
    delivered=[]
    run_due_notifications(h.path,h.now,{"BOT_ADMIN_ID":str(ADMIN)},telegram_send=lambda recipient,text:delivered.append((recipient,text)))
    assert any(recipient==ADMIN and "Google" in text for recipient,text in delivered)


def test_mutations_cancel_profile_consent_and_unknown_owner(h):
    h.login();h.login(ADMIN);h.login(OTHER)
    profile=h.client.get(P+"/me/profile",headers=h.headers[VISITOR]).json()["data"]
    result=h.command("/me/profile",dict(revision=profile["revision"],name="Profile",email="profile@example.org",phone=None),method="PATCH",key="profile-edit-key-001")
    assert result.status_code==200,result.text
    data,_=h.ready_hold();bid=data["booking"]["id"]
    result=h.command(f"/holds/{bid}/draft",dict(revision=data["current"]["revision"],purpose="Foreign"),user=OTHER,method="PATCH",key="foreign-draft-key-01")
    assert result.status_code in (404,409)
    form=h.client.get(P+"/me/draft",headers=h.headers[VISITOR]).json()["data"]
    result=h.command("/me/consents",dict(revision=form["revision"],policy_version="policy-v1",action="withdrawn"),key="withdraw-consent-key1")
    assert result.status_code==200,result.text
    form=result.json()["data"]
    result=h.command("/me/consents",dict(revision=form["revision"],policy_version="policy-v1",action="accepted"),key="accept-consent-key01")
    assert result.status_code==200,result.text
    h.service.submit_request(VISITOR,bid,name="Name",username="verified",email="email@example.org",phone=None,purpose="Goal",policy_version="policy-v1",now=h.now)
    booking=h.client.get(P+f"/me/bookings/{bid}",headers=h.headers[VISITOR]).json()["data"]
    result=h.command(f"/me/bookings/{bid}/cancel",dict(revision=booking["revision"],confirmed=True),key="visitor-cancel-key01")
    assert result.status_code==200 and result.json()["data"]["status"]=="cancelled_by_visitor",result.text


def test_expired_hold_has_stable_error_and_cannot_resubmit(h):
    h.login()
    data,_=h.ready_hold();bid=data["booking"]["id"]
    h.now+=600
    result=h.command(f"/holds/{bid}/draft",dict(revision=data["current"]["revision"],contact_name="Late"),method="PATCH",key="expired-hold-key-001")
    assert result.status_code==410 and result.json()["error"]["code"]=="HOLD_EXPIRED",result.text
    with connect(h.path) as c:
        assert c.execute("SELECT contact_name FROM visitor_dialogs WHERE booking_id=?",(bid,)).fetchone()[0] is None
    form=h.client.get(P+"/me/draft",headers=h.headers[VISITOR]).json()["data"]
    assert form["draft"] is None and h.service.booking_status(bid)=="expired"


def test_invalid_slot_duration_rejected_before_google(h):
    h.login()
    template=h.service.create_template(ADMIN,"Meeting","","",h.now)
    result=h.client.get(P+f"/slots?template_id={template}&duration_minutes=45&video_service=zoom",headers=h.headers[VISITOR])
    assert result.status_code==422 and h.calendar.calls==0


def test_launcher_disables_access_debug_logging_and_uses_loopback(tmp_path,monkeypatch):
    from meeting_bot.api import __main__ as launcher
    import uvicorn
    env=tmp_path/".env-miniapp-test"
    env.write_text("MINI_APP_MODE=test\nTEST_BOT_USERNAME=IsolatedMeetTest_bot\nTELEGRAM_BOT_TOKEN=123456789:synthetic-local-test-secret\nBOT_ADMIN_ID=9001\nMINI_APP_ORIGIN=https://mini-test.example.org\nPERSONAL_DATA_POLICY_VERSION=policy-v1\nPERSONAL_DATA_POLICY_URL=https://example.org/privacy\n",encoding="utf8")
    monkeypatch.setattr(launcher,"verify_test_bot",lambda *a:None)
    configs=[];launches=[]
    monkeypatch.setattr(launcher.logging.config,"dictConfig",lambda config:configs.append(config))
    monkeypatch.setattr(uvicorn,"run",lambda app,**kwargs:launches.append(kwargs))
    assert launcher.main(["--env-file",str(env)])==0
    assert configs[0]["disable_existing_loggers"] is True
    assert configs[0]["root"]["handlers"]==["silent"]
    assert launches[0]["access_log"] is False
    assert launches[0]["proxy_headers"] is False
    assert launches[0]["host"]=="127.0.0.1"
