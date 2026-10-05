"""Prefer the VPS's working IPv4 route for Telegram API connections."""

import socket

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession


def telegram_bot(token: str) -> Bot:
    session = AiohttpSession()
    # aiogram 3.31 stores TCPConnector options here. Keep this workaround in
    # one place because future aiogram versions may expose a public setting.
    session._connector_init["family"] = socket.AF_INET
    return Bot(token=token, session=session)
