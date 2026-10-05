"""Pure scheduling rules. No Telegram or Google API calls live here."""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc
DURATIONS = (15, 30, 45, 60, 90)
VIDEO_SERVICES = ("telemost", "zoom")
SLOT_STEP_SECONDS = 15 * 60
MIN_LEAD_SECONDS = 2 * 60 * 60
CANCEL_LIMIT_SECONDS = 60 * 60
GAP_SECONDS = 30 * 60
HORIZON_SECONDS = 7 * 24 * 60 * 60
REVIEW_SECONDS = 3 * 60 * 60
CLOSED_WEEKDAYS = frozenset((2, 5, 6))  # Wednesday, Saturday, Sunday


@dataclass(frozen=True)
class ReviewWindow:
    weekday: int
    starts_minute: int
    ends_minute: int


@dataclass(frozen=True)
class ReviewSchedule:
    time_zone: str
    windows: tuple[ReviewWindow, ...]

    def __post_init__(self) -> None:
        ZoneInfo(self.time_zone)
        by_day: dict[int, list[ReviewWindow]] = {}
        for window in self.windows:
            if not 0 <= window.weekday <= 6:
                raise ValueError("Invalid review weekday")
            if not 0 <= window.starts_minute < window.ends_minute <= 1440:
                raise ValueError("Invalid review window")
            by_day.setdefault(window.weekday, []).append(window)
        for windows in by_day.values():
            ordered = sorted(windows, key=lambda item: item.starts_minute)
            if any(a.ends_minute > b.starts_minute for a, b in zip(ordered, ordered[1:])):
                raise ValueError("Overlapping review windows")


@dataclass(frozen=True)
class Availability:
    id: int
    starts_at: int
    ends_at: int
    time_zone: str
    is_published: bool = True
    is_manual_exception: bool = False


@dataclass(frozen=True)
class BusyInterval:
    starts_at: int
    ends_at: int


def allowed_durations(video_service: str) -> tuple[int, ...]:
    if video_service == "telemost":
        return DURATIONS
    if video_service == "zoom":
        return (15, 30)
    raise ValueError("Unknown video service")


def _day_window(day: date, start_minute: int, end_minute: int,
                zone: ZoneInfo) -> tuple[int, int]:
    midnight = datetime.combine(day, time.min, tzinfo=zone)
    start = midnight + timedelta(minutes=start_minute)
    end = midnight + timedelta(minutes=end_minute)
    return int(start.timestamp()), int(end.timestamp())


def decision_deadline(submitted_at: int, schedule: ReviewSchedule) -> int:
    """Add three real hours inside configured review windows."""
    if not schedule.windows:
        raise ValueError("Review hours are not configured")
    zone = ZoneInfo(schedule.time_zone)
    local_day = datetime.fromtimestamp(submitted_at, zone).date()
    remaining = REVIEW_SECONDS
    cursor = submitted_at
    for offset in range(370):
        day = local_day + timedelta(days=offset)
        for window in sorted((item for item in schedule.windows if item.weekday == day.weekday()),
                             key=lambda item: item.starts_minute):
            window_start, window_end = _day_window(
                day, window.starts_minute, window.ends_minute, zone)
            begin = max(cursor, window_start)
            if begin >= window_end:
                continue
            available = window_end - begin
            if remaining <= available:
                return begin + remaining
            remaining -= available
            cursor = window_end
    raise ValueError("Review schedule has insufficient hours")


def conflicts(starts_at: int, ends_at: int, occupied: BusyInterval,
              gap_seconds: int = GAP_SECONDS) -> bool:
    return starts_at < occupied.ends_at + gap_seconds and occupied.starts_at < ends_at + gap_seconds


def available_slots(now: int, availability: list[Availability], duration_minutes: int,
                    video_service: str, schedule: ReviewSchedule,
                    occupied: list[BusyInterval] | None = None) -> list[int]:
    """Return UTC starts; occupied includes local bookings and Google busy ranges."""
    if duration_minutes not in allowed_durations(video_service):
        return []
    deadline = decision_deadline(now, schedule)
    occupied = occupied or []
    latest = now + HORIZON_SECONDS
    duration_seconds = duration_minutes * 60
    slots: set[int] = set()
    for interval in availability:
        if not interval.is_published or interval.ends_at <= now:
            continue
        zone = ZoneInfo(interval.time_zone)
        candidate = ((max(interval.starts_at, now + MIN_LEAD_SECONDS) +
                      SLOT_STEP_SECONDS - 1) // SLOT_STEP_SECONDS) * SLOT_STEP_SECONDS
        while candidate + duration_seconds <= interval.ends_at and candidate < latest:
            local_day = datetime.fromtimestamp(candidate, zone).weekday()
            if (local_day not in CLOSED_WEEKDAYS or interval.is_manual_exception) and \
                    deadline <= candidate - MIN_LEAD_SECONDS and \
                    not any(conflicts(candidate, candidate + duration_seconds, item)
                            for item in occupied):
                slots.add(candidate)
            candidate += SLOT_STEP_SECONDS
    return sorted(slots)


def can_cancel_confirmed(now: int, starts_at: int) -> bool:
    return now <= starts_at - CANCEL_LIMIT_SECONDS


def meets_min_lead(now: int, starts_at: int) -> bool:
    return starts_at >= now + MIN_LEAD_SECONDS


def add_calendar_months(timestamp: int, months: int, time_zone: str) -> int:
    """Calendar-month retention, clamping dates such as January 31."""
    zone = ZoneInfo(time_zone)
    local = datetime.fromtimestamp(timestamp, zone)
    month_index = local.year * 12 + local.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return int(local.replace(year=year, month=month, day=day).timestamp())
