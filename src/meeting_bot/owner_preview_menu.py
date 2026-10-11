"""Configure only the owner's private menu button; preserve a rollback snapshot."""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen
from .owner_preview import owner_settings, verify_owner_bot


def telegram_request(token, method, payload):
    if method not in ("getChatMenuButton", "setChatMenuButton"):
        raise ValueError("Unsupported menu operation")
    try:
        request = Request("https://api.telegram.org/bot" + token + "/" + method,
            data=json.dumps(payload).encode("utf8"), headers={"Content-Type":"application/json"}, method="POST")
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(65536))
        if result.get("ok") is not True:
            raise ValueError("Telegram rejected menu operation")
        return result["result"]
    except Exception:
        raise ValueError("Telegram menu operation failed; credentials are not logged") from None


def update_menu(env_file, *, restore=False):
    database, owner, config = owner_settings(env_file)
    verify_owner_bot(config.bot_token)
    call = lambda method, payload: telegram_request(config.bot_token, method, payload)
    snapshot = database.parent / "previous-menu.json"
    if snapshot.is_symlink() or (snapshot.exists() and snapshot.stat().st_nlink > 1):
        raise ValueError("Unsafe menu snapshot")
    default_before = call("getChatMenuButton", {})
    current = call("getChatMenuButton", {"chat_id":owner})
    if restore:
        saved = json.loads(snapshot.read_text(encoding="utf8"))
        if saved["chat_id"] != owner:
            raise ValueError("Menu snapshot belongs to a different owner")
        target = saved["menu_button"]
    else:
        if not snapshot.exists():
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            with snapshot.open("x", encoding="utf8") as stream:
                json.dump({"chat_id":owner,"menu_button":current}, stream, ensure_ascii=False)
        target = {"type":"web_app","text":"Планировщик · тест", "web_app":{"url":config.allowed_origins[0]+"/app/"}}
    if call("setChatMenuButton", {"chat_id":owner,"menu_button":target}) is not True:
        raise ValueError("Menu update was not acknowledged")
    if call("getChatMenuButton", {"chat_id":owner}) != target:
        raise ValueError("Menu update could not be verified")
    if call("getChatMenuButton", {}) != default_before:
        raise ValueError("Default menu changed concurrently; inspect Telegram settings")
    return target


def main(argv=None):
    parser=argparse.ArgumentParser(description="Owner-only Mini App menu button")
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--restore", action="store_true")
    args=parser.parse_args(argv)
    try:
        update_menu(args.env_file, restore=args.restore)
    except (ValueError, OSError, KeyError):
        print("Private menu update failed; previous settings are retained locally for recovery.", file=sys.stderr)
        return 1
    print("Owner private menu verified; default menu is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
