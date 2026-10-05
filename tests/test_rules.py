from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from meeting_bot.rules import (
    Availability, BusyInterval, ReviewSchedule, ReviewWindow, add_calendar_months,
    available_slots, can_cancel_confirmed, decision_deadline, meets_min_lead,
)


ZONE = ZoneInfo("Europe/Moscow")


def at(day: int, hour: int, minute: int = 0, *, month: int = 10) -> int:
    return int(datetime(2026, month, day, hour, minute, tzinfo=ZONE).timestamp())


@pytest.fixture
def schedule() -> ReviewSchedule:
    return ReviewSchedule(
        "Europe/Moscow", tuple(ReviewWindow(day, 600, 1140) for day in range(5))
    )


def test_review_hours_pause_overnight_and_weekend(schedule: ReviewSchedule) -> None:
    assert decision_deadline(at(6, 23), schedule) == at(7, 13)
    assert decision_deadline(at(9, 18), schedule) == at(12, 12)
    assert decision_deadline(at(5, 19), schedule) == at(6, 13)
    with pytest.raises(ValueError):
        ReviewSchedule("Europe/Moscow", (ReviewWindow(0, 600, 800),
                                         ReviewWindow(0, 700, 900)))


def test_slots_duration_cutoff_closed_days_google_gap_and_horizon(
        schedule: ReviewSchedule) -> None:
    now = at(5, 10)
    monday = Availability(1, at(5, 14), at(5, 18), "Europe/Moscow")
    assert available_slots(now, [monday], 30, "telemost", schedule)[0] == at(5, 15)
    assert at(5, 14, 45) not in available_slots(now, [monday], 30, "telemost", schedule)
    assert at(5, 17, 45) not in available_slots(now, [monday], 30, "telemost", schedule)
    assert available_slots(now, [monday], 45, "zoom", schedule) == []
    assert at(5, 15) in available_slots(now, [monday], 15, "zoom", schedule)

    busy = BusyInterval(at(5, 16), at(5, 16, 30))
    slots = available_slots(now, [monday], 30, "telemost", schedule, [busy])
    assert at(5, 15) in slots
    assert at(5, 15, 15) not in slots
    assert at(5, 16, 45) not in slots
    assert at(5, 17) in slots

    wednesday = Availability(2, at(7, 15), at(7, 17), "Europe/Moscow")
    assert available_slots(now, [wednesday], 30, "telemost", schedule) == []
    exception = Availability(2, at(7, 15), at(7, 17), "Europe/Moscow",
                             is_manual_exception=True)
    assert at(7, 15) in available_slots(now, [exception], 30, "telemost", schedule)
    unpublished = Availability(3, at(6, 15), at(6, 17), "Europe/Moscow",
                               is_published=False)
    assert available_slots(now, [unpublished], 30, "telemost", schedule) == []
    beyond = Availability(4, at(12, 10, 15), at(12, 12), "Europe/Moscow")
    assert available_slots(now, [beyond], 30, "telemost", schedule) == []


def test_two_hour_lead_cancel_boundary_and_calendar_months() -> None:
    now = at(5, 10)
    assert meets_min_lead(now, at(5, 12))
    assert not meets_min_lead(now, at(5, 11, 59))
    assert can_cancel_confirmed(at(5, 14), at(5, 15))
    assert not can_cancel_confirmed(at(5, 14, 1), at(5, 15))
    jan_31 = int(datetime(2026, 1, 31, 10, tzinfo=ZONE).timestamp())
    march_31 = int(datetime(2026, 3, 31, 10, tzinfo=ZONE).timestamp())
    assert add_calendar_months(jan_31, 2, "Europe/Moscow") == march_31
