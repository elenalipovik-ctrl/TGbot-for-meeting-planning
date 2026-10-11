"""Shared SQLite technical state. Durations/limits are injected, not implicit policy."""

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field

from ..database import connect
from ..unit_of_work import atomic
from .errors import ApiError


@dataclass(frozen=True)
class SecurityPolicy:
    init_max_age: int
    future_skew: int
    session_seconds: int
    operation_seconds: int
    auth_per_minute: int
    reads_per_minute: int
    writes_per_minute: int
    holds_per_minute: int
    google_per_minute: int

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in self.__dict__.values()):
            raise ValueError("Invalid security policy")


@dataclass(frozen=True)
class Principal:
    actor_id: int
    username: str | None = field(repr=False)
    token_hash: str = field(repr=False)
    expires_at: int


class StateStore:
    def __init__(self, path, secret, policy):
        self.path, self.secret, self.policy = path, secret, policy

    def fingerprint(self, purpose, value):
        return hmac.new(self.secret, (purpose + "\n" + value).encode("utf-8"), hashlib.sha256).hexdigest()

    def rate(self, scope, limit, now):
        scope_hash = self.fingerprint("rate", scope)
        with atomic(self.path) as c:
            c.execute("DELETE FROM api_rate_limits WHERE expires_at<=?", (now,))
            rows = c.execute("SELECT bucket,hits FROM api_rate_limits WHERE scope_hash=? AND bucket>?",
                             (scope_hash, now-60)).fetchall()
            if sum(row[1] for row in rows) >= limit:
                retry = max(1, min(row[0] for row in rows)+60-now)
                raise ApiError(429, "RATE_LIMITED", "Слишком много запросов. Повторите немного позже.",
                               retryable=True, retry_after=retry)
            c.execute("INSERT INTO api_rate_limits(scope_hash,bucket,hits,expires_at) VALUES (?,?,1,?) "
                      "ON CONFLICT(scope_hash,bucket) DO UPDATE SET hits=hits+1", (scope_hash, now, now+60))

    def exchange(self, identity, now):
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expiry = now+self.policy.session_seconds
        with atomic(self.path) as c:
            c.execute("DELETE FROM api_auth_exchanges WHERE expires_at<=?", (now,))
            c.execute("DELETE FROM api_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL", (now,))
            if c.execute("SELECT 1 FROM api_auth_exchanges WHERE fingerprint=?", (identity.fingerprint,)).fetchone():
                raise ApiError(401, "AUTH_REPLAY", "Этот вход уже использован. Переоткройте приложение в Telegram.")
            c.execute("INSERT INTO api_auth_exchanges VALUES (?,?)", (identity.fingerprint,
                      identity.auth_date+self.policy.init_max_age+1))
            c.execute("INSERT INTO api_sessions(token_hash,actor_id,username,issued_at,expires_at) "
                      "VALUES (?,?,?,?,?)", (token_hash, identity.telegram_id, identity.username, now, expiry))
        return dict(bearer=token, expires_at=expiry)

    def authenticate(self, authorization, now):
        if not authorization or not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", authorization):
            raise ApiError(401, "AUTH_INVALID", "Откройте приложение через Telegram.")
        token_hash = hashlib.sha256(authorization[7:].encode("ascii")).hexdigest()
        c = connect(self.path)
        try:
            row = c.execute("SELECT actor_id,username,expires_at,revoked_at FROM api_sessions WHERE token_hash=?",
                            (token_hash,)).fetchone()
        finally:
            c.close()
        if row is None or row[2] <= now or row[3] is not None:
            raise ApiError(401, "SESSION_EXPIRED", "Сессия завершилась. Переоткройте приложение в Telegram.")
        return Principal(row[0], row[1], token_hash, row[2])

    def revoke(self, principal, now):
        with atomic(self.path) as c:
            c.execute("UPDATE api_sessions SET revoked_at=? WHERE token_hash=?", (now, principal.token_hash))

    def operation_key(self, actor_id, route, key, body):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", key):
            raise ApiError(400, "INVALID_REQUEST", "Передайте Idempotency-Key длиной от 16 до 128 символов.")
        body_text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return (actor_id, route, self.fingerprint("key", key), self.fingerprint("body", body_text))

    @staticmethod
    def _match(row, request_hash):
        if row and not hmac.compare_digest(row[1], request_hash):
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого действия.")

    def peek(self, identity, now, authorize):
        actor, route, key_hash, request_hash = identity
        with atomic(self.path) as c:
            authorize(c)
            row = c.execute("SELECT id,request_hash,state,result_ref,lease_until FROM api_operations "
                            "WHERE actor_id=? AND route=? AND key_hash=? AND expires_at>?",
                            (actor, route, key_hash, now)).fetchone()
            self._match(row, request_hash)
            if row:
                return dict(operation_id=row[0], state=row[2], ref=json.loads(row[3]),
                            lease_until=row[4])
        return None

    def execute(self, identity, now, authorize, apply, *, external=False):
        actor, route, key_hash, request_hash = identity
        with atomic(self.path) as c:
            authorize(c)
            c.execute("DELETE FROM api_operations WHERE expires_at<=?", (now,))
            row = c.execute("SELECT id,request_hash,state,result_ref,lease_until FROM api_operations "
                            "WHERE actor_id=? AND route=? AND key_hash=?", (actor, route, key_hash)).fetchone()
            self._match(row, request_hash)
            if row:
                result = dict(operation_id=row[0], state=row[2], ref=json.loads(row[3]))
                if row[2] == "done" or (row[4] is not None and row[4] > now):
                    return result, None
                lease = secrets.token_hex(16)
                c.execute("UPDATE api_operations SET lease_token=?,lease_until=? WHERE id=?",
                          (lease, now+120, row[0]))
                return result, lease
            # Local business changes, jobs, notices and operation result commit together.
            ref = apply()
            operation_id = uuid.uuid4().hex
            lease = secrets.token_hex(16) if external else None
            state = "accepted" if external else "done"
            c.execute("INSERT INTO api_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, actor, route, key_hash, request_hash, state,
                 json.dumps(ref, separators=(",", ":")), now, now+self.policy.operation_seconds,
                 lease, now+120 if external else None))
            return dict(operation_id=operation_id, state=state, ref=ref), lease

    def finish_external(self, operation_id, lease, now, *, success):
        with atomic(self.path) as c:
            c.execute("UPDATE api_operations SET state=?,lease_token=NULL,lease_until=NULL "
                      "WHERE id=? AND lease_token=?", ("done" if success else "accepted", operation_id, lease))

    def operation(self, principal, operation_id, now, authorize):
        with atomic(self.path) as c:
            authorize(c)
            row = c.execute("SELECT actor_id,state,result_ref FROM api_operations WHERE id=? AND expires_at>?",
                            (operation_id, now)).fetchone()
            if row is None or row[0] != principal.actor_id:
                raise ApiError(404, "NOT_FOUND", "Объект не найден.")
            return dict(operation_id=operation_id, state=row[1], ref=json.loads(row[2]))
