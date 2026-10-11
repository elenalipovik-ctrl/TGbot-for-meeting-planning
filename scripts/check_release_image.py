"""CI-only image smoke: network=none, temporary fake settings/data, no Telegram/Google."""
import json
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
        app = build_app(assets=Path("/app/mini_app/dist"))
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
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
        if thread.is_alive():
            raise RuntimeError("Image API shutdown failed")
    print("Release image: startup, built frontend, readiness and authentication OK")


if __name__ == "__main__":
    main()
