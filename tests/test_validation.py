import pytest

from meeting_bot.validation import (
    valid_email, valid_phone, valid_video_url, valid_visitor_text,
)


@pytest.mark.parametrize("value,expected", [
    (" user+tag@example.org ", True),
    ("user@пример.рф", True),
    ("a@b.c", True),
    ("user@example", False),
    ("user@@example.org", False),
    ("user name@example.org", False),
    ("user@exam\nple.org", False),
    ("", False),
])
def test_shared_email_preserves_chat_acceptance(value, expected):
    assert valid_email(value) is expected


@pytest.mark.parametrize("value,expected", [
    (" +7 (999) 123-45-67 ", True),
    ("1234567", True),
    ("1" * 25, True),
    ("1" * 26, False),
    ("123456", False),
    ("call-me", False),
    ("", False),
])
def test_shared_phone_format_and_boundaries(value, expected):
    assert valid_phone(value) is expected


def test_shared_text_limit_counts_trimmed_characters():
    assert valid_visitor_text(" а ")
    assert valid_visitor_text(" " + "я" * 500 + " ")
    assert not valid_visitor_text("я" * 501)
    assert not valid_visitor_text(" \n ")


@pytest.mark.parametrize("url,provider,expected", [
    (" https://telemost.yandex.ru/j/example ", "telemost", True),
    ("https://TELEMOST.YANDEX.RU:443/j/example", "telemost", True),
    ("https://us05web.zoom.us/j/123?pwd=example", "zoom", True),
    ("https://zoom.us/j/123", "zoom", True),
    ("http://telemost.yandex.ru/j/example", "telemost", False),
    ("https://telemost.yandex.ru.evil.test/j/example", "telemost", False),
    ("https://eviltelemost.yandex.ru/j/example", "telemost", False),
    ("https://zoom.us.evil.test/j/123", "zoom", False),
    ("https://evilzoom.us/j/123", "zoom", False),
    ("https://us05web.zoom.us/j/123", "telemost", False),
    ("https://user:password@telemost.yandex.ru/j/example", "telemost", False),
    ("https://telemost.yandex.ru:8443/j/example", "telemost", False),
    ("https://telemost.yandex.ru:bad/j/example", "telemost", False),
    ("https://telemost.yandex.ru/", "telemost", False),
    ("https://[broken", "telemost", False),
    ("https://telemost.yandex.ru/j/example", "unknown", False),
])
def test_shared_video_url_provider_and_authority(url, provider, expected):
    assert valid_video_url(url, provider) is expected
