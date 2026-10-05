"""Require an explicit audience choice for local and server operation."""

from pathlib import Path


def allowed_test_users(settings: dict[str, str]) -> frozenset[int] | None:
    """Return a test allowlist, or None for explicitly opened server operation."""
    server = settings.get("BOT_STORAGE_IN_RUSSIA") == "true"
    local = settings.get("BOT_LOCAL_TEST_MODE") == "true"
    server_test = settings.get("BOT_SERVER_TEST_MODE")
    if server:
        if local:
            raise RuntimeError("Local test mode must be off on the server")
        if server_test not in ("true", "false"):
            raise RuntimeError("Server test mode must be explicitly true or false")
        if server_test == "false":
            return None
        visitor_key = "BOT_SERVER_TEST_VISITOR_ID"
        expected_path = (Path.cwd() / "data" / "bot.sqlite").resolve()
    else:
        if server_test == "true":
            raise RuntimeError("Server test mode requires verified Russian storage")
        if not local:
            raise RuntimeError("Visitor intake requires a verified database location in Russia")
        visitor_key = "BOT_LOCAL_TEST_VISITOR_ID"
        expected_path = (Path.cwd() / "data" / "local_test.sqlite").resolve()
    try:
        admin_id = int(settings["BOT_ADMIN_ID"])
        visitor_id = int(settings[visitor_key])
    except (KeyError, ValueError) as error:
        raise RuntimeError("Test mode requires two numeric Telegram IDs") from error
    if admin_id <= 0 or visitor_id <= 0 or admin_id == visitor_id:
        raise RuntimeError("Test mode requires two distinct Telegram IDs")
    database_path = Path(settings.get("BOT_DB_PATH") or "").resolve()
    if database_path != expected_path:
        if server:
            raise RuntimeError("Server test mode requires data/bot.sqlite")
        raise RuntimeError("Local test requires the isolated data/local_test.sqlite database")
    return frozenset((admin_id, visitor_id))
