"""Persistent at-least-once Telegram delivery."""

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from aiogram import Bot

from .database import connect
from .technical_log import log_event


@dataclass(frozen=True)
class Notice:
    id: int
    booking_id: int | None
    visitor_id: int | None
    channel: str
    kind: str
    attempts: int


def claim_notice(path: Path, now: int, *, lease_seconds: int = 120,
                 logger=None) -> Notice | None:
    connection = connect(path, logger=logger)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, booking_id, visitor_id, channel, kind, attempts FROM notifications "
            "WHERE channel = 'telegram' AND "
            "((status IN ('queued', 'failed') AND COALESCE(next_attempt_at, 0) <= ?) "
            "OR (status = 'sending' AND next_attempt_at <= ?)) "
            "ORDER BY id LIMIT 1", (now, now),
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        connection.execute(
            "UPDATE notifications SET status = 'sending', attempts = attempts + 1, "
            "next_attempt_at = ? WHERE id = ?", (now + lease_seconds, row[0]),
        )
        connection.execute("COMMIT")
        log_event(logger, "notice_claimed", code=row[3], record_id=row[0])
        return Notice(*row[:5], row[5] + 1)
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _booking_text(connection: sqlite3.Connection, booking_id: int) -> dict:
    row = connection.execute(
        "SELECT b.id, b.starts_at, b.ends_at, b.contact_name_snapshot, "
        "b.username_snapshot, b.email_snapshot, b.purpose, b.video_service, "
        "b.template_name_snapshot, b.decision_deadline_at, m.video_url "
        "FROM bookings b LEFT JOIN meetings m ON m.booking_id = b.id WHERE b.id = ?",
        (booking_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Booking missing for notification")
    keys = ("id", "starts", "ends", "name", "username", "email", "purpose",
            "video", "template", "deadline", "video_url")
    return dict(zip(keys, row))


def render_notice(path: Path, notice: Notice, administrator_id: int) -> tuple[int, str]:
    connection = connect(path)
    try:
        zone_name = connection.execute(
            "SELECT value FROM app_settings WHERE key = 'time_zone'").fetchone()[0]
        zone = ZoneInfo(zone_name)
        booking = _booking_text(connection, notice.booking_id) if notice.booking_id else None
        visitor = connection.execute(
            "SELECT username FROM visitors WHERE telegram_id = ?", (notice.visitor_id,)
        ).fetchone() if notice.visitor_id else None
    finally:
        connection.close()
    when = (datetime.fromtimestamp(booking["starts"], zone).strftime("%d.%m.%Y %H:%M МСК")
            if booking else "")
    kind = notice.kind
    if kind == "admin_access_request":
        text = (f"Запрос доступа от @{visitor[0] if visitor and visitor[0] else 'без username'} "
                f"(ID {notice.visitor_id}). Откройте /admin → Запросы доступа.")
        recipient = administrator_id
    elif kind.startswith("access_"):
        labels = {"access_allowed": "Доступ к записи открыт.",
                  "access_rejected": "Запрос доступа отклонён.",
                  "access_blocked": "Доступ к записи заблокирован."}
        text = labels.get(kind, "Статус доступа изменён.")
        recipient = notice.visitor_id
    elif kind == "admin_new_request":
        text = (f"Новая заявка №{booking['id']}: {booking['name']}, "
                f"@{booking['username']}; {when}; {booking['video']}; "
                f"{booking['template']}. Email: {booking['email']}. "
                f"Цель: {booking['purpose']}. "
                "Решение — до " +
                datetime.fromtimestamp(booking["deadline"], zone).strftime("%d.%m.%Y %H:%M МСК") +
                ". Откройте /admin → Заявки.")
        recipient = administrator_id
    elif kind == "meeting_confirmed":
        text = (f"Встреча №{booking['id']} подтверждена: {when}, "
                f"{booking['video']}. Ссылка: {booking['video_url']}. "
                f"Приглашение отправлено на {booking['email']}. "
                "Отменить можно не позднее чем за час до начала.")
        recipient = notice.visitor_id
    elif kind == "request_rejected":
        text = f"Заявка №{booking['id']} на {when} отклонена."
        recipient = notice.visitor_id
    elif kind == "request_expired":
        text = f"Срок рассмотрения заявки №{booking['id']} истёк. Вы можете записаться заново."
        recipient = notice.visitor_id
    elif kind in ("meeting_cancelled", "admin_cancelled"):
        text = f"Встреча №{booking['id']} на {when} отменена."
        recipient = notice.visitor_id
    elif kind == "visitor_cancelled":
        text = f"Посетитель отменил заявку №{booking['id']} на {when}."
        recipient = administrator_id
    elif kind in ("reminder_24h", "reminder_2h"):
        text = f"Напоминание: встреча №{booking['id']} {when}. Ссылка: {booking['video_url']}."
        recipient = notice.visitor_id
    elif kind == "calendar_attention":
        text = f"Требует внимания заявка №{booking['id']}: сбой синхронизации с Google Календарем."
        recipient = administrator_id
    else:
        raise ValueError("Unknown notification kind")
    if notice.channel != "telegram" or recipient is None:
        raise ValueError("Invalid notification recipient")
    return int(recipient), text


def send_telegram(token: str, recipient: int, text: str) -> None:
    async def deliver():
        bot = Bot(token=token)
        try:
            await bot.send_message(recipient, text)
        finally:
            await bot.session.close()
    asyncio.run(deliver())


def _mark_sent(path: Path, notice_id: int, now: int, logger=None) -> None:
    connection = connect(path, logger=logger)
    try:
        connection.execute(
            "UPDATE notifications SET status = 'sent', next_attempt_at = NULL, "
            "sent_at = ? WHERE id = ? AND status = 'sending'", (now, notice_id))
        log_event(logger, "notice_sent", record_id=notice_id)
    finally:
        connection.close()


def _mark_failed(path: Path, notice: Notice, now: int, error: Exception,
                 logger=None) -> None:
    connection = connect(path, logger=logger)
    try:
        connection.execute("BEGIN IMMEDIATE")
        delay = min(3600, 60 * 2 ** min(notice.attempts, 6))
        connection.execute(
            "UPDATE notifications SET status = 'failed', next_attempt_at = ? "
            "WHERE id = ? AND status = 'sending'", (now + delay, notice.id))
        connection.execute("COMMIT")
        log_event(logger, "notice_failed", code=type(error).__name__, record_id=notice.id)
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def run_due_notifications(path: Path, now: int, settings: dict[str, str],
                          *, logger=None, limit: int = 100,
                          telegram_send: Callable[[int, str], None] | None = None,
                          allowed_recipients: frozenset[int] | None = None) -> int:
    """Deliver due Telegram messages; leave failures queued for a later retry."""
    telegram_ready = bool(settings.get("TELEGRAM_BOT_TOKEN")) or telegram_send is not None
    if not telegram_ready:
        return 0
    completed = 0
    for _ in range(limit):
        notice = claim_notice(path, now, logger=logger)
        if notice is None:
            break
        try:
            recipient, message = render_notice(path, notice, int(settings["BOT_ADMIN_ID"]))
            if allowed_recipients is not None and recipient not in allowed_recipients:
                raise PermissionError("Local test recipient is outside the allowlist")
            (telegram_send or (lambda ident, text: send_telegram(
                settings["TELEGRAM_BOT_TOKEN"], ident, text)))(recipient, message)
            _mark_sent(path, notice.id, now, logger)
            completed += 1
        except Exception as error:
            _mark_failed(path, notice, now, error, logger)
    return completed
