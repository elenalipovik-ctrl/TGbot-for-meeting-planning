"""Explicit loopback-only development app. Never imported by the production launcher."""
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import logging.config
from datetime import datetime, time as daytime, timedelta
from pathlib import Path
import secrets
import time
import uuid
from zoneinfo import ZoneInfo

from fastapi import Request
from pydantic import BaseModel, ConfigDict
from typing import Literal
from starlette.responses import JSONResponse, HTMLResponse
from starlette.staticfiles import StaticFiles

from .api.app import create_app
from .api.config import ApiConfig
from .api.telegram_auth import TelegramIdentity
from .calendar_flow import confirm_with_google
from .database import connect
from .service import BookingService
from .technical_log import configure_logging
from .worker import run_due_jobs

ADMIN=990001
PERSONAS={"first":(990102,"first_preview"),"returning":(990101,"repeat_preview"),"new":(990104,"new_preview"),"pending":(990106,"pending_preview"),"blocked":(990105,"blocked_preview"),"admin":(ADMIN,"admin_preview")}
POLICY="local-preview-v1"
PREVIEW_STARTS={(10,30),(11,30),(13,30),(15,0),(16,30)}

class PreviewCalendar:
    """No external calls: only demo events in process memory."""
    def __init__(self):self.events={}
    def freebusy(self,*args):return []
    def find_matching(self,**kwargs):return kwargs["event_id"] in self.events
    def create_or_find(self,**kwargs):
        self.events[kwargs["event_id"]]=kwargs
        return kwargs["event_id"]
    def delete_or_find_absent(self,event_id,booking_id):
        self.events.pop(event_id,None)

class PersonaInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    persona: Literal["first","returning","new","pending","blocked","admin"]="first"

class LoopbackOnly:
    def __init__(self,app,*,port):
        self.app=app
        self.hosts={f"127.0.0.1:{port}",f"localhost:{port}","127.0.0.1:5173","localhost:5173"}
        self.origins={"http://"+host for host in self.hosts}
    async def __call__(self,scope,receive,send):
        if scope["type"]!="http":return await self.app(scope,receive,send)
        headers={k.decode("latin1"):v.decode("latin1") for k,v in scope["headers"]}
        client=scope.get("client")
        origins=[v.decode("latin1") for k,v in scope["headers"] if k==b"origin"]
        allowed=(client and client[0] in ("127.0.0.1","::1","testclient") and headers.get("host") in self.hosts
            and len(origins)<=1 and all(origin in self.origins for origin in origins)
            and headers.get("sec-fetch-site") not in ("cross-site","same-site"))
        if scope["method"] not in ("GET","HEAD") and not origins:
            allowed=False
        if not allowed:
            result=JSONResponse({"error":{"code":"LOCAL_ONLY","message":"Локальный просмотр доступен только с этого компьютера.","retryable":False,"field_errors":{}},"server_time":int(time.time()),"request_id":uuid.uuid4().hex},status_code=403,headers={"Cache-Control":"no-store"})
            return await result(scope,receive,send)
        forwarded=dict(scope)
        # Local HTTP is allowed ONLY by this dedicated development wrapper.
        forwarded["headers"]=[(k,v) for k,v in scope["headers"] if k!=b"origin"]
        return await self.app(forwarded,receive,send)

def seed(service,calendar,now):
    with connect(service.path) as c:
        if c.execute("SELECT 1 FROM meeting_templates LIMIT 1").fetchone():return
    for name in ("first","returning","blocked","pending"):
        visitor,username=PERSONAS[name]
        service.request_access(visitor,username,now)
        if name!="pending":service.decide_access(ADMIN,visitor,"blocked" if name=="blocked" else "allowed",now)
        if name in ("first","returning"):service.record_consent(visitor,POLICY,now)
    template=service.create_template(ADMIN,"Знакомство и обсуждение задачи","Первая встреча, ваш вопрос и следующие шаги.","Подготовьте вопросы, которые хотите обсудить.",now)
    service.create_template(ADMIN,"Рабочая консультация","Разбор конкретного вопроса и возможных решений.","При необходимости подготовьте материалы к встрече.",now)
    zone=ZoneInfo("Europe/Moscow")
    today=datetime.fromtimestamp(now,zone).date()
    for offset in range(1,7):
        day=today+timedelta(days=offset)
        if day.weekday() in (2,5,6):continue
        start=int(datetime.combine(day,daytime(10,30),zone).timestamp())
        end=int(datetime.combine(day,daytime(18),zone).timestamp())
        service.publish_interval(ADMIN,start,end,"Europe/Moscow",now)
    visitor,username=PERSONAS["returning"]
    for action in ("pending","confirmed","cancelled"):
        slots=service.list_slots(visitor,now,30,"telemost",google_busy=[])
        if not slots:break
        start=slots[0]
        with connect(service.path) as c:
            interval=c.execute("SELECT id FROM availability_intervals WHERE starts_at<=? AND ends_at>=?",(start,start+1800)).fetchone()[0]
        booking=service.create_hold(visitor,template,interval,start,30,"telemost",now,google_busy=[])
        service.submit_request(visitor,booking,name="Тестовый посетитель",username=username,email="visitor@example.test",phone=None,purpose="Обсуждение тестовой задачи",policy_version=POLICY,now=now)
        if action=="confirmed":
            service.admin_decision(ADMIN,booking,True,now)
            confirm_with_google(service,calendar,booking,"https://telemost.yandex.ru/j/local-preview",now)
        elif action=="cancelled":service.cancel_by_visitor(visitor,booking,now)

def create_local_app(path,*,port=8766,assets=None,clock=time.time,logger=None):
    service=BookingService(path,administrator_id=ADMIN,logger=logger)
    service.initialize()
    calendar=PreviewCalendar()
    # Set once for local preview, preserving later edits made in the admin UI.
    with connect(service.path) as c:
        configured=c.execute("SELECT revision FROM setting_revisions WHERE name='booking_start_options'").fetchone()[0]
    if configured==0:
        service.set_start_options(ADMIN,[630,690,810,900,990],int(clock()),expected_revision=0)
    seed(service,calendar,int(clock()))
    app=create_app(service,calendar,ApiConfig(secrets.token_urlsafe(48),POLICY,"https://example.invalid/privacy",()),clock=clock)
    app.add_middleware(LoopbackOnly,port=port)
    @asynccontextmanager
    async def lifespan(application):
        async def tick():
            while True:
                await asyncio.to_thread(run_due_jobs,path,int(clock()),limit=100,calendar=calendar,administrator_id=ADMIN)
                await asyncio.sleep(2)
        worker=asyncio.create_task(tick())
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
    app.router.lifespan_context=lifespan
    @app.post("/local-preview/session",include_in_schema=False)
    def local_session(body:PersonaInput,request:Request):
        now=int(clock())
        app.state.store.rate("preview-session",20,now)
        run_due_jobs(path,now,limit=100,calendar=calendar,administrator_id=ADMIN)
        visitor,username=PERSONAS[body.persona]
        session=app.state.store.exchange(TelegramIdentity(visitor,username,now,uuid.uuid4().hex),now)
        return dict(data={**session,"preview":True,"persona":body.persona},server_time=now,request_id=request.state.request_id,next_cursor=None)
    @app.get("/local-preview/policy",include_in_schema=False)
    def local_policy():
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Условия локальной проверки</title><h1>Локальная проверка интерфейса</h1><p>Это тестовая среда. Вводите только вымышленные контакты. Данные сохраняются в отдельной локальной базе. Сообщения Telegram, приглашения и события Google не отправляются. Этот текст предназначен для теста формы согласия; перед настоящим запуском используется действующая политика сервиса.</p></html>',headers={"Cache-Control":"no-store"})
    if assets is not None:
        app.mount("/app",StaticFiles(directory=assets,html=True),name="preview-frontend")
    return app

def main():
    parser=argparse.ArgumentParser(description="Local-only Mini App preview; no Telegram/Google credentials")
    parser.add_argument("--port",type=int,default=8766)
    args=parser.parse_args()
    if not 1024<=args.port<=65535:raise SystemExit("Invalid local port")
    repository=Path(__file__).resolve().parents[2]
    folder=repository/".secrets"/"miniapp-preview"
    database=folder/"test-v2.sqlite"
    if folder.resolve()!=folder or database.is_symlink() or (database.exists() and database.stat().st_nlink>1):
        raise SystemExit("Unsafe preview storage path")
    assets=repository/"mini_app"/"dist"
    if not (assets/"index.html").is_file():raise SystemExit("Build mini_app first")
    logging.config.dictConfig({"version":1,"disable_existing_loggers":True,"handlers":{"silent":{"class":"logging.NullHandler"}},"root":{"handlers":["silent"],"level":"WARNING"}})
    logger=configure_logging(folder/"api.log")
    import uvicorn
    uvicorn.run(create_local_app(database,port=args.port,assets=assets,logger=logger),host="127.0.0.1",port=args.port,access_log=False,proxy_headers=False,server_header=False,log_config=None)

if __name__=="__main__":main()
