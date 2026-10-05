from pathlib import Path

import pytest

from meeting_bot.runtime_mode import allowed_test_users


def test_local_test_requires_two_ids_and_isolated_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = {
        "BOT_STORAGE_IN_RUSSIA": "false", "BOT_LOCAL_TEST_MODE": "true",
        "BOT_ADMIN_ID": "900", "BOT_LOCAL_TEST_VISITOR_ID": "101",
        "BOT_DB_PATH": "data/local_test.sqlite",
    }
    assert allowed_test_users(settings) == frozenset((900, 101))
    settings["BOT_DB_PATH"] = "data/bot.sqlite"
    with pytest.raises(RuntimeError, match="isolated"):
        allowed_test_users(settings)
    settings["BOT_DB_PATH"] = "data/local_test.sqlite"
    settings["BOT_LOCAL_TEST_VISITOR_ID"] = "900"
    with pytest.raises(RuntimeError, match="distinct"):
        allowed_test_users(settings)


def test_local_mode_cannot_be_used_on_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = {
        "BOT_STORAGE_IN_RUSSIA": "true", "BOT_LOCAL_TEST_MODE": "true",
        "BOT_ADMIN_ID": "900", "BOT_LOCAL_TEST_VISITOR_ID": "101",
        "BOT_DB_PATH": str(Path("data/local_test.sqlite")),
    }
    with pytest.raises(RuntimeError, match="off on the server"):
        allowed_test_users(settings)
    settings["BOT_LOCAL_TEST_MODE"] = "false"
    with pytest.raises(RuntimeError, match="explicitly"):
        allowed_test_users(settings)
    settings["BOT_SERVER_TEST_MODE"] = "false"
    assert allowed_test_users(settings) is None


def test_server_pilot_allows_only_two_test_accounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = {
        "BOT_STORAGE_IN_RUSSIA": "true", "BOT_LOCAL_TEST_MODE": "false",
        "BOT_SERVER_TEST_MODE": "true", "BOT_ADMIN_ID": "900",
        "BOT_SERVER_TEST_VISITOR_ID": "101", "BOT_DB_PATH": "data/bot.sqlite",
    }
    assert allowed_test_users(settings) == frozenset((900, 101))
    settings["BOT_DB_PATH"] = "data/local_test.sqlite"
    with pytest.raises(RuntimeError, match="data/bot.sqlite"):
        allowed_test_users(settings)
    settings["BOT_DB_PATH"] = "data/bot.sqlite"
    settings["BOT_SERVER_TEST_VISITOR_ID"] = "900"
    with pytest.raises(RuntimeError, match="distinct"):
        allowed_test_users(settings)
