"""Check the static site and exact text of the owner-approved policy."""

import html
import sys
from pathlib import Path
from urllib.request import urlopen


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    home = (root / "public_site" / "index.html").read_text(encoding="utf-8")
    page = (root / "public_site" / "privacy" / "index.html").read_text(encoding="utf-8")
    approved = (root / "docs" / "privacy_policy_public_v0.2.txt").read_text(
        encoding="utf-8"
    ).strip()
    check(home, page, approved)
    if "--live" in sys.argv:
        with urlopen("https://meetcalendar.fin-partner.pro/", timeout=12) as response:
            live_home = response.read().decode("utf-8")
        with urlopen("https://meetcalendar.fin-partner.pro/privacy/", timeout=12) as response:
            live_policy = response.read().decode("utf-8")
        check(live_home, live_policy, approved)
        if live_home != home or live_policy != page:
            raise SystemExit("Published pages differ from reviewed local files")
        print("Live HTTPS site and policy: OK")
    else:
        print("Public site links and approved policy text: OK")


def check(home: str, page: str, approved: str) -> None:
    if "https://t.me/MyMeetPlanning_bot" not in home or 'href="/privacy/"' not in home:
        raise SystemExit("Homepage is missing bot or policy link")
    if '<div class="policy">' not in page or "</div>" not in page:
        raise SystemExit("Policy page structure is incomplete")
    published = html.unescape(page.split('<div class="policy">', 1)[1].split("</div>", 1)[0])
    if published.strip() != approved:
        raise SystemExit("Policy page differs from the owner-approved version 0.2")


if __name__ == "__main__":
    main()
