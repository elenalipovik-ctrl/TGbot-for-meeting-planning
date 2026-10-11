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


def test_verified_clock_preserves_auth_ttl_despite_wall_clock_skew(tmp_path):
    from email.utils import formatdate
    from meeting_bot.owner_preview import telegram_clock
    from meeting_bot.api.app import create_app
    from meeting_bot.api.telegram_auth import verify_init_data
    from meeting_bot.api.errors import ApiError
    h = Harness(tmp_path)
    actual = h.now
    monotonic = [100.0]
    wall = [actual + 459]
    clock = telegram_clock(formatdate(actual, usegmt=True), monotonic=lambda:monotonic[0], system_time=lambda:wall[0])
    with pytest.raises(ApiError):
        verify_init_data(signed(ADMIN, actual), TOKEN, now=wall[0], max_age=300, future_skew=30)
    h.config = replace(h.config, allowed_user_ids=frozenset({ADMIN}))
    client = TestClient(create_app(h.service, h.calendar, h.config, clock=clock))
    result = client.post(P + "/session", json={"init_data":signed(ADMIN, actual)})
    assert result.status_code == 200
    assert result.json()["server_time"] == actual
    assert result.json()["data"]["expires_at"] == actual + 1800
    # OS time jumps have no effect; elapsed time still expires old auth and sessions.
    wall[0] += 3600
    assert clock() == actual
    monotonic[0] += 301
    old = client.post(P + "/session", json={"init_data":signed(ADMIN, actual, extra={"query_id":"old-second"})})
    assert old.status_code == 401
    monotonic[0] += 1499
    headers = {"Authorization":"Bearer " + result.json()["data"]["bearer"]}
    assert client.get(P + "/bootstrap", headers=headers).status_code == 401


@pytest.mark.parametrize("header", [None, "invalid", "Sun, 11 Oct 2026 14:00:00", "Sun, 11 Oct 2026 14:00:00 +0300"])
def test_verified_clock_rejects_missing_or_non_utc_date(header):
    from meeting_bot.owner_preview import telegram_clock
    with pytest.raises(ValueError):
        telegram_clock(header)


def test_verified_clock_rejects_excessive_discrepancy():
    from email.utils import formatdate
    from meeting_bot.owner_preview import telegram_clock
    with pytest.raises(ValueError):
        telegram_clock(formatdate(1000000, usegmt=True), system_time=lambda:1000901)


def test_auth_diagnostic_never_includes_raw_input():
    from meeting_bot.api.telegram_auth import verify_init_data
    from meeting_bot.api.errors import ApiError
    codes = []
    now = at(5, 10)
    for raw in ("private-value", signed(ADMIN, now-301), signed(ADMIN, now+31), signed(ADMIN, now, token="wrong-secret")):
        with pytest.raises(ApiError):
            verify_init_data(raw, TOKEN, now=now, max_age=300, future_skew=30, on_reject=codes.append)
    assert codes == ["AUTH_FORMAT", "AUTH_TOO_OLD", "AUTH_IN_FUTURE", "AUTH_SIGNATURE"]


def test_paired_visitor_is_visitor_and_third_account_denied(tmp_path):
    from meeting_bot.owner_preview import owner_settings, create_owner_app
    env = tmp_path / ".env-miniapp-owner"
    env.write_text(f"MINI_APP_MODE=owner-preview\nTELEGRAM_BOT_TOKEN={TOKEN}\nBOT_ADMIN_ID={ADMIN}\nMINI_APP_VISITOR_ID={VISITOR}\nMINI_APP_ORIGIN={ORIGIN}\n", encoding="utf8")
    database, owner, config = owner_settings(env)
    assert config.allowed_user_ids == frozenset({ADMIN, VISITOR})
    app = create_owner_app(database, owner, config, clock=lambda: at(5,10))
    client = TestClient(app)
    login = client.post(P + "/session", json={"init_data":signed(VISITOR, at(5,10))})
    assert login.status_code == 200
    headers = {"Authorization":"Bearer " + login.json()["data"]["bearer"]}
    result = client.get(P + "/bootstrap", headers=headers).json()["data"]
    assert result["role"] == "visitor" and result["access_status"] == "allowed"
    assert client.get(P + "/templates", headers=headers).status_code == 200
    assert client.get(P + "/admin/templates", headers=headers).status_code == 403
    assert client.post(P + "/session", json={"init_data":signed(1234567890123, at(5,10))}).status_code == 403
    # Restart must preserve a block made by the administrator, not grant access again.
    app.state.service.decide_access(owner, VISITOR, "blocked", at(5,10))
    app = create_owner_app(database, owner, config, clock=lambda: at(5,10))
    assert TestClient(app).get(P + "/me/profile", headers=headers).status_code == 403


@pytest.mark.parametrize("visitor", ["0", "-1", "bad", str(ADMIN), str(2**63)])
def test_paired_visitor_id_is_strict(tmp_path, visitor):
    env = tmp_path / ".env-miniapp-owner"
    env.write_text(f"MINI_APP_MODE=owner-preview\nTELEGRAM_BOT_TOKEN={TOKEN}\nBOT_ADMIN_ID={ADMIN}\nMINI_APP_VISITOR_ID={visitor}\nMINI_APP_ORIGIN={ORIGIN}\n", encoding="utf8")
    with pytest.raises(ValueError):
        owner_settings(env)


def test_visitor_menu_only_changes_visitor_and_can_restore(tmp_path, monkeypatch):
    import meeting_bot.owner_preview_menu as menu
    env = tmp_path / ".env-miniapp-owner"
    env.write_text(f"MINI_APP_MODE=owner-preview\nTELEGRAM_BOT_TOKEN={TOKEN}\nBOT_ADMIN_ID={ADMIN}\nMINI_APP_VISITOR_ID={VISITOR}\nMINI_APP_ORIGIN={ORIGIN}\n", encoding="utf8")
    menus = {None:{"type":"commands"}, ADMIN:{"type":"web_app","text":"Owner","web_app":{"url":ORIGIN+"/app/"}}, VISITOR:{"type":"commands"}}
    before_owner = dict(menus[ADMIN])
    def call(token, method, payload):
        if method == "getChatMenuButton": return dict(menus[payload.get("chat_id")])
        assert payload["chat_id"] == VISITOR
        menus[VISITOR] = payload["menu_button"]
        return True
    monkeypatch.setattr(menu, "verify_owner_bot", lambda token: None)
    monkeypatch.setattr(menu, "telegram_request", call)
    menu.update_menu(env, visitor=True)
    assert menus[VISITOR]["type"] == "web_app" and menus[ADMIN] == before_owner
    menu.update_menu(env, visitor=True, restore=True)
    assert menus[VISITOR] == {"type":"commands"} and menus[None] == {"type":"commands"}
