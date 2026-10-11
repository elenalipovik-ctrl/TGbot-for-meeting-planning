import uuid
import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from meeting_bot.local_preview import create_local_app
from test_mini_app_api import h, P, VISITOR


def command(h,path,body,method='PATCH'):
    return h.command(path,body,method=method,key=str(uuid.uuid4()))


def test_prehold_selection_cas_reset_and_active_hold(h):
    h.login()
    data,_=h.ready_hold()
    revision=data['current']['revision']
    result=command(h,'/me/draft',{'revision':revision,'template_id':data['current']['draft']['template_id']})
    assert result.status_code==409
    result=command(h,'/me/draft',{'revision':revision,'confirmed':True},'DELETE')
    assert result.status_code==200,result.text
    revision=result.json()['data']['revision']
    assert result.json()['data']['draft'] is None
    template=data['current']['draft']['template_id']
    result=command(h,'/me/draft',{'revision':revision,'template_id':template})
    assert result.status_code==200,result.text
    assert result.json()['data']['draft']['template_id']==template
    assert command(h,'/me/draft',{'revision':revision,'template_id':template}).status_code==409
    revision=result.json()['data']['revision']
    assert command(h,'/me/draft',{'revision':revision,'template_id':template,'duration_minutes':45,'video_service':'zoom'}).status_code==422
    assert command(h,'/me/draft',{'revision':revision,'template_id':template,'duration_minutes':None}).status_code==200


def test_production_does_not_expose_local_identity(h):
    assert h.client.post('/local-preview/session',json={'persona':'first'}).status_code==404
    assert h.client.get(P+'/bootstrap?devTelegramId=1').status_code==400


@pytest.fixture
def preview(tmp_path):
    app=create_local_app(tmp_path/'isolated.sqlite')
    return TestClient(app,base_url='http://127.0.0.1:8766')


def test_preview_restricts_host_origin_and_identity(preview):
    endpoint='/local-preview/session'
    for headers in ({'host':'evil.test','origin':'http://127.0.0.1:8766'}, {'origin':'https://evil.test'}, {'origin':'http://127.0.0.1:8766','sec-fetch-site':'cross-site'}, {}):
        assert preview.post(endpoint,json={'persona':'first'},headers=headers).status_code==403
    headers={'origin':'http://127.0.0.1:8766'}
    assert preview.post(endpoint,json={'persona':'first','telegram_id':1},headers=headers).status_code==400
    result=preview.post(endpoint,json={'persona':'first'},headers=headers)
    assert result.status_code==200,result.text
    assert result.json()['data']['preview'] is True
    auth={'Authorization':'Bearer '+result.json()['data']['bearer']}
    assert preview.get(P+'/bootstrap',headers=auth).status_code==200
    assert preview.get(P+'/admin/bookings',headers=auth).status_code==403


@pytest.mark.parametrize('persona,expected',[('first','allowed'),('returning','allowed'),('pending','pending'),('blocked','blocked')])
def test_preview_persona_access_and_group_filter(preview,persona,expected):
    result=preview.post('/local-preview/session',json={'persona':persona},headers={'origin':'http://127.0.0.1:8766'})
    assert result.status_code==200,result.text
    auth={'Authorization':'Bearer '+result.json()['data']['bearer']}
    bootstrap=preview.get(P+'/bootstrap',headers=auth).json()['data']
    assert bootstrap['access_status']==expected
    if persona=='returning':
        for group,status in [('review','pending'),('upcoming','confirmed'),('history','cancelled_by_visitor')]:
            response=preview.get(P+'/me/bookings',params={'group':group},headers=auth)
            assert response.status_code==200,response.text
            assert response.json()['data']
            assert all(b['status']==status for b in response.json()['data'])
    if persona in ('blocked','pending'):
        assert preview.get(P+'/me/profile',headers=auth).status_code==403


@pytest.mark.parametrize('duration',[15,30,45,60,90])
def test_preview_slots_follow_owner_schedule(preview,duration):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from meeting_bot.local_preview import PREVIEW_STARTS
    session=preview.post('/local-preview/session',json={'persona':'first'},headers={'origin':'http://127.0.0.1:8766'})
    auth={'Authorization':'Bearer '+session.json()['data']['bearer']}
    template=preview.get(P+'/templates',headers=auth).json()['data'][0]['id']
    result=preview.get(P+'/slots',params={'template_id':template,'duration_minutes':duration,'video_service':'telemost'},headers=auth)
    assert result.status_code==200,result.text
    slots=result.json()['data']['slots']
    assert slots
    zone=ZoneInfo('Europe/Moscow')
    for slot in slots:
        start=datetime.fromtimestamp(slot['starts_at'],zone)
        end=datetime.fromtimestamp(slot['ends_at'],zone)
        assert start.weekday() in (0,1,3,4)
        assert (start.hour,start.minute) in PREVIEW_STARTS
        assert end.date()==start.date() and (end.hour,end.minute)<=(18,0)
    assert all(sum(datetime.fromtimestamp(s['starts_at'],zone).date()==day for s in slots)<=5 for day in {datetime.fromtimestamp(s['starts_at'],zone).date() for s in slots})
