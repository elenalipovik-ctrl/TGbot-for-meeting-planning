"""Owner-only Google OAuth with secrets stored in the project's local .env."""

import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .env_config import ENV_FILE, read_env, require_keys, write_env_values
from .google_calendar import GoogleCalendar
from .technical_log import log_event


SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.owned",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
]
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def authorize(env_file: Path = ENV_FILE, logger: logging.Logger | None = None) -> None:
    """Print a consent URL for the owner and save only the refresh token on disk."""
    values = read_env(env_file)
    require_keys(values, "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    config = {"installed": {
        "client_id": values["GOOGLE_CLIENT_ID"],
        "client_secret": values["GOOGLE_CLIENT_SECRET"],
        "auth_uri": AUTH_URI,
        "token_uri": TOKEN_URI,
        "redirect_uris": ["http://localhost"],
    }}
    try:
        flow = InstalledAppFlow.from_client_config(config, SCOPES)
        credentials = flow.run_local_server(port=0, open_browser=False,
                                            access_type="offline", prompt="consent")
    except Exception as error:
        log_event(logger, "google_oauth_failed", code=type(error).__name__)
        raise RuntimeError("Google authorization did not complete") from None
    if not credentials.refresh_token:
        raise RuntimeError("Google did not provide offline access")
    write_env_values(env_file, {"GOOGLE_REFRESH_TOKEN": credentials.refresh_token})
    log_event(logger, "google_oauth_saved")


class _PersistingSession:
    """Persist a rotated refresh token before returning a Google API result."""

    def __init__(self, credentials: Credentials, env_file: Path):
        self.credentials = credentials
        self.session = AuthorizedSession(credentials)
        self.env_file = env_file
        self.saved_refresh_token = credentials.refresh_token

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return self.session.request(method, url, **kwargs)
        finally:
            token = self.credentials.refresh_token
            if token and token != self.saved_refresh_token:
                write_env_values(self.env_file, {"GOOGLE_REFRESH_TOKEN": token})
                self.saved_refresh_token = token


def calendar_from_env(env_file: Path = ENV_FILE, calendar_id: str = "primary",
                      logger: logging.Logger | None = None) -> GoogleCalendar:
    values = read_env(env_file)
    require_keys(values, "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
    credentials = Credentials(
        token=None,
        refresh_token=values["GOOGLE_REFRESH_TOKEN"],
        token_uri=TOKEN_URI,
        client_id=values["GOOGLE_CLIENT_ID"],
        client_secret=values["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    return GoogleCalendar(_PersistingSession(credentials, env_file), calendar_id, logger=logger)


def main() -> None:
    from .technical_log import configure_logging
    logger = configure_logging(Path.cwd() / "logs" / "technical.jsonl")
    authorize(ENV_FILE, logger=logger)
    print("Google Calendar authorization saved in the local .env.")


if __name__ == "__main__":
    main()
