"""CI-only image smoke: network=none, temporary fake settings/data, no Telegram/Google."""
import json
import hashlib
import hmac
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from urllib.request import Request
from unittest.mock import patch
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen


def main():
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="release-image-") as folder:
        os.chdir(folder)
        for name in ("data", "logs"):
            Path(name).mkdir(mode=0o700)
        values = dict(TELEGRAM_BOT_TOKEN="000000:synthetic-image-check-only",
            BOT_ADMIN_ID="900001", GOOGLE_CLIENT_ID="synthetic", GOOGLE_CLIENT_SECRET="synthetic",
            GOOGLE_REFRESH_TOKEN="synthetic", POLICY_URL="https://meetcalendar.fin-partner.pro/privacy/",
            POLICY_VERSION="image-check", BOT_STORAGE_IN_RUSSIA="true", BOT_LOCAL_TEST_MODE="false",
            BOT_SERVER_TEST_MODE="false", BOT_DB_PATH="data/bot.sqlite", BOT_LOG_PATH="logs/technical.jsonl",
            MINI_APP_MODE="production", MINI_APP_ORIGIN="https://meetcalendar.fin-partner.pro",
            MINI_APP_ENABLED="true", MINI_APP_RESTRICTED="true", BOT_SERVER_TEST_VISITOR_ID="900002")
        Path(".env").write_text("".join(key + "=" + json.dumps(value) + "\n" for key, value in values.items()))
        Path(".env").chmod(0o600)
        # Import only after cwd is fixed: ENV_FILE is intentionally process-local.
        from meeting_bot.database import initialize_database
        from meeting_bot.api.production import build_app
        sys.path.insert(0, "/app/scripts")
        from check_server_readiness import main as readiness
        initialize_database(Path("data/bot.sqlite"))
        readiness()
        from meeting_bot.service import BookingService
        from meeting_bot.rules import ReviewSchedule, ReviewWindow
        from meeting_bot.drafts import DraftStore
        now = int(time.time())
        service = BookingService(Path('data/bot.sqlite'), administrator_id=900001)
        service.request_access(900002, 'image_check', now)
        service.decide_access(900001, 900002, 'allowed', now)
        service.set_review_schedule(900001, ReviewSchedule('Europe/Moscow',
            tuple(ReviewWindow(day, 0, 1440) for day in range(7))), now)
        template = service.create_template(900001, 'Synthetic smoke', 'Test', 'Test', now)
        zone = ZoneInfo('Europe/Moscow')
        day = datetime.fromtimestamp(now, zone).date() + timedelta(days=2)
        while day.weekday() in (2, 5, 6):
            day += timedelta(days=1)
        start = int(datetime(day.year, day.month, day.day, 16, tzinfo=zone).timestamp())
        interval = service.publish_interval(900001, start, start + 7200, 'Europe/Moscow', now)
        class OfflineCalendar:
            def freebusy(self, *_):
                return []
        with patch('meeting_bot.api.production.calendar_from_env', return_value=OfflineCalendar()):
            app = build_app(assets=Path('/app/mini_app/dist'))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_config=None,
                                 proxy_headers=False, server_header=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("Image API startup failed")
            base = "http://127.0.0.1:" + str(port)
            with urlopen(base + "/health/ready", timeout=5) as response:
                assert json.load(response) == {"ready": True}
            with urlopen(base + "/app/", timeout=5) as response:
                assert "telegram.org" in response.headers["Content-Security-Policy"]
                assert b'<div id="root">' in response.read()
            try:
                urlopen(base + "/api/v1/bootstrap", timeout=5)
            except HTTPError as error:
                assert error.code == 401
            else:
                raise RuntimeError("Image API allowed anonymous access")
            def request(path, body, method='POST', headers=None):
                request = Request(base + '/api/v1' + path, data=json.dumps(body).encode(),
                    method=method, headers={'Content-Type': 'application/json', **(headers or {})})
                with urlopen(request, timeout=5) as response:
                    return json.load(response)['data']
            identity = dict(auth_date=str(int(time.time())), query_id='offline-image-only',
                            user=json.dumps(dict(id=900002, username='image_check')))
            secret = hmac.digest(b'WebAppData', values['TELEGRAM_BOT_TOKEN'].encode(), 'sha256')
            message = '\n'.join(key + '=' + value for key, value in sorted(identity.items()))
            identity['hash'] = hmac.digest(secret, message.encode(), 'sha256').hex()
            session = request('/session', dict(init_data=urlencode(identity)))
            auth = {'Authorization': 'Bearer ' + session['bearer']}
            revision = DraftStore(service).read(900002, 900002, now).revision
            selected = request('/me/draft', dict(revision=revision, template_id=template,
                duration_minutes=30, video_service='telemost'), 'PATCH',
                {**auth, 'Idempotency-Key': 'image-select-command'})
            body = dict(revision=selected['revision'], template_id=template, interval_id=interval,
                        starts_at=start, duration_minutes=30, video_service='telemost')
            hold = request('/holds', body, headers={**auth, 'Idempotency-Key': 'image-hold-command'})
            assert hold['booking']['status'] == 'held'
            assert hold['current']['draft']['step'] == 'contacts'
            replay = request('/holds', body, headers={**auth, 'Idempotency-Key': 'image-hold-command'})
            assert replay['booking']['id'] == hold['booking']['id']
            saved = request('/holds/' + str(hold['booking']['id']) + '/draft',
                dict(revision=hold['current']['revision'], contact_name='Synthetic Check'), 'PATCH',
                {**auth, 'Idempotency-Key': 'image-contacts-command'})
            assert saved['draft']['contact_name'] == 'Synthetic Check'
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
        if thread.is_alive():
            raise RuntimeError("Image API shutdown failed")
    print("Release image: startup, built frontend, readiness, authentication, hold/replay and contact draft OK")


if __name__ == "__main__":
    main()
