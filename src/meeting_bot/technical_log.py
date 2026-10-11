"""Small structured log with an explicit allowlist of fields."""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.event,
        }
        for key in ("code", "record_id", "request_id", "component", "route", "method", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(path: Path) -> logging.Logger:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("meeting_bot.technical")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(SafeFormatter())
    logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger | None, event: str, *, code: str | None = None,
              record_id: int | None = None, request_id: str | None = None,
              component: str | None = None, route: str | None = None, method: str | None = None,
              status: int | None = None, duration_ms: int | None = None) -> None:
    if logger is None:
        return
    from .unit_of_work import defer_log
    if defer_log(logger, event, dict(code=code, record_id=record_id, request_id=request_id,
            component=component, route=route, method=method, status=status, duration_ms=duration_ms)):
        return
    # Only fixed event/code values from application code are passed here.
    logger.info("", extra={"event": event, "code": code, "record_id": record_id,
        "request_id": request_id, "component": component, "route": route, "method": method,
        "status": status, "duration_ms": duration_ms})
