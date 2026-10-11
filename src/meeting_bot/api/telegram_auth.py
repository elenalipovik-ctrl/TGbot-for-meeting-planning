"""Telegram HMAC verification; policy durations are supplied by configuration.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from .errors import ApiError


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None = field(repr=False)
    auth_date: int
    fingerprint: str


def invalid():
    return ApiError(401, "AUTH_INVALID", "Не удалось проверить вход Telegram. Откройте приложение заново.")


def verify_init_data(raw, bot_token, *, now, max_age, future_skew, on_reject=None):
    phase = "AUTH_FORMAT"
    try:
        if not isinstance(raw, str) or not 1 <= len(raw.encode("utf-8")) <= 16384:
            raise ValueError("size")
        if any(ord(char) < 32 or ord(char) > 126 for char in raw):
            raise ValueError("encoding")
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError("percent")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=32)
        data = unique_object(pairs)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in data):
            raise ValueError("key")
        supplied = data.pop("hash")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
            raise ValueError("hash")
        phase = "AUTH_SIGNATURE"
        check = "\n".join(key + "=" + value for key, value in sorted(data.items()))
        secret = hmac.digest(b"WebAppData", bot_token.encode("utf-8"), "sha256")
        digest = hmac.digest(secret, check.encode("utf-8"), "sha256")
        if not hmac.compare_digest(digest, bytes.fromhex(supplied)):
            raise ValueError("signature")
        phase = "AUTH_DATE"
        if not re.fullmatch(r"[0-9]{1,12}", data["auth_date"]):
            raise ValueError("date")
        issued = int(data["auth_date"])
        if now-issued > max_age:
            phase = "AUTH_TOO_OLD"
            raise ValueError("expiry")
        if issued-now > future_skew:
            phase = "AUTH_IN_FUTURE"
            raise ValueError("expiry")
        phase = "AUTH_USER"
        user = json.loads(data["user"], object_pairs_hook=unique_object)
        if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
            raise ValueError("identity")
        if user.get("is_bot") is True:
            raise ValueError("bot")
        username = user.get("username")
        if username is not None and (not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username)):
            raise ValueError("username")
        # Canonical signed values prevent replay by query reordering/re-encoding.
        fingerprint = hashlib.sha256(digest + check.encode("utf-8")).hexdigest()
        return TelegramIdentity(user["id"], username, issued, fingerprint)
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        if on_reject is not None:
            on_reject(phase)
        raise invalid() from None
