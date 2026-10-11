from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from meeting_bot.api.config import ApiConfig
from meeting_bot.database import connect
from meeting_bot.owner_preview import owner_settings, create_owner_app
from test_mini_app_api import Harness, TOKEN, ORIGIN, P, signed
from test_service import ADMIN, VISITOR, at


def test_allowlist_denies_valid_other_user_without_session(tmp_path):
    h = Harness(tmp_path)
    h.config = replace(h.config, allowed_user_ids=frozenset({ADMIN}))
    h.client = h.restart()
    result = h.client.post(P + "/session", json={"init_data": signed(VISITOR, h.now)})
    assert result.status_code == 403
    assert result.json()["error"]["code"] == "TEST_ACCESS_DENIED"
    with connect(h.path) as c:
        assert c.execute("SELECT count(*) FROM api_sessions").fetchone()[0] == 0
    assert h.calendar.calls == 0
    headers = h.login(ADMIN)
    assert h.client.get(P + "/bootstrap", headers=headers).status_code == 200


def test_allowlist_applies_to_sessions_created_before_restriction(tmp_path):
    h = Harness(tmp_path)
    headers = h.login(VISITOR)
    h.config = replace(h.config, allowed_user_ids=frozenset({ADMIN}))
    h.client = h.restart()
    for path in ("/bootstrap", "/me/profile", "/admin/templates"):
        assert h.client.get(P + path, headers=headers).status_code == 403
    assert h.client.delete(P + "/session", headers=headers).status_code == 403
    assert h.calendar.calls == 0


@pytest.mark.parametrize("ids", [frozenset(), frozenset({True}), frozenset({0}), frozenset({-1}), frozenset({2**63}), {ADMIN}, (ADMIN,)])
def test_allowlist_is_strict(ids):
    with pytest.raises(ValueError):
        ApiConfig(TOKEN, "test", ORIGIN + "/privacy", (ORIGIN,), allowed_user_ids=ids)


def test_owner_launcher_configuration_and_synthetic_app(tmp_path):
    env = tmp_path / ".env-miniapp-owner"
    content = f"MINI_APP_MODE=owner-preview\nTELEGRAM_BOT_TOKEN={TOKEN}\nBOT_ADMIN_ID={ADMIN}\nMINI_APP_ORIGIN={ORIGIN}\n"
    env.write_text(content, encoding="utf8")
    database, owner, config = owner_settings(env)
    assert database == tmp_path / ".secrets" / "miniapp-owner" / "synthetic.sqlite"
    app = create_owner_app(database, owner, config, clock=lambda: at(5, 10))
    client = TestClient(app)
    result = client.post(P + "/session", json={"init_data": signed(ADMIN, at(5, 10))})
    headers = {"Authorization": "Bearer " + result.json()["data"]["bearer"]}
    bootstrap = client.get(P + "/bootstrap", headers=headers).json()["data"]
    assert bootstrap["role"] == "admin" and bootstrap["capabilities"]["test_environment"] is True
    assert client.post("/local-preview/session", json={"persona":"admin"}).status_code == 404
    assert client.get("/owner-preview/policy").status_code == 200
    with connect(database) as c:
        assert c.execute("SELECT count(*) FROM meeting_templates").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM bookings").fetchone()[0] == 1
    # Restart does not duplicate fixtures or replace user changes.
    create_owner_app(database, owner, config, clock=lambda: at(5, 10))
    env.write_text(content + "GOOGLE_CALENDAR_ID=primary\n", encoding="utf8")
    with pytest.raises(ValueError):
        owner_settings(env)
    for bad in (".env", ".env-miniapp-test"):
        path = tmp_path / bad
        path.write_text(content, encoding="utf8")
        with pytest.raises(ValueError):
            owner_settings(path)


def test_owner_factory_requires_matching_owner_allowlist(tmp_path):
    config = ApiConfig(TOKEN, "test", ORIGIN + "/privacy", (ORIGIN,), test_environment=True)
    with pytest.raises(ValueError):
        create_owner_app(tmp_path / "db.sqlite", ADMIN, config)
    assert not (tmp_path / "db.sqlite").exists()


def test_menu_update_and_restore_never_change_default(tmp_path, monkeypatch):
    import meeting_bot.owner_preview_menu as menu
    env = tmp_path / ".env-miniapp-owner"
    env.write_text(f"MINI_APP_MODE=owner-preview\nTELEGRAM_BOT_TOKEN={TOKEN}\nBOT_ADMIN_ID={ADMIN}\nMINI_APP_ORIGIN={ORIGIN}\n", encoding="utf8")
    default = {"type":"commands"}
    private = {"type":"commands"}
    writes = []
    def call(token, method, payload):
        nonlocal private
        if method == "getChatMenuButton":
            return dict(private if payload else default)
        assert payload["chat_id"] == ADMIN
        writes.append(payload)
        private = payload["menu_button"]
        return True
    monkeypatch.setattr(menu, "verify_owner_bot", lambda token: None)
    monkeypatch.setattr(menu, "telegram_request", call)
    target = menu.update_menu(env)
    assert target["type"] == "web_app" and target["web_app"]["url"] == ORIGIN + "/app/"
    assert menu.update_menu(env, restore=True) == {"type":"commands"}
    assert len(writes) == 2 and default == {"type":"commands"}
