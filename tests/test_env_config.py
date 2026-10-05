from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_bot.env_config import read_env, write_env_values
from meeting_bot import google_auth
from meeting_bot.google_auth import calendar_from_env


def test_env_keeps_values_with_special_characters_and_no_extra_file(tmp_path: Path):
    path = tmp_path / ".env"
    write_env_values(path, {"GOOGLE_CLIENT_ID": "example", "GOOGLE_CLIENT_SECRET": "a#b=c\"d"})
    write_env_values(path, {"GOOGLE_REFRESH_TOKEN": "refresh-placeholder"})
    assert read_env(path) == {
        "GOOGLE_CLIENT_ID": "example",
        "GOOGLE_CLIENT_SECRET": "a#b=c\"d",
        "GOOGLE_REFRESH_TOKEN": "refresh-placeholder",
    }
    assert list(tmp_path.iterdir()) == [path]


def test_google_session_persists_rotated_refresh_token_only_in_env(tmp_path: Path):
    path = tmp_path / ".env"
    write_env_values(path, {
        "GOOGLE_CLIENT_ID": "example-id",
        "GOOGLE_CLIENT_SECRET": "example-secret",
        "GOOGLE_REFRESH_TOKEN": "old-placeholder",
    })
    calendar = calendar_from_env(path)

    def fake_request(*args, **kwargs):
        calendar.session.credentials._refresh_token = "new-placeholder"
        return object()

    calendar.session.session.request = fake_request
    calendar.session.request("GET", "https://example.invalid")
    assert read_env(path)["GOOGLE_REFRESH_TOKEN"] == "new-placeholder"
    assert list(tmp_path.iterdir()) == [path]


def test_google_session_rejects_missing_env_values(tmp_path: Path):
    path = tmp_path / ".env"
    write_env_values(path, {"GOOGLE_CLIENT_ID": "example-id"})
    with pytest.raises(RuntimeError, match="incomplete"):
        calendar_from_env(path)


def test_owner_authorization_saves_only_refresh_token_in_env(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    write_env_values(path, {
        "GOOGLE_CLIENT_ID": "example-id",
        "GOOGLE_CLIENT_SECRET": "example-secret",
    })

    class FakeFlow:
        def run_local_server(self, **kwargs):
            return SimpleNamespace(refresh_token="refresh-placeholder")

    def fake_from_client_config(config, scopes):
        assert config["installed"]["client_id"] == "example-id"
        assert config["installed"]["client_secret"] == "example-secret"
        assert scopes == google_auth.SCOPES
        return FakeFlow()

    monkeypatch.setattr(google_auth.InstalledAppFlow, "from_client_config",
                        fake_from_client_config)
    google_auth.authorize(path)
    assert read_env(path)["GOOGLE_REFRESH_TOKEN"] == "refresh-placeholder"
    assert list(tmp_path.iterdir()) == [path]
