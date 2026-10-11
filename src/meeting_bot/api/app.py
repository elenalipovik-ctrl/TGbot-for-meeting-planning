"""Explicit FastAPI application factory. Does not read .env, migrate or deploy."""

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import time
from dataclasses import asdict
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from ..technical_log import log_event
from ..calendar_flow import confirm_with_google
from ..commands import Commands
from ..database import SCHEMA_VERSION
from ..drafts import DraftStore, require_allowed
from ..errors import AccessDenied, RuleError, SlotUnavailable, StateConflict, StorageUnavailable
from ..google_calendar import CalendarError
from ..integration_status import check_google, google_status
from ..mini_app_queries import MiniAppQueries
from ..rules import Availability, BusyInterval, ReviewSchedule, ReviewWindow, available_slots, allowed_durations, decision_deadline
from ..validation import valid_video_url
from .boundary import Boundary
from .errors import ApiError
from .models import (
    AccessDecisionInput, AccessRequestInput, AvailabilityInput, BlockInput, ConfirmInput,
    ConsentInput, DayInput, DecisionInput, DraftInput, ErrorResponse, HoldInput, ImpactInput,
    Input, ProfileInput, SelectionInput, StartOptionsInput, ScheduleInput, SessionInput, SubmitInput, SuccessResponse,
    TemplateEditInput, TemplateInput, VersionInput, VideoInput,
)
from .outputs import (Envelope, SessionData, BootstrapData, TemplateData, ProfileData, DraftData, BookingData,
    BookingSummary, HoldData, SlotsData, VisitorData, AccessRequestData, IntervalData, AvailabilityResult,
    ImpactData, BlockData, BlockResult, ScheduleData, GoogleData, AlertsData, StatisticsData, LogoutData, StartOptionsData, SchedulePreviewData)
from .state import StateStore
from .telegram_auth import verify_init_data


def create_app(service, calendar, config, *, clock=time.time):
    if not service.path.is_file():
        raise RuntimeError("Explicit initialized test database is required")
    c = service._connect()
    try:
        if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise RuntimeError("API schema does not match application")
    finally:
        c.close()
    def require_enabled():
        if not app.state.mini_app_enabled:
            raise ApiError(503, "MINI_APP_DISABLED",
                "Приложение временно отключено. Вернитесь в чат бота, чтобы продолжить.")

    app = FastAPI(dependencies=[Depends(require_enabled)], title="Meeting Planning Mini App API", version="1.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None,
                  responses={code: {"model": ErrorResponse} for code in (400,401,403,404,409,410,413,422,429,500,503)})
    secret = hmac.digest(b"MeetingMiniAppTechnicalState", config.bot_token.encode("utf-8"), "sha256")
    store = StateStore(service.path, secret, config.security)
    views, drafts = MiniAppQueries(service), DraftStore(service)
    commands = Commands(service, calendar, policy_version=config.policy_version)
    now = lambda: int(clock())
    prefix = "/api/v1"
    bearer = HTTPBearer(auto_error=False)
    private = [Depends(bearer)]
    app.state.store, app.state.service = store, service
    app.state.mini_app_enabled = config.enabled
    app.add_middleware(Boundary, origins=config.allowed_origins, clock=clock, logger=service.logger)

    def response(request, data, *, next_cursor=None, status=200):
        payload = dict(data=data, server_time=now(), request_id=request.state.request_id, next_cursor=next_cursor)
        request.scope["route"].response_model.model_validate(payload)
        return JSONResponse(payload, status_code=status)

    @app.exception_handler(ApiError)
    async def api_error(request, error):
        request.state.error_code = error.code
        headers = {"Retry-After":str(error.retry_after)} if error.retry_after is not None else None
        return JSONResponse(dict(error=dict(code=error.code,message=error.message,
            retryable=error.retryable,field_errors={}), server_time=now(), request_id=request.state.request_id),
            status_code=error.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Pydantic errors can contain raw input, ctx/ValueError and arbitrary key names.
        # Only fixed known field names and fixed messages cross this boundary.
        known = {"revision","template_id","interval_id","starts_at","ends_at","duration_minutes", "video_service",
            "contact_name","contact_email","contact_phone","purpose","name","email","phone","policy_version",
            "accepted","confirmed","approve","status","video_url","time_zone","windows","weekday",
            "starts_minute","ends_minute","description","instructions","is_active","manual_exception",
            "local_date","kind","init_data","limit","cursor","days","from_time","to_time","booking_id"}
        fields = {}
        extra = False
        for item in error.errors():
            field = next((str(part) for part in reversed(item.get("loc", ())) if str(part) in known), "request")
            fields[field] = "Проверьте значение поля."
            extra |= item.get("type") == "extra_forbidden"
        code = "INVALID_REQUEST" if extra else "VALIDATION_ERROR"
        request.state.error_code = code
        return JSONResponse(dict(error=dict(code=code,message="Проверьте данные формы.",retryable=False,field_errors=fields),
                                 server_time=now(),request_id=request.state.request_id), status_code=400 if extra else 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return await api_error(request, ApiError(error.status_code, "NOT_FOUND" if error.status_code==404 else "INVALID_REQUEST",
                                                "Объект не найден." if error.status_code==404 else "Некорректный запрос."))

    @app.exception_handler(RuleError)
    async def rule_error(request, error):
        if "удержан" in str(error).lower() and "истек" in str(error).lower().replace("ё","е"):
            result = ApiError(410,"HOLD_EXPIRED","Время удержания истекло. Выберите время заново.")
        elif isinstance(error, StateConflict):
            result = ApiError(409,"STATE_CHANGED",str(error))
        elif isinstance(error, SlotUnavailable):
            result = ApiError(409,"SLOT_UNAVAILABLE",str(error))
        elif isinstance(error, AccessDenied):
            result = ApiError(404,"NOT_FOUND","Объект не найден.")
        elif "согласие" in str(error):
            result = ApiError(422,"POLICY_REQUIRED",str(error))
        elif "час" in str(error) and "Отмена" in str(error):
            result = ApiError(422,"CANCELLATION_TOO_LATE",str(error))
        else:
            result = ApiError(422,"VALIDATION_ERROR",str(error))
        return await api_error(request, result)

    @app.exception_handler(StorageUnavailable)
    @app.exception_handler(sqlite3.OperationalError)
    async def storage_error(request, error):
        return await api_error(request, ApiError(503,"STORAGE_BUSY","Сервис временно занят. Повторите действие позже.",retryable=True,retry_after=2))

    @app.exception_handler(CalendarError)
    async def calendar_error(request, error):
        return await api_error(request, ApiError(503,"CALENDAR_UNAVAILABLE",
            "Не удалось проверить Google Календарь. Перечитайте состояние заявки и повторите позже.", retryable=True,retry_after=5))

    def require_user(user_id):
        if config.allowed_user_ids is not None and user_id not in config.allowed_user_ids:
            raise ApiError(403,"TEST_ACCESS_DENIED","Закрытая проверка доступна только владельцу приложения.")

    def check(principal, *, admin=False, bootstrap=False, connection=None):
        require_enabled()
        require_user(principal.actor_id)
        c = connection or service._connect()
        try:
            row = c.execute("SELECT expires_at,revoked_at FROM api_sessions WHERE token_hash=?", (principal.token_hash,)).fetchone()
            if row is None or row[0] <= now() or row[1] is not None:
                raise ApiError(401,"SESSION_EXPIRED","Сессия завершилась. Переоткройте приложение в Telegram.")
            if admin:
                if principal.actor_id != service.administrator_id:
                    raise ApiError(403,"ADMIN_REQUIRED","Действие доступно только администратору.")
            elif not bootstrap:
                row = c.execute("SELECT access_status FROM visitors WHERE telegram_id=?", (principal.actor_id,)).fetchone()
                if row is None or row[0] != "allowed":
                    code = "ACCESS_BLOCKED" if row and row[0]=="blocked" else "ACCESS_PENDING"
                    raise ApiError(403,code,"Доступ к этому разделу закрыт. Проверьте статус допуска.")
        finally:
            if connection is None:
                c.close()

    def actor(request, *, admin=False, bootstrap=False, query=()):
        if set(request.query_params)-set(query) or len(request.query_params.multi_items()) != len(request.query_params):
            raise ApiError(400,"INVALID_REQUEST","Некорректные параметры запроса.")
        principal = store.authenticate(request.headers.get("authorization"), now())
        check(principal, admin=admin, bootstrap=bootstrap)
        if request.method == "GET":
            store.rate("read:"+principal.token_hash, config.security.reads_per_minute, now())
        else:
            store.rate("write:"+str(principal.actor_id), config.security.writes_per_minute, now())
        return principal

    def policy(version):
        if version != config.policy_version:
            raise ApiError(422,"POLICY_REQUIRED","Ознакомьтесь с текущей версией политики и подтвердите согласие.")

    def offset(cursor, scope):
        if cursor is None:
            return 0
        try:
            if len(cursor)>256:
                raise ValueError("size")
            encoded, supplied = cursor.split(".")
            expected = store.fingerprint("cursor", scope+":"+encoded)
            if not hmac.compare_digest(supplied, expected):
                raise ValueError("signature")
            value = base64.urlsafe_b64decode(encoded+"="*((-len(encoded))%4)).decode("ascii")
            if not re.fullmatch(r"[0-9]{1,8}", value):
                raise ValueError("offset")
            return int(value)
        except (ValueError, UnicodeError):
            raise ApiError(400,"INVALID_REQUEST","Некорректная страница списка.") from None

    def next_page(position, scope):
        value = base64.urlsafe_b64encode(str(position).encode("ascii")).decode("ascii").rstrip("=")
        return value+"."+store.fingerprint("cursor",scope+":"+value)

    def listed(request, principal, scope, limit, cursor, read, serialize):
        context = str(principal.actor_id)+":"+scope
        start = offset(cursor, context)
        # Fetch one sentinel without exceeding the shared layer's max limit=100.
        rows = read(limit, start)
        more = read(1,start+limit) if len(rows)==limit else []
        return response(request,[serialize(row) for row in rows],next_cursor=next_page(start+limit,context) if more else None)

    def draft_view(principal):
        snapshot = drafts.read(principal.actor_id, principal.actor_id, now())
        if snapshot.values is None:
            return dict(revision=snapshot.revision, draft=None)
        r = snapshot.values
        return dict(revision=snapshot.revision, draft=dict(step=r[0],template_id=r[1],duration_minutes=r[2],video_service=r[3],
            selected_day=r[4],booking_id=r[5],contact_name=r[6],contact_email=r[7],contact_phone=r[8],purpose=r[9],
            expires_at=r[10],used_saved_contacts=bool(r[11]),hold_expires_at=snapshot.hold_expires_at))

    def bootstrap(principal):
        status = views.access_status(principal.actor_id)
        administrative = principal.actor_id == service.administrator_id
        return dict(telegram_id=str(principal.actor_id),role="admin" if administrative else "visitor", access_status=status,
            username=principal.username, policy_version=config.policy_version,policy_url=config.policy_url,
            time_zone=service.review_schedule().time_zone, durations={name:list(allowed_durations(name)) for name in ("telemost","zoom")},
            capabilities=dict(admin=administrative,booking=status=="allowed",test_environment=config.test_environment),
            allowed_actions=(["request_access"] if status in ("new","rejected") else []))

    def resolve(principal, ref, *, admin=False):
        kind = ref["kind"]
        check(principal, admin=admin, bootstrap=kind=="access")
        if kind == "booking":
            return views.booking_details(principal.actor_id, ref["id"], now(), administrative=admin)
        if kind == "hold":
            return dict(booking=views.booking_details(principal.actor_id, ref["id"], now()), current=draft_view(principal))
        if kind == "draft":
            return draft_view(principal)
        if kind == "profile":
            return views.profile(principal.actor_id)
        if kind == "access":
            return bootstrap(principal)
        if kind == "google":
            return google_status(service, principal.actor_id, now())
        if kind == "start_options":
            return service.start_options(principal.actor_id)
        if kind == "schedule":
            return views.schedule_details(principal.actor_id)
        if kind == "visitor":
            rows = views._read(principal.actor_id,"SELECT telegram_id,username,access_status,revision FROM visitors WHERE telegram_id=?",(ref["id"],),admin=True)
            if not rows:
                raise ApiError(404,"NOT_FOUND","Объект не найден.")
            r=rows[0]
            return dict(telegram_id=str(r[0]),username=r[1],access_status=r[2],revision=r[3])
        if kind == "template":
            rows=views._read(principal.actor_id,"SELECT id,name,description,instructions,is_active,revision FROM meeting_templates WHERE id=?",(ref["id"],),admin=True)
            if not rows:
                raise ApiError(404,"NOT_FOUND","Объект не найден.")
            return template_view(rows[0])
        if kind == "availability":
            return dict(interval_id=ref.get("id"), booking_ids=ref.get("booking_ids",[]), preview=False)
        if kind == "block":
            return dict(block_id=ref["id"], booking_ids=ref.get("booking_ids",[]), released=ref.get("released",False))
        raise RuntimeError("Unknown command result")

    def template_view(r):
        return dict(id=r[0],name=r[1],description=r[2],instructions=r[3],is_active=bool(r[4]),revision=r[5])

    def mutate(request, principal, body, apply, *, admin=False, bootstrap_access=False, prepare=None, external=None):
        route = request.method+" "+request.url.path
        values = body.model_dump(mode="json",exclude_unset=True)
        identity = store.operation_key(principal.actor_id,route,request.headers.get("idempotency-key"),values)
        authorize = lambda c: check(principal,admin=admin,bootstrap=bootstrap_access,connection=c)
        existing = store.peek(identity,now(),authorize)
        prepared = None
        if existing is None and prepare is not None:
            prepared = prepare()
        result, lease = store.execute(identity,now(),authorize,lambda: apply(prepared),external=external is not None)
        if lease is not None:
            succeeded = False
            try:
                external(result["ref"])
                succeeded = True
            finally:
                store.finish_external(result["operation_id"],lease,now(),success=succeeded)
            result["state"]="done"
        data = resolve(principal,result["ref"],admin=admin)
        data.update(operation_id=result["operation_id"],operation_state=result["state"])
        return response(request,data,status=202 if result["state"]=="accepted" else 200)

    @app.post(prefix+"/session",response_model=Envelope[SessionData])
    def session(body: SessionInput, request: Request):
        if request.query_params:
            raise ApiError(400,"INVALID_REQUEST","Передайте данные входа в теле запроса.")
        peer = request.client.host if request.client else "unknown"
        store.rate("auth:"+peer,config.security.auth_per_minute,now())
        identity = verify_init_data(body.init_data,config.bot_token,now=now(),max_age=config.security.init_max_age,
                                    future_skew=config.security.future_skew,
                                    on_reject=lambda code: log_event(service.logger,"telegram_auth_rejected",
                                        code=code,request_id=request.state.request_id,component="api"))
        require_user(identity.telegram_id)
        return response(request,store.exchange(identity,now()))

    @app.delete(prefix+"/session",response_model=Envelope[LogoutData],dependencies=private)
    def logout(request: Request):
        principal=actor(request,bootstrap=True)
        store.revoke(principal,now())
        return response(request,dict(revoked=True))

    @app.get(prefix+"/bootstrap",response_model=Envelope[BootstrapData],dependencies=private)
    def get_bootstrap(request: Request):
        return response(request,bootstrap(actor(request,bootstrap=True)))

    @app.post(prefix+"/me/access-requests",response_model=Envelope[BootstrapData],dependencies=private)
    def request_access(body: AccessRequestInput, request: Request):
        principal=actor(request,bootstrap=True)
        policy(body.policy_version)
        def apply(_):
            if views.access_status(principal.actor_id)=="blocked":
                raise ApiError(403,"ACCESS_BLOCKED","Доступ к этому разделу закрыт.")
            service.request_access(principal.actor_id,principal.username,now())
            service.record_consent(principal.actor_id,config.policy_version,now())
            return dict(kind="access")
        return mutate(request,principal,body,apply,bootstrap_access=True)

    @app.post(prefix+"/me/consents",response_model=Envelope[DraftData],dependencies=private)
    def consent(body: ConsentInput, request: Request):
        principal=actor(request)
        policy(body.policy_version)
        def apply(_):
            if body.action=="accepted":
                commands.consent(principal.actor_id,now(),revision=body.revision)
            else:
                commands.withdraw_consent(principal.actor_id,now(),revision=body.revision)
            return dict(kind="draft")
        return mutate(request,principal,body,apply)

    @app.get(prefix+"/templates",response_model=Envelope[list[TemplateData]],dependencies=private)
    def templates(request: Request,limit: int=Query(20,ge=1,le=100),cursor: str|None=None):
        principal=actor(request,query=("limit","cursor"))
        return listed(request,principal,"templates",limit,cursor,
            lambda size,start: views.templates(principal.actor_id,limit=size,offset=start),template_view)

    @app.get(prefix+"/slots",response_model=Envelope[SlotsData],dependencies=private)
    def slots(request: Request, template_id: int=Query(...,gt=0), duration_minutes: int=Query(...,ge=15,le=90),
              video_service: Literal["telemost","zoom"]="telemost",
              from_time: int|None=Query(None,ge=0),to_time: int|None=Query(None,ge=0)):
        principal=actor(request,query=("template_id","duration_minutes","video_service","from_time","to_time"))
        start_bound=now() if from_time is None else from_time
        end_bound=now()+7*86400 if to_time is None else to_time
        if start_bound<now() or end_bound>now()+7*86400 or start_bound>=end_bound:
            raise ApiError(422,"VALIDATION_ERROR","Выберите диапазон в ближайшие 7 дней.")
        if not views._read(principal.actor_id,"SELECT id FROM meeting_templates WHERE id=? AND is_active=1",(template_id,)):
            raise ApiError(404,"NOT_FOUND","Тип встречи не найден.")
        if duration_minutes not in allowed_durations(video_service):
            raise ApiError(422,"VALIDATION_ERROR","Эта длительность недоступна для выбранного сервиса.")
        busy=calendar.freebusy(now()-1800,now()+7*86400+1800)
        starts=service.list_slots(principal.actor_id,now(),duration_minutes,video_service,google_busy=busy)
        intervals=views._read(principal.actor_id,"SELECT id,starts_at,ends_at,time_zone,is_published,is_manual_exception FROM availability_intervals WHERE is_published=1 AND ends_at>?",(now(),))
        schedule=service.review_schedule()
        result=[]
        for start in starts:
            if not start_bound<=start<end_bound:
                continue
            matching=next((r for r in intervals if start in available_slots(now(),[Availability(*r)],duration_minutes,video_service,schedule,[])),None)
            if matching:
                result.append(dict(starts_at=start,ends_at=start+duration_minutes*60,interval_id=matching[0],time_zone=matching[3]))
        return response(request,dict(slots=result,checked_at=now()))

    @app.get(prefix+"/me/draft",response_model=Envelope[DraftData],dependencies=private)
    def get_draft(request: Request):
        return response(request,draft_view(actor(request)))

    @app.patch(prefix+"/me/draft",response_model=Envelope[DraftData],dependencies=private)
    def select_draft(body: SelectionInput,request: Request):
        principal=actor(request)
        def apply(_):
            drafts.select(principal.actor_id,principal.actor_id,now(),expected_revision=body.revision,**body.model_dump(exclude={"revision"}))
            return dict(kind="draft")
        return mutate(request,principal,body,apply)

    @app.delete(prefix+"/me/draft",response_model=Envelope[DraftData],dependencies=private)
    def reset_draft(body: ConfirmInput,request: Request):
        principal=actor(request)
        def apply(_):
            drafts.reset(principal.actor_id,principal.actor_id,now(),expected_revision=body.revision)
            return dict(kind="draft")
        return mutate(request,principal,body,apply)

    @app.post(prefix+"/holds",response_model=Envelope[HoldData],dependencies=private)
    def hold(body: HoldInput,request: Request):
        principal=actor(request)
        store.rate("hold:"+str(principal.actor_id),config.security.holds_per_minute,now())
        def prepare():
            if drafts.read(principal.actor_id,principal.actor_id,now()).revision!=body.revision:
                raise StateConflict("Запись уже изменена. Обновите экран.")
            return calendar.freebusy(body.starts_at-1800,body.starts_at+body.duration_minutes*60+1800)
        def apply(busy):
            booking_id=commands.create_hold(principal.actor_id,body.template_id,body.interval_id,body.starts_at,
                body.duration_minutes,body.video_service,now(),revision=body.revision,google_busy=busy)
            return dict(kind="hold",id=booking_id)
        return mutate(request,principal,body,apply,prepare=prepare)

    @app.patch(prefix+"/holds/{booking_id}/draft",response_model=Envelope[DraftData],dependencies=private)
    def patch_draft(booking_id: int,body: DraftInput,request: Request):
        principal=actor(request)
        def apply(_):
            fields=body.model_dump(exclude_unset=True,exclude={"revision"})
            drafts.patch_contacts(principal.actor_id,principal.actor_id,booking_id,now(),expected_revision=body.revision,**fields)
            return dict(kind="draft")
        return mutate(request,principal,body,apply)

    @app.delete(prefix+"/holds/{booking_id}",response_model=Envelope[DraftData],dependencies=private)
    def release(booking_id: int,body: ConfirmInput,request: Request):
        principal=actor(request)
        def apply(_):
            booking=views.booking_details(principal.actor_id,booking_id,now())
            snapshot=drafts.read(principal.actor_id,principal.actor_id,now())
            if booking["status"]!="held" or not snapshot.values or snapshot.values[5]!=booking_id:
                raise StateConflict("Удержание уже изменено. Обновите экран.")
            drafts.reset(principal.actor_id,principal.actor_id,now(),expected_revision=body.revision)
            return dict(kind="draft")
        return mutate(request,principal,body,apply)

    @app.post(prefix+"/holds/{booking_id}/use-saved-contacts",response_model=Envelope[DraftData],dependencies=private)
    def use_contacts(booking_id: int,body: ConfirmInput,request: Request):
        principal=actor(request)
        def apply(_):
            drafts.use_saved_contacts(principal.actor_id,principal.actor_id,booking_id,now(),expected_revision=body.revision)
            return dict(kind="draft")
        return mutate(request,principal,body,apply)

    @app.post(prefix+"/holds/{booking_id}/submit",response_model=Envelope[BookingData],dependencies=private)
    def submit(booking_id: int,body: SubmitInput,request: Request):
        principal=actor(request)
        policy(body.policy_version)
        def prepare():
            snapshot=drafts.read(principal.actor_id,principal.actor_id,now())
            if snapshot.revision!=body.revision:
                raise StateConflict("Запись уже изменена. Обновите экран.")
            booking=views.booking_details(principal.actor_id,booking_id,now())
            return calendar.freebusy(booking["starts_at"]-1800,booking["ends_at"]+1800)
        def apply(busy):
            commands.submit(principal.actor_id,booking_id,username=principal.username,now=now(),revision=body.revision,google_busy=busy)
            return dict(kind="booking",id=booking_id)
        return mutate(request,principal,body,apply,prepare=prepare)

    def booking_list(request,principal,administrative,status,limit,cursor,from_time,to_time,group=None):
        return listed(request,principal,"bookings:"+str(administrative)+":"+str(status)+":"+str(from_time)+":"+str(to_time)+":"+str(group),limit,cursor,
            lambda size,start: views.bookings(principal.actor_id,administrative=administrative,status=status,from_time=from_time,to_time=to_time,group=group,now=now(),limit=size,offset=start),
            lambda r: dict(id=r[0],starts_at=r[1],status=r[2],revision=r[3],ends_at=r[4],time_zone=r[5],template_name=r[6],video_service=r[7],decision_deadline_at=r[8]))

    @app.get(prefix+"/me/bookings",response_model=Envelope[list[BookingSummary]],dependencies=private)
    def own_bookings(request: Request,status: str|None=None,limit: int=Query(20,ge=1,le=100),cursor: str|None=None,
                     from_time: int|None=Query(None,ge=0,le=4102444800),to_time: int|None=Query(None,ge=0,le=4102444800),group: Literal["review","upcoming","history"]|None=None):
        return booking_list(request,actor(request,query=("status","limit","cursor","from_time","to_time","group")),False,status,limit,cursor,from_time,to_time,group)

    @app.get(prefix+"/me/bookings/{booking_id}",response_model=Envelope[BookingData],dependencies=private)
    def own_booking(booking_id: int,request: Request):
        principal=actor(request)
        return response(request,views.booking_details(principal.actor_id,booking_id,now()))

    @app.post(prefix+"/me/bookings/{booking_id}/cancel",response_model=Envelope[BookingData],dependencies=private)
    def visitor_cancel(booking_id: int,body: ConfirmInput,request: Request):
        principal=actor(request)
        def apply(_):
            commands.cancel(principal.actor_id,booking_id,now(),revision=body.revision)
            return dict(kind="booking",id=booking_id)
        return mutate(request,principal,body,apply)

    @app.get(prefix+"/me/profile",response_model=Envelope[ProfileData],dependencies=private)
    def get_profile(request: Request):
        principal=actor(request)
        return response(request,views.profile(principal.actor_id))

    @app.patch(prefix+"/me/profile",response_model=Envelope[ProfileData],dependencies=private)
    def patch_profile(body: ProfileInput,request: Request):
        principal=actor(request)
        def apply(_):
            service.correct_saved_contacts(principal.actor_id,principal.actor_id,name=body.name,email=body.email,
                phone=body.phone,now=now(),expected_revision=body.revision)
            return dict(kind="profile")
        return mutate(request,principal,body,apply)

    @app.delete(prefix+"/me/profile/contacts",response_model=Envelope[ProfileData],dependencies=private)
    def clear_profile(body: ConfirmInput,request: Request):
        principal=actor(request)
        def apply(_):
            service.clear_saved_contacts(principal.actor_id,principal.actor_id,now(),expected_revision=body.revision)
            return dict(kind="profile")
        return mutate(request,principal,body,apply)

    @app.get(prefix+"/admin/access-requests",response_model=Envelope[list[AccessRequestData]],dependencies=private)
    def access_requests(request: Request,limit: int=Query(20,ge=1,le=100),cursor: str|None=None):
        principal=actor(request,admin=True,query=("limit","cursor"))
        return listed(request,principal,"access_requests",limit,cursor,
            lambda size,start: views.access_requests(principal.actor_id,limit=size,offset=start),
            lambda r: dict(id=r[0],telegram_id=str(r[1]),username=r[2],requested_at=r[3],revision=r[4]))

    @app.get(prefix+"/admin/visitors",response_model=Envelope[list[VisitorData]],dependencies=private)
    def visitors(request: Request,status: str|None=None,limit: int=Query(20,ge=1,le=100),cursor: str|None=None):
        principal=actor(request,admin=True,query=("status","limit","cursor"))
        return listed(request,principal,"visitors:"+str(status),limit,cursor,
            lambda size,start: views.filtered_visitors(principal.actor_id,status=status,limit=size,offset=start),
            lambda r: dict(telegram_id=str(r[0]),username=r[1],access_status=r[2],revision=r[3]))

    @app.post(prefix+"/admin/visitors/{visitor_id}/access",response_model=Envelope[VisitorData],dependencies=private)
    def decide_access(visitor_id: int,body: AccessDecisionInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            service.decide_access(principal.actor_id,visitor_id,body.status,now(),expected_revision=body.revision)
            return dict(kind="visitor",id=visitor_id)
        return mutate(request,principal,body,apply,admin=True)

    @app.get(prefix+"/admin/bookings",response_model=Envelope[list[BookingSummary]],dependencies=private)
    def admin_bookings(request: Request,status: str|None=None,limit: int=Query(20,ge=1,le=100),cursor: str|None=None,
                       from_time: int|None=Query(None,ge=0,le=4102444800),to_time: int|None=Query(None,ge=0,le=4102444800)):
        return booking_list(request,actor(request,admin=True,query=("status","limit","cursor","from_time","to_time")),True,status,limit,cursor,from_time,to_time)

    @app.get(prefix+"/admin/bookings/{booking_id}",response_model=Envelope[BookingData],dependencies=private)
    def admin_booking(booking_id: int,request: Request):
        principal=actor(request,admin=True)
        return response(request,views.booking_details(principal.actor_id,booking_id,now(),administrative=True))

    @app.post(prefix+"/admin/bookings/{booking_id}/decision",response_model=Envelope[BookingData],dependencies=private)
    def decision(booking_id: int,body: DecisionInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            commands.decide(principal.actor_id,booking_id,body.approve,now(),revision=body.revision)
            return dict(kind="booking",id=booking_id)
        return mutate(request,principal,body,apply,admin=True)

    def calendar_command(request,principal,booking_id,body,retry=False):
        def apply(_):
            booking=views.booking_details(principal.actor_id,booking_id,now(),administrative=True)
            if booking["revision"]!=body.revision:
                raise StateConflict("Заявка уже изменена. Обновите экран.")
            url=booking["video_url"] if retry else body.video_url
            if not url or not valid_video_url(url,booking["video_service"]):
                raise RuleError("Пришлите корректную HTTPS-ссылку выбранного видеосервиса.")
            if retry and "retry_calendar" not in booking["allowed_actions"]:
                raise StateConflict("Заявка не готова к повторной проверке.")
            service.begin_calendar_creation(booking_id,url,now(),expected_revision=body.revision)
            service.reserve_google_event_id(booking_id,now())
            return dict(kind="booking",id=booking_id)
        def external(ref):
            booking=service.calendar_booking(ref["id"])
            confirm_with_google(service,calendar,ref["id"],booking.video_url,now())
        return mutate(request,principal,body,apply,admin=True,external=external)

    @app.post(prefix+"/admin/bookings/{booking_id}/video-link",response_model=Envelope[BookingData],responses={202:{"model":Envelope[BookingData]}},dependencies=private)
    def video_link(booking_id: int,body: VideoInput,request: Request):
        return calendar_command(request,actor(request,admin=True),booking_id,body)

    @app.post(prefix+"/admin/bookings/{booking_id}/retry-calendar",response_model=Envelope[BookingData],responses={202:{"model":Envelope[BookingData]}},dependencies=private)
    def retry_calendar(booking_id: int,body: VersionInput,request: Request):
        return calendar_command(request,actor(request,admin=True),booking_id,body,retry=True)

    @app.post(prefix+"/admin/bookings/{booking_id}/cancel",response_model=Envelope[BookingData],dependencies=private)
    def admin_cancel(booking_id: int,body: ConfirmInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            commands.cancel(principal.actor_id,booking_id,now(),revision=body.revision,administrative=True)
            return dict(kind="booking",id=booking_id)
        return mutate(request,principal,body,apply,admin=True)

    @app.get(prefix+"/admin/availability",response_model=Envelope[list[IntervalData]],dependencies=private)
    def availability(request: Request,limit: int=Query(20,ge=1,le=100),cursor: str|None=None):
        principal=actor(request,admin=True,query=("limit","cursor"))
        return listed(request,principal,"availability",limit,cursor,
            lambda size,start: views.intervals(principal.actor_id,limit=size,offset=start),
            lambda r: dict(id=r[0],starts_at=r[1],ends_at=r[2],time_zone=r[3],is_published=bool(r[4]),manual_exception=bool(r[5]),revision=r[6]))

    @app.post(prefix+"/admin/availability",response_model=Envelope[AvailabilityResult],dependencies=private)
    def publish(body: AvailabilityInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            identity=service.publish_interval(principal.actor_id,body.starts_at,body.ends_at,body.time_zone,now(),manual_exception=body.manual_exception)
            return dict(kind="availability",id=identity)
        return mutate(request,principal,body,apply,admin=True)

    @app.post(prefix+"/admin/availability/impact",response_model=Envelope[ImpactData],dependencies=private)
    def impact(body: ImpactInput,request: Request):
        principal=actor(request,admin=True)
        return response(request,views.impact(principal.actor_id,**body.model_dump()))

    @app.post(prefix+"/admin/availability/{interval_id}/close",response_model=Envelope[AvailabilityResult],dependencies=private)
    def close_interval(interval_id: int,body: ConfirmInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            affected=service.close_interval(principal.actor_id,interval_id,now(),expected_revision=body.revision)
            return dict(kind="availability",id=interval_id,booking_ids=affected)
        return mutate(request,principal,body,apply,admin=True)

    @app.post(prefix+"/admin/availability/close-day",response_model=Envelope[AvailabilityResult],dependencies=private)
    def close_day(body: DayInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            affected=service.close_day(principal.actor_id,body.local_date,body.time_zone,now())
            return dict(kind="availability",booking_ids=affected)
        return mutate(request,principal,body,apply,admin=True)

    @app.get(prefix+"/admin/manual-blocks",response_model=Envelope[list[BlockData]],dependencies=private)
    def blocks(request: Request,limit: int=Query(20,ge=1,le=100),cursor: str|None=None):
        principal=actor(request,admin=True,query=("limit","cursor"))
        return listed(request,principal,"blocks",limit,cursor,
            lambda size,start: views.manual_blocks(principal.actor_id,limit=size,offset=start),
            lambda r: dict(id=r[0],starts_at=r[1],ends_at=r[2],revision=r[3]))

    @app.post(prefix+"/admin/manual-blocks",response_model=Envelope[BlockResult],dependencies=private)
    def block(body: BlockInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            identity,affected=service.block_time(principal.actor_id,body.starts_at,body.ends_at,now())
            return dict(kind="block",id=identity,booking_ids=affected)
        return mutate(request,principal,body,apply,admin=True)

    @app.delete(prefix+"/admin/manual-blocks/{block_id}",response_model=Envelope[BlockResult],dependencies=private)
    def unblock(block_id: int,body: ConfirmInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            service.unblock_time(principal.actor_id,block_id,now(),expected_revision=body.revision)
            return dict(kind="block",id=block_id,released=True)
        return mutate(request,principal,body,apply,admin=True)

    @app.get(prefix+"/admin/templates",response_model=Envelope[list[TemplateData]],dependencies=private)
    def admin_templates(request: Request,limit: int=Query(20,ge=1,le=100),cursor: str|None=None):
        principal=actor(request,admin=True,query=("limit","cursor"))
        return listed(request,principal,"admin_templates",limit,cursor,
            lambda size,start: views.templates(principal.actor_id,administrative=True,limit=size,offset=start),template_view)

    @app.post(prefix+"/admin/templates",response_model=Envelope[TemplateData],dependencies=private)
    def create_template(body: TemplateInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            identity=service.create_template(principal.actor_id,body.name,body.description,body.instructions,now())
            return dict(kind="template",id=identity)
        return mutate(request,principal,body,apply,admin=True)

    @app.patch(prefix+"/admin/templates/{template_id}",response_model=Envelope[TemplateData],dependencies=private)
    def edit_template(template_id: int,body: TemplateEditInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            service.update_template(principal.actor_id,template_id,now=now(),expected_revision=body.revision,
                **body.model_dump(exclude={"revision"}))
            return dict(kind="template",id=template_id)
        return mutate(request,principal,body,apply,admin=True)

    @app.get(prefix+"/admin/review-schedule",response_model=Envelope[ScheduleData],dependencies=private)
    def schedule(request: Request):
        principal=actor(request,admin=True)
        return response(request,views.schedule_details(principal.actor_id))

    @app.put(prefix+"/admin/review-schedule",response_model=Envelope[ScheduleData],dependencies=private)
    def edit_schedule(body: ScheduleInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            try:
                value=ReviewSchedule(body.time_zone,tuple(ReviewWindow(**w.model_dump()) for w in body.windows))
            except ValueError:
                raise RuleError("Проверьте часы рассмотрения: окна не должны пересекаться.") from None
            service.set_review_schedule(principal.actor_id,value,now(),expected_revision=body.revision)
            return dict(kind="schedule")
        return mutate(request,principal,body,apply,admin=True)

    @app.get(prefix+"/admin/start-options",response_model=Envelope[StartOptionsData],dependencies=private)
    def start_options(request: Request):
        principal=actor(request,admin=True)
        return response(request,service.start_options(principal.actor_id))

    @app.put(prefix+"/admin/start-options",response_model=Envelope[StartOptionsData],dependencies=private)
    def edit_start_options(body: StartOptionsInput,request: Request):
        principal=actor(request,admin=True)
        def apply(_):
            service.set_start_options(principal.actor_id,body.minutes,now(),expected_revision=body.revision)
            return dict(kind="start_options")
        return mutate(request,principal,body,apply,admin=True)

    @app.post(prefix+"/admin/review-schedule/preview",response_model=Envelope[SchedulePreviewData],dependencies=private)
    def preview_schedule(body: ScheduleInput,request: Request):
        actor(request,admin=True)
        try:
            schedule=ReviewSchedule(body.time_zone,tuple(ReviewWindow(**w.model_dump()) for w in body.windows))
            submitted=now()
            deadline=decision_deadline(submitted,schedule)
        except ValueError:
            raise RuleError("Проверьте часы рассмотрения.") from None
        return response(request,dict(submitted_at=submitted,decision_deadline_at=deadline))

    @app.get(prefix+"/admin/statistics",response_model=Envelope[StatisticsData],dependencies=private)
    def statistics(request: Request,days: int=Query(7,ge=7,le=30)):
        principal=actor(request,admin=True,query=("days",))
        return response(request,views.stats(principal.actor_id,days,now()))

    @app.get(prefix+"/admin/alerts",response_model=Envelope[AlertsData],dependencies=private)
    def alerts(request: Request):
        principal=actor(request,admin=True)
        return response(request,views.alerts(principal.actor_id))

    @app.get(prefix+"/admin/integrations/google",response_model=Envelope[GoogleData],dependencies=private)
    def google(request: Request):
        principal=actor(request,admin=True)
        return response(request,google_status(service,principal.actor_id,now()))

    @app.post(prefix+"/admin/integrations/google/check",response_model=Envelope[GoogleData],responses={202:{"model":Envelope[GoogleData]}},dependencies=private)
    def google_check(body: Input,request: Request):
        principal=actor(request,admin=True)
        store.rate("google:"+str(principal.actor_id),config.security.google_per_minute,now())
        return mutate(request,principal,body,lambda _: dict(kind="google"),admin=True,
            external=lambda _: check_google(service,calendar,principal.actor_id,now()))

    @app.get(prefix+"/operations/{operation_id}",response_model=Envelope[BookingData|HoldData|DraftData|ProfileData|BootstrapData|GoogleData|ScheduleData|StartOptionsData|VisitorData|TemplateData|AvailabilityResult|BlockResult],responses={202:{"model":Envelope[BookingData|HoldData|DraftData|ProfileData|BootstrapData|GoogleData|ScheduleData|VisitorData|TemplateData|AvailabilityResult|BlockResult]}},dependencies=private)
    def operation(operation_id: str,request: Request):
        principal=actor(request,bootstrap=True)
        administrative=principal.actor_id==service.administrator_id
        item=store.operation(principal,operation_id,now(),lambda c: check(principal,bootstrap=True,connection=c))
        data=resolve(principal,item["ref"],admin=administrative and item["ref"]["kind"]!="access")
        data.update(operation_id=item["operation_id"],operation_state=item["state"])
        return response(request,data,status=202 if item["state"]=="accepted" else 200)

    from .smoke import install_smoke_page
    install_smoke_page(app)
    return app
