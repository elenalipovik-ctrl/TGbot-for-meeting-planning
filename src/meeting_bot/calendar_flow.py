"""Booking transitions around Google calls; SQLite transactions stay short."""

from .google_calendar import CalendarError, GoogleCalendar
from .rules import BusyInterval
from .service import BookingService, RuleError, SlotUnavailable


GAP_SECONDS = 1800


def _conflicts(starts_at: int, ends_at: int, busy: list[BusyInterval]) -> bool:
    return any(starts_at < item.ends_at + GAP_SECONDS
               and item.starts_at < ends_at + GAP_SECONDS for item in busy)


def submit_with_google(service: BookingService, calendar: GoogleCalendar,
                       visitor_id: int, booking_id: int, *, name: str,
                       username: str | None, email: str, phone: str | None,
                       purpose: str, policy_version: str, now: int,
                       used_saved_contacts: bool = False) -> int:
    starts_at, ends_at = service.booking_window(booking_id)
    busy = calendar.freebusy(starts_at - GAP_SECONDS, ends_at + GAP_SECONDS)
    return service.submit_request(
        visitor_id, booking_id, name=name, username=username, email=email,
        phone=phone, purpose=purpose, policy_version=policy_version, now=now,
        google_busy=busy, used_saved_contacts=used_saved_contacts,
    )


def confirm_with_google(service: BookingService, calendar: GoogleCalendar,
                        booking_id: int, video_url: str, now: int) -> str:
    status = service.booking_status(booking_id)
    if status == "confirmed":
        current = service.calendar_booking(booking_id)
        if current.video_url != video_url:
            raise RuleError("Для заявки уже сохранена другая ссылка.")
        return "confirmed"
    if status in ("creating_calendar", "attention"):
        current = service.calendar_booking(booking_id)
        if current.cancel_requested_at is not None or current.video_url != video_url:
            raise RuleError("Заявка не готова к повторной проверке Google Календаря.")
    else:
        service.begin_calendar_creation(booking_id, video_url, now)
    event_id = service.reserve_google_event_id(booking_id, now)
    booking = service.calendar_booking(booking_id)
    try:
        found = calendar.find_matching(
            event_id=event_id, booking_id=booking_id, starts_at=booking.starts_at,
            ends_at=booking.ends_at, email=booking.email,
        )
        if not found:
            busy = calendar.freebusy(booking.starts_at - GAP_SECONDS,
                                     booking.ends_at + GAP_SECONDS)
            if _conflicts(booking.starts_at, booking.ends_at, busy):
                raise SlotUnavailable("Время занято в Google Календаре.")
            calendar.create_or_find(
                event_id=event_id, booking_id=booking_id, starts_at=booking.starts_at,
                ends_at=booking.ends_at, email=booking.email, video_url=booking.video_url,
            )
        return service.mark_calendar_created(booking_id, event_id, booking.video_url, now)
    except (CalendarError, SlotUnavailable) as error:
        code = error.code if isinstance(error, CalendarError) else "busy"
        service.mark_calendar_attention(booking_id, code, now)
        raise


def finish_calendar_cancellation(service: BookingService, calendar: GoogleCalendar,
                                 booking_id: int, now: int) -> str:
    booking = service.calendar_booking(booking_id)
    if booking.status != "attention" or booking.cancel_requested_at is None:
        raise ValueError("No pending calendar cancellation")
    if booking.google_event_id is None:
        raise ValueError("No Google event for confirmed booking")
    try:
        calendar.delete_or_find_absent(booking.google_event_id, booking_id)
        return service.mark_calendar_cancelled(booking_id, now)
    except CalendarError as error:
        service.mark_calendar_attention(booking_id, error.code, now)
        raise
