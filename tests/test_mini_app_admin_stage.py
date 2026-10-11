import uuid
import pytest
pytest.importorskip('fastapi')
from test_mini_app_api import h,P,VISITOR,ADMIN,at
from meeting_bot.database import connect,initialize_database,SCHEMA_VERSION
from meeting_bot.errors import SlotUnavailable,StateConflict
from meeting_bot.rules import decision_deadline,ReviewSchedule,ReviewWindow


def command(h,path,body,user=ADMIN,method='POST',key=None):
    return h.command(path,body,user=user,method=method,key=key or str(uuid.uuid4()))


def test_admin_setting_shared_with_chat_and_hold_validation(h):
    h.login();h.login(ADMIN)
    hold,_=h.ready_hold()
    c=hold['current']
    release=command(h,'/me/draft',{'revision':c['revision'],'confirmed':True},user=VISITOR,method='DELETE')
    assert release.status_code==200
    assert h.service.start_options(ADMIN)['minutes']==[]
    result=command(h,'/admin/start-options',{'revision':0,'minutes':[1020]},method='PUT',key='start-options-fixed-key')
    assert result.status_code==200,result.text
    revision=result.json()['data']['revision']
    assert h.service.start_options(ADMIN)=={'revision':revision,'minutes':[1020]}
    slots=h.service.list_slots(VISITOR,h.now,30,'telemost',google_busy=[])
    assert slots==[at(5,17)]
    with connect(h.path) as db:
        interval=db.execute('SELECT id FROM availability_intervals LIMIT 1').fetchone()[0]
        template=db.execute('SELECT id FROM meeting_templates LIMIT 1').fetchone()[0]
    with pytest.raises(SlotUnavailable):
        h.service.create_hold(VISITOR,template,interval,at(5,16),30,'telemost',h.now,google_busy=[])
    h.service.create_hold(VISITOR,template,interval,at(5,17),30,'telemost',h.now,google_busy=[])
    replay=command(h,'/admin/start-options',{'revision':0,'minutes':[1020]},method='PUT',key='start-options-fixed-key')
    assert replay.status_code==200 and replay.json()['data']['revision']==revision
    assert command(h,'/admin/start-options',{'revision':0,'minutes':[630]},method='PUT').status_code==409
    assert command(h,'/admin/start-options',{'revision':revision,'minutes':[630]},method='PUT',user=VISITOR).status_code==403
    assert h.client.get(P+'/admin/start-options',headers=h.headers[VISITOR]).status_code==403


@pytest.mark.parametrize('minutes',[[631],[630,630],[True],[-15],[1440]])
def test_admin_setting_invalid_choices_preserve_revision(h,minutes):
    h.login(ADMIN)
    r=command(h,'/admin/start-options',{'revision':0,'minutes':minutes},method='PUT')
    assert r.status_code==422,r.text
    assert h.service.start_options(ADMIN)=={'revision':0,'minutes':[]}


def test_review_preview_uses_common_rules_without_saving(h):
    h.login(ADMIN);h.login()
    before=h.client.get(P+'/admin/review-schedule',headers=h.headers[ADMIN]).json()['data']
    body={'revision':before['revision'],'time_zone':'Europe/Moscow','windows':[{'weekday':0,'starts_minute':600,'ends_minute':1080}]}
    result=command(h,'/admin/review-schedule/preview',body)
    assert result.status_code==200,result.text
    assert result.json()['data']['decision_deadline_at']==decision_deadline(h.now,ReviewSchedule('Europe/Moscow',(ReviewWindow(0,600,1080),)))
    assert h.client.get(P+'/admin/review-schedule',headers=h.headers[ADMIN]).json()['data']==before
    assert command(h,'/admin/review-schedule/preview',body,user=VISITOR).status_code==403


def test_admin_alerts_link_failed_delivery_to_booking(h):
    h.login();h.login(ADMIN)
    hold,_=h.ready_hold();identity=hold['booking']['id']
    with connect(h.path) as c:
        c.execute("INSERT INTO notifications(channel,kind,status,dedup_key,booking_id) VALUES ('telegram','admin_new_request','failed','ma4-test-failure',?)",(identity,))
    result=h.client.get(P+'/admin/alerts',headers=h.headers[ADMIN])
    assert result.status_code==200,result.text
    assert result.json()['data']['notification_booking_ids']==[identity]
    assert result.json()['data']['notification_failures']==1


def test_v10_upgrade_preserves_existing_records_and_defaults(tmp_path,monkeypatch):
    import meeting_bot.database as module
    path=tmp_path/'upgrade.sqlite'
    monkeypatch.setattr(module,'SCHEMA_VERSION',10)
    initialize_database(path)
    with connect(path) as c:
        c.execute("INSERT INTO visitors(telegram_id,created_at,last_used_at) VALUES (123,1,1)")
    monkeypatch.setattr(module,'SCHEMA_VERSION',SCHEMA_VERSION)
    initialize_database(path);initialize_database(path)
    with connect(path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==11
        assert c.execute('SELECT telegram_id FROM visitors').fetchall()==[(123,)]
        assert c.execute('SELECT minute FROM booking_start_options').fetchall()==[]
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
