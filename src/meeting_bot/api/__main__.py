"""Explicit isolated test launcher. Never defaults to the working .env or data."""
import argparse
import json
import logging.config
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

from ..env_config import read_env
from ..google_calendar import CalendarUnavailable
from ..service import BookingService
from ..technical_log import configure_logging
from .app import create_app
from .config import ApiConfig


class DisabledCalendar:
    def freebusy(self, *args):
        raise CalendarUnavailable("Test calendar is not configured")
    def find_matching(self, **kwargs):
        raise CalendarUnavailable("Test calendar is not configured")
    def create_or_find(self, **kwargs):
        raise CalendarUnavailable("Test calendar is not configured")


def test_settings(env_file):
    env_file=Path(env_file).resolve()
    if not env_file.name.startswith(".env-miniapp-test") or not env_file.is_file():
        raise ValueError("Use an explicit .env-miniapp-test file")
    values=read_env(env_file)
    if values.get("MINI_APP_MODE")!="test":
        raise ValueError("MINI_APP_MODE=test is required")
    username=values.get("TEST_BOT_USERNAME", "").lstrip("@")
    if username.lower()=="mymeetplanning_bot" or not re.fullmatch(r"[A-Za-z0-9_]{5,32}",username):
        raise ValueError("A separate test bot is required")
    token=values.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}",token):
        raise ValueError("Test TELEGRAM_BOT_TOKEN is missing")
    folder=env_file.parent/".secrets"/"miniapp-test"
    # No user-controlled database/log path; resolve symlinks before enforcing isolation.
    folder=folder.resolve()
    expected=(env_file.parent.resolve()/".secrets"/"miniapp-test")
    if folder!=expected:
        raise ValueError("Test storage must not be a redirected directory")
    database=folder/"test.sqlite"
    if database.is_symlink() or (database.exists() and database.stat().st_nlink>1):
        raise ValueError("Test database must not be a symlink")
    administrator=int(values.get("BOT_ADMIN_ID", "0"))
    if administrator<=0:
        raise ValueError("Test administrator ID is required")
    origin=values.get("MINI_APP_ORIGIN", "")
    enabled=values.get("MINI_APP_ENABLED", "true")
    if enabled not in ("true", "false"):
        raise ValueError("MINI_APP_ENABLED must be true or false")
    config=ApiConfig(token,values.get("PERSONAL_DATA_POLICY_VERSION", ""),
        values.get("PERSONAL_DATA_POLICY_URL", ""),(origin,),enabled=enabled=="true")
    return env_file,values,username,database,administrator,config


def verify_test_bot(token,expected):
    # urllib does not emit request URLs; never expose raw exceptions from a token URL.
    try:
        request=Request("https://api.telegram.org/bot"+token+"/getMe",data=b"",method="POST")
        with urlopen(request,timeout=15) as response:
            result=json.loads(response.read(16384))
        actual=result["result"]["username"]
        if not result.get("ok") or actual.lower()!=expected.lower() or actual.lower()=="mymeetplanning_bot":
            raise ValueError("mismatch")
    except Exception:
        raise ValueError("Test bot could not be verified; working bot is forbidden") from None


def main(argv=None):
    parser=argparse.ArgumentParser(description="Run the isolated Mini App test API")
    parser.add_argument("--env-file",required=True,type=Path)
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--frontend",action="store_true",help="Serve the built mini_app/dist at /app/")
    args=parser.parse_args(argv)
    try:
        if not 1024<=args.port<=65535:
            raise ValueError("Invalid port")
        env_file,values,username,database,administrator,config=test_settings(args.env_file)
        verify_test_bot(config.bot_token,username)
        logging.config.dictConfig({"version":1,"disable_existing_loggers":True,
            "handlers":{"silent":{"class":"logging.NullHandler"}},"root":{"handlers":["silent"],"level":"WARNING"}})
        logger=configure_logging(database.parent/"api.log")
        service=BookingService(database,administrator_id=administrator,logger=logger)
        service.initialize()
        calendar=DisabledCalendar()
        if values.get("GOOGLE_CALENDAR_ID"):
            if values.get("MINI_APP_CALENDAR_IS_TEST")!="true" or values["GOOGLE_CALENDAR_ID"]=="primary":
                raise ValueError("Use an explicitly isolated test calendar")
            from ..google_auth import calendar_from_env
            calendar=calendar_from_env(env_file,values["GOOGLE_CALENDAR_ID"],logger=logger)
        import uvicorn
        app=create_app(service,calendar,config)
        if args.frontend:
            from .frontend import mount_frontend
            mount_frontend(app,Path(__file__).resolve().parents[3]/"mini_app"/"dist")
        uvicorn.run(app,host="127.0.0.1",port=args.port,
            access_log=False,proxy_headers=False,server_header=False,log_config=None)
    except (ValueError, RuntimeError, OSError):
        print("Mini App test API could not start. Check the isolated test configuration; secrets are not logged.",file=sys.stderr)
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
