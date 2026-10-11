"""Owner-only real Telegram authentication with synthetic data and no deliveries.

This entry point never polls Telegram, starts a notification worker, reads the
working .env implicitly, or connects to Google. Expose only this app, never the
local-preview persona endpoint, through the agreed HTTPS endpoint.
"""
import argparse
import json
import logging.config
from email.utils import parsedate_to_datetime
from datetime import datetime, time as daytime, timedelta
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .api.app import create_app
from .api.config import ApiConfig
from .api.frontend import mount_frontend
from .database import connect
from .env_config import read_env
from .local_preview import PreviewCalendar
from .service import BookingService
from .technical_log import configure_logging


def owner_settings(env_file):
    env_file = Path(env_file).resolve()
    if not env_file.name.startswith(".env-miniapp-owner") or not env_file.is_file():
        raise ValueError("Use an explicit .env-miniapp-owner file")
    values = read_env(env_file)
    if values.get("MINI_APP_MODE") != "owner-preview":
        raise ValueError("Owner preview mode is required")
    if any(key.startswith("GOOGLE_") and value for key, value in values.items()):
        raise ValueError("Google credentials are forbidden in owner preview")
    token = values.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Missing bot credential")
    administrator = int(values.get("BOT_ADMIN_ID", "0"))
    folder = env_file.parent / ".secrets" / "miniapp-owner"
    database = folder / "synthetic.sqlite"
    if folder.resolve() != folder or database.is_symlink() or (database.exists() and database.stat().st_nlink > 1):
        raise ValueError("Unsafe owner preview storage")
    origin = values.get("MINI_APP_ORIGIN", "")
    config = ApiConfig(token, "owner-preview-v1", origin + "/owner-preview/policy", (origin,),
                       allowed_user_ids=frozenset({administrator}), test_environment=True)
    return database, administrator, config


def telegram_clock(date_header, *, monotonic=time.monotonic, system_time=time.time):
    """Preview-only clock anchored to the verified HTTPS response, never user input.

    Do not alter the OS clock or enlarge Telegram authentication age limits.
    Monotonic elapsed time prevents later wall-clock corrections changing TTLs.
    """
    try:
        value = parsedate_to_datetime(date_header)
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Expected UTC HTTP date")
        epoch = value.timestamp()
        if abs(epoch - system_time()) > 900:
            raise ValueError("Clock discrepancy exceeds preview limit")
        anchor = monotonic()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Verified Telegram server time is required") from None
    return lambda: epoch + monotonic() - anchor


def verify_owner_bot(token):
    # Never expose an upstream exception, request URL, or returned body.
    try:
        started = time.monotonic()
        request = Request("https://api.telegram.org/bot" + token + "/getMe", data=b"", method="POST")
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(16384))
            date_header = response.headers.get("Date")
        if result.get("ok") is not True or result["result"]["username"].lower() != "mymeetplanning_bot":
            raise ValueError("Unexpected bot")
        if time.monotonic() - started > 5:
            raise ValueError("Server time request took too long")
        return telegram_clock(date_header)
    except Exception:
        raise ValueError("Owner bot verification failed") from None


def seed_owner(service, now):
    """Only synthetic fixtures; reuse all booking/availability business commands."""
    owner = service.administrator_id
    with connect(service.path) as c:
        if c.execute("SELECT 1 FROM meeting_templates LIMIT 1").fetchone():
            return
    service.request_access(owner, None, now)
    service.decide_access(owner, owner, "allowed", now)
    template = service.create_template(owner, "Знакомство и обсуждение задачи",
                            "Первая встреча, ваш вопрос и следующие шаги.", "Подготовьте вопросы.", now)
    service.create_template(owner, "Рабочая консультация",
                            "Разбор конкретного вопроса и возможных решений.", "Подготовьте материалы.", now)
    service.set_start_options(owner, [630, 690, 810, 900, 990], now, expected_revision=0)
    zone = ZoneInfo("Europe/Moscow")
    today = datetime.fromtimestamp(now, zone).date()
    for offset in range(1, 7):
        day = today + timedelta(days=offset)
        if day.weekday() in (2, 5, 6):
            continue
        service.publish_interval(owner, int(datetime.combine(day, daytime(10, 30), zone).timestamp()),
                                 int(datetime.combine(day, daytime(18), zone).timestamp()), "Europe/Moscow", now)

    visitor = 990101 if owner != 990101 else 990102
    service.request_access(visitor, "synthetic_visitor", now)
    service.decide_access(owner, visitor, "allowed", now)
    service.record_consent(visitor, "owner-preview-v1", now)
    slots = service.list_slots(visitor, now, 30, "telemost", google_busy=[])
    if slots:
        start = slots[0]
        with connect(service.path) as c:
            interval = c.execute("SELECT id FROM availability_intervals WHERE starts_at<=? AND ends_at>=?", (start, start+1800)).fetchone()[0]
        booking = service.create_hold(visitor, template, interval, start, 30, "telemost", now, google_busy=[])
        service.submit_request(visitor, booking, name="Тестовый посетитель", username="synthetic_visitor",
            email="visitor@example.test", phone=None, purpose="Тестовая заявка", policy_version="owner-preview-v1", now=now)


def create_owner_app(database, administrator, config, *, assets=None, clock=time.time, logger=None):
    if config.allowed_user_ids != frozenset({administrator}) or not config.test_environment:
        raise ValueError("Owner-only test configuration is required")
    service = BookingService(database, administrator_id=administrator, logger=logger)
    service.initialize()
    seed_owner(service, int(clock()))
    app = create_app(service, PreviewCalendar(), config, clock=clock)
    # No persona endpoint, notification worker, polling, or Google client is installed.
    from starlette.responses import HTMLResponse
    @app.get("/owner-preview/policy", include_in_schema=False)
    def policy():
        return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Тестовая проверка</title><h1>Закрытая проверка Mini App</h1><p>Используйте вымышленные контакты. Данные сохраняются в отдельной тестовой базе. Встречи Google и сообщения Telegram не отправляются. Это тест формы согласия, а не действующая политика сервиса.</p></html>', headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","Content-Security-Policy":"default-src 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org"})
    if assets is not None:
        mount_frontend(app, assets)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Owner-only Mini App preview using the existing bot identity")
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args(argv)
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("Invalid port")
        database, administrator, config = owner_settings(args.env_file)
        assets = Path(__file__).resolve().parents[2] / "mini_app" / "dist"
        if not (assets / "index.html").is_file():
            raise ValueError("Build the frontend first")
        clock = verify_owner_bot(config.bot_token)
        logging.config.dictConfig({"version":1,"disable_existing_loggers":True,
            "handlers":{"silent":{"class":"logging.NullHandler"}},"root":{"handlers":["silent"],"level":"WARNING"}})
        logger = configure_logging(database.parent / "api.log")
        import uvicorn
        uvicorn.run(create_owner_app(database, administrator, config, assets=assets, logger=logger, clock=clock),
                    host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False,
                    server_header=False, log_config=None)
    except (ValueError, RuntimeError, OSError):
        print("Owner preview could not start. Check isolated configuration; secrets are not logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
