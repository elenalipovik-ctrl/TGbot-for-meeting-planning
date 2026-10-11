"""ASGI boundary: bounded JSON, exact origins, safe responses and metadata-only logs."""

import json
import time
import uuid

from starlette.responses import JSONResponse

from ..technical_log import log_event
from .telegram_auth import unique_object


class Boundary:
    def __init__(self, app, *, origins, clock, logger):
        self.app, self.origins, self.clock, self.logger = app, origins, clock, logger

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scope.setdefault("state", {})["request_id"] = uuid.uuid4().hex
        started = time.monotonic()
        request_id = scope["state"]["request_id"]
        response_started, status = False, 500
        origin_headers = [v.decode("latin-1") for k,v in scope["headers"] if k==b"origin"]
        headers = {k.decode("latin-1"): v.decode("latin-1") for k,v in scope["headers"]}
        origin = origin_headers[0] if len(origin_headers)==1 else None

        async def safe_send(message):
            nonlocal response_started, status
            if message["type"] == "http.response.start":
                response_started, status = True, message["status"]
                stripped = {b"cache-control", b"x-request-id", b"referrer-policy"}
                result = [(k,v) for k,v in message.get("headers", []) if k.lower() not in stripped]
                result += [(b"cache-control",b"no-store"),(b"x-request-id",request_id.encode("ascii")),
                           (b"referrer-policy",b"no-referrer"),(b"x-content-type-options",b"nosniff")]
                if origin in self.origins:
                    result += [(b"access-control-allow-origin", origin.encode("ascii")), (b"vary", b"Origin")]
                message["headers"] = result
            await send(message)

        async def error(http_status, code, text):
            scope["state"]["error_code"] = code
            response = JSONResponse(dict(error=dict(code=code,message=text,retryable=False,field_errors={}),
                                         request_id=request_id,server_time=int(self.clock())), status_code=http_status)
            await response(scope, receive, safe_send)

        try:
            if len(origin_headers)>1 or (origin is not None and origin not in self.origins):
                return await error(403, "ORIGIN_FORBIDDEN", "Этот источник приложения не разрешён.")
            if sum(k==b"authorization" for k,v in scope["headers"])>1 or sum(k==b"idempotency-key" for k,v in scope["headers"])>1:
                return await error(400, "INVALID_REQUEST", "Некорректные заголовки запроса.")
            if scope["method"] == "OPTIONS":
                requested = {v.strip().lower() for v in headers.get("access-control-request-headers", "").split(",") if v.strip()}
                if origin not in self.origins or requested - {"authorization", "content-type", "idempotency-key"}:
                    return await error(403, "ORIGIN_FORBIDDEN", "Этот запрос приложения не разрешён.")
                response = JSONResponse(None, headers={"Access-Control-Allow-Methods":"GET,POST,PATCH,PUT,DELETE,OPTIONS",
                    "Access-Control-Allow-Headers":"Authorization,Content-Type,Idempotency-Key", "Access-Control-Max-Age":"600"})
                return await response(scope, receive, safe_send)
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body)+len(chunk)>16384:
                    return await error(413, "INVALID_REQUEST", "Запрос слишком большой.")
                body.extend(chunk)
                if not message.get("more_body",False):
                    break
            if body:
                if headers.get("content-type", "").split(";",1)[0].strip().lower() != "application/json":
                    return await error(400, "INVALID_REQUEST", "Ожидается JSON-запрос.")
                try:
                    value = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object,
                                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError("number")))
                    if not isinstance(value, dict):
                        raise ValueError("object")
                except (ValueError, UnicodeError, RecursionError):
                    return await error(400, "INVALID_REQUEST", "Некорректный JSON-запрос.")
            delivered = False
            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return dict(type="http.request", body=bytes(body), more_body=False)
                return await receive()
            await self.app(scope, buffered_receive, safe_send)
        except Exception:
            if not response_started:
                await error(500, "INTERNAL_ERROR", "Не удалось выполнить действие. Сообщите номер запроса.")
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            method = scope["method"] if scope["method"] in {"GET","POST","PATCH","PUT","DELETE","OPTIONS"} else "OTHER"
            log_event(self.logger, "api_request", code=scope["state"].get("error_code"),
                      request_id=request_id, component="api", route=route, method=method,
                      status=status, duration_ms=round((time.monotonic()-started)*1000))
