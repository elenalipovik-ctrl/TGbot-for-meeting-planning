"""Export schema without .env, actual bot tokens or persistent databases."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from meeting_bot.api.app import create_app
from meeting_bot.api.config import ApiConfig
from meeting_bot.api.__main__ import DisabledCalendar
from meeting_bot.service import BookingService


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    target=Path(__file__).resolve().parents[1]/"docs"/"mini_app_openapi.json"
    with TemporaryDirectory() as folder:
        service=BookingService(Path(folder)/"schema.sqlite",administrator_id=1)
        service.initialize()
        app=create_app(service,DisabledCalendar(),ApiConfig("schema-only","policy-placeholder","https://example.invalid/privacy",("https://example.invalid",)))
        content=json.dumps(app.openapi(),ensure_ascii=False,indent=2,sort_keys=True)+"\n"
    if args.check:
        if not target.exists() or target.read_text(encoding="utf8")!=content:
            raise SystemExit("Mini App OpenAPI is out of date")
    else:
        target.write_text(content,encoding="utf8")

if __name__=="__main__":main()
