"""Narrow Google Calendar API boundary. Responses and exceptions are never logged raw."""

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from google.auth.exceptions import RefreshError

from .rules import BusyInterval
from .technical_log import log_event


BASE_URL = "https://www.googleapis.com/calendar/v3"
EVENT_ID_PATTERN = re.compile(r"m[0-9a-f]{32}\Z")


class CalendarError(Exception):
    code = "calendar_error"


class CalendarUnavailable(CalendarError):
    code = "unavailable"


class CalendarPermissionDenied(CalendarError):
    code = "permission_denied"


class CalendarAuthExpired(CalendarError):
    code = "auth_expired"


class CalendarUncertain(CalendarError):
    code = "uncertain"


class CalendarConflict(CalendarError):
    code = "conflict"


def _utc(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class GoogleCalendar:
    def __init__(self, session: Any, calendar_id: str, *, logger: logging.Logger | None = None):
        if not calendar_id:
            raise ValueError("Calendar ID is required")
        self.session = session
        self.calendar_id = calendar_id
        self.logger = logger
        self._session_lock = threading.Lock()
        self._calendar_url = BASE_URL + "/calendars/" + quote(calendar_id, safe="")

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            # The polling adapter may run visitor actions in different threads.
            # AuthorizedSession also persists a rotated refresh token via .env.
            with self._session_lock:
                response = self.session.request(method, url, timeout=15, **kwargs)
        except RefreshError as error:
            log_event(self.logger, "google_request_failed", code="auth_expired")
            raise CalendarAuthExpired("Подключение Google Календаря истекло.") from error
        except Exception as error:
            log_event(self.logger, "google_transport_failed", code=type(error).__name__)
            raise CalendarUnavailable("Google Календарь временно недоступен.") from error
        status = response.status_code
        if status == 401:
            log_event(self.logger, "google_request_failed", code="auth_expired")
            raise CalendarAuthExpired("Подключение Google Календаря истекло.")
        if status == 403:
            log_event(self.logger, "google_request_failed", code="permission_denied")
            raise CalendarPermissionDenied("Нет доступа к Google Календарю.")
        if status >= 400 and status != 404:
            log_event(self.logger, "google_request_failed", code=str(status))
            raise CalendarUnavailable("Google Календарь временно недоступен.")
        return response

    def freebusy(self, starts_at: int, ends_at: int) -> list[BusyInterval]:
        response = self._request(
            "POST", BASE_URL + "/freeBusy",
            json={"timeMin": _utc(starts_at), "timeMax": _utc(ends_at),
                  "items": [{"id": self.calendar_id}]},
        )
        try:
            calendars = response.json()["calendars"]
            if self.calendar_id == "primary" and self.calendar_id not in calendars \
                    and len(calendars) == 1:
                calendar = next(iter(calendars.values()))
            else:
                calendar = calendars[self.calendar_id]
            if calendar.get("errors"):
                raise ValueError("freebusy errors")
            intervals = [BusyInterval(_epoch(item["start"]), _epoch(item["end"]))
                         for item in calendar["busy"]]
        except (KeyError, TypeError, ValueError) as error:
            log_event(self.logger, "google_freebusy_failed", code="invalid_response")
            raise CalendarUnavailable("Ответ Google Календаря неполон.") from error
        log_event(self.logger, "google_freebusy_ok", code="busy" if intervals else "free")
        return intervals

    def _get(self, event_id: str) -> dict[str, Any] | None:
        response = self._request("GET", self._calendar_url + "/events/" + event_id)
        if response.status_code == 404:
            return None
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise CalendarUncertain("Не удалось проверить событие Google.") from error

    def find_matching(self, *, event_id: str, booking_id: int, starts_at: int,
                      ends_at: int, email: str) -> bool:
        event = self._get(event_id)
        if event is None or event.get("status") == "cancelled":
            return False
        if not self._matches(event, event_id, booking_id, starts_at, ends_at, email):
            if self._belongs(event, event_id, booking_id):
                raise CalendarUncertain("Участник события Google не подтвержден.")
            raise CalendarConflict("Событие Google не совпадает с заявкой.")
        log_event(self.logger, "google_event_found", record_id=booking_id)
        return True

    @staticmethod
    def _belongs(event: dict[str, Any], event_id: str, booking_id: int) -> bool:
        try:
            return (event["id"] == event_id
                    and event["extendedProperties"]["private"]["booking_id"] == str(booking_id))
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _matches(event: dict[str, Any], event_id: str, booking_id: int,
                 starts_at: int, ends_at: int, email: str) -> bool:
        try:
            return (GoogleCalendar._belongs(event, event_id, booking_id)
                    and event.get("status") != "cancelled"
                    and _epoch(event["start"]["dateTime"]) == starts_at
                    and _epoch(event["end"]["dateTime"]) == ends_at
                    and any(item["email"].casefold() == email.casefold()
                            for item in event["attendees"]))
        except (KeyError, TypeError, ValueError):
            return False

    def create_or_find(self, *, event_id: str, booking_id: int, starts_at: int,
                       ends_at: int, email: str, video_url: str) -> str:
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ValueError("Invalid reserved Google event ID")
        existing = self._get(event_id)
        if existing is not None:
            if not self._matches(existing, event_id, booking_id, starts_at, ends_at, email):
                if self._belongs(existing, event_id, booking_id):
                    raise CalendarUncertain("Участник события Google не подтвержден.")
                raise CalendarConflict("Событие Google не совпадает с заявкой.")
            log_event(self.logger, "google_event_found", record_id=booking_id)
            return event_id
        body = {
            "id": event_id,
            "summary": "Онлайн-встреча",
            "description": video_url,
            "start": {"dateTime": _utc(starts_at)},
            "end": {"dateTime": _utc(ends_at)},
            "attendees": [{"email": email}],
            "extendedProperties": {"private": {"booking_id": str(booking_id),
                                               "source": "meeting_bot"}},
        }
        try:
            response = self._request("POST", self._calendar_url + "/events",
                                     params={"sendUpdates": "all"}, json=body)
            event = response.json()
            if not self._matches(event, event_id, booking_id, starts_at, ends_at, email):
                raise CalendarUncertain("Google не подтвердил участника события.")
            log_event(self.logger, "google_event_created", record_id=booking_id)
            return event_id
        except CalendarError as error:
            try:
                recovered = self._get(event_id)
            except CalendarError:
                recovered = None
            if recovered is not None:
                if not self._matches(recovered, event_id, booking_id, starts_at, ends_at, email):
                    if self._belongs(recovered, event_id, booking_id):
                        raise CalendarUncertain("Участник события Google не подтвержден.") from error
                    raise CalendarConflict("Событие Google не совпадает с заявкой.") from error
                log_event(self.logger, "google_event_reconciled", record_id=booking_id)
                return event_id
            if isinstance(error, (CalendarPermissionDenied, CalendarAuthExpired)):
                raise
            raise CalendarUncertain("Результат создания события Google неясен.") from error
        except (TypeError, ValueError) as error:
            raise CalendarUncertain("Ответ Google о событии неполон.") from error

    def delete_or_find_absent(self, event_id: str, booking_id: int) -> None:
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ValueError("Invalid reserved Google event ID")
        try:
            response = self._request("DELETE", self._calendar_url + "/events/" + event_id,
                                     params={"sendUpdates": "all"})
            if response.status_code in (200, 204):
                log_event(self.logger, "google_event_deleted", record_id=booking_id)
                return
            if response.status_code == 404:
                log_event(self.logger, "google_event_absent", record_id=booking_id)
                return
            raise CalendarUncertain("Google не подтвердил отмену события.")
        except CalendarError as error:
            try:
                event = self._get(event_id)
                if event is None or event.get("status") == "cancelled":
                    log_event(self.logger, "google_event_absent", record_id=booking_id)
                    return
            except CalendarError:
                pass
            raise CalendarUncertain("Результат отмены события Google неясен.") from error
