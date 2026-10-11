"""VPS-only API factory. No polling, migration, fixture calendar or sender."""
from pathlib import Path
import sqlite3

from starlette.responses import JSONResponse

from ..database import SCHEMA_VERSION
from ..env_config import ENV_FILE, read_env, require_keys
from ..google_auth import calendar_from_env
from ..release_database import require_schema
from ..runtime_mode import allowed_test_users
from ..service import BookingService
from .app import create_app
from .config import ApiConfig
from .frontend import mount_frontend

ORIGIN = "https://meetcalendar.fin-partner.pro"


def production_config(values):
    require_keys(values, "TELEGRAM_BOT_TOKEN", "BOT_ADMIN_ID", "POLICY_URL", "POLICY_VERSION",
                 "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
    if (values.get("MINI_APP_MODE") != "production"
            or values.get("BOT_STORAGE_IN_RUSSIA") != "true"
            or values.get("BOT_LOCAL_TEST_MODE") != "false"):
        raise ValueError("Explicit VPS production settings are required")
    if values.get("MINI_APP_ORIGIN") != ORIGIN:
        raise ValueError("Use the approved Mini App origin")
    enabled = values.get("MINI_APP_ENABLED")
    if enabled not in ("true", "false"):
        raise ValueError("Choose MINI_APP_ENABLED explicitly")
    if not 0 < int(values["BOT_ADMIN_ID"]) < 2**63:
        raise ValueError("Invalid administrator")
    if Path(values.get("BOT_DB_PATH", "")).resolve() != Path.cwd() / "data" / "bot.sqlite":
        raise ValueError("Use the shared server database")
    restricted = values.get("MINI_APP_RESTRICTED")
    if restricted not in ("true", "false"):
        raise ValueError("Choose MINI_APP_RESTRICTED explicitly")
    audience = allowed_test_users(values)
    if restricted == "true":
        audience = allowed_test_users(dict(values, BOT_SERVER_TEST_MODE="true"))
    return ApiConfig(values["TELEGRAM_BOT_TOKEN"], values["POLICY_VERSION"],
                     values["POLICY_URL"], (ORIGIN,), enabled=enabled == "true",
                     allowed_user_ids=audience)


def build_app(*, env_file=ENV_FILE, logger=None, assets=None):
    values = read_env(env_file)
    config = production_config(values)
    path = Path(values["BOT_DB_PATH"]).resolve()
    require_schema(path, SCHEMA_VERSION)
    service = BookingService(path, administrator_id=int(values["BOT_ADMIN_ID"]), logger=logger)
    calendar = calendar_from_env(env_file, values.get("BOT_CALENDAR_ID") or "primary", logger)
    app = create_app(service, calendar, config)
    mount_frontend(app, assets or Path.cwd() / "mini_app" / "dist")

    @app.get("/health/ready", include_in_schema=False)
    def readiness():
        try:
            require_schema(path, SCHEMA_VERSION)
            ready = True
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            ready = False
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    return app


def main():
    import logging.config
    import uvicorn
    from ..technical_log import configure_logging
    logging.config.dictConfig({"version": 1, "disable_existing_loggers": True,
        "handlers": {"silent": {"class": "logging.NullHandler"}},
        "root": {"handlers": ["silent"], "level": "WARNING"}})
    # Separate rotated API log avoids multiple processes rotating the same file.
    logger = configure_logging(Path.cwd() / "logs" / "api.jsonl")
    app = build_app(logger=logger)
    uvicorn.run(app, host="0.0.0.0", port=8770, workers=1, limit_concurrency=32,
                backlog=64, timeout_keep_alive=5, access_log=False, proxy_headers=False,
                server_header=False, log_config=None)
