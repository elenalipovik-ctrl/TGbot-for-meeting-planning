"""Transport-independent input rules shared by chat and Mini App services.

Do not replace these checks with a framework's email/URL type: accepting a
different set of values in HTTP and chat would change the product rules.
"""

import re
from urllib.parse import urlparse


MAX_VISITOR_TEXT = 500
MAX_ADMIN_TEXT = 1000
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9 ()-]{7,25}$")
SERVICE_HOSTS = {"telemost": "telemost.yandex.ru", "zoom": "zoom.us"}


def valid_visitor_text(value: str) -> bool:
    return 1 <= len(value.strip()) <= MAX_VISITOR_TEXT


def valid_email(value: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(value.strip()))


def valid_phone(value: str) -> bool:
    """Validate a supplied phone; skipping an optional phone is a separate action."""
    return bool(PHONE_PATTERN.fullmatch(value.strip()))


def valid_video_url(value: str, service: str) -> bool:
    """Accept HTTPS on the selected provider's host, as in the existing chat."""
    try:
        parsed = urlparse(value.strip())
        hostname = (parsed.hostname or "").lower()
        root = SERVICE_HOSTS[service]
        if service == "telemost":
            host_ok = hostname == root
        else:
            host_ok = hostname == root or hostname.endswith("." + root)
        return (parsed.scheme == "https" and host_ok and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and bool(parsed.path.strip("/")))
    except (ValueError, KeyError):
        return False
