"""The working calendar is what every projection divides by, so pin it down."""

from datetime import date

import pytest

from app.core.calendar_rules import WorkingCalendar, holiday_dates
from app.settings import CalendarSettings, HolidaySettings

MAJOR = (
    "new_years_day",
    "memorial_day",
    "independence_day",
    "labor_day",
    "thanksgiving",
    "christmas_day",
)


def build(**kwargs) -> WorkingCalendar:
    settings = CalendarSettings(
        exclude_weekdays=kwargs.pop("exclude_weekdays", ("Sunday",)),
        holidays=HolidaySettings(observed=kwargs.pop("observed", MAJOR), **kwargs),
    )
    return WorkingCalendar.from_settings(settings)


def test_floating_holidays_land_on_the_right_day():
    dates = holiday_dates(2026, MAJOR)
    assert dates["memorial_day"] == date(2026, 5, 25)      # last Monday of May
    assert dates["labor_day"] == date(2026, 9, 7)          # first Monday of Sept
    assert dates["thanksgiving"] == date(2026, 11, 26)     # fourth Thursday
    assert dates["christmas_day"] == date(2026, 12, 25)


def test_good_friday_tracks_easter():
    assert holiday_dates(2026, ("good_friday",))["good_friday"] == date(2026, 4, 3)
    assert holiday_dates(2027, ("good_friday",))["good_friday"] == date(2027, 3, 26)


def test_sunday_holiday_shifts_to_monday():
    # July 4 2027 is a Sunday, which is already closed -- observe it on Monday.
    calendar = build()
    assert calendar.holidays_for_year(2027)[date(2027, 7, 5)] == "Independence Day"
    assert calendar.is_working_day(date(2027, 7, 5)) is False


def test_sunday_holiday_stays_put_when_shifting_is_off():
    calendar = build(shift_to_next_working_day=False)
    assert date(2027, 7, 4) in calendar.holidays_for_year(2027)
    assert calendar.is_working_day(date(2027, 7, 5)) is True


def test_sundays_are_never_working_days():
    calendar = build()
    assert calendar.is_working_day(date(2026, 9, 6)) is False   # Sunday
    assert calendar.is_working_day(date(2026, 9, 5)) is True    # Saturday


def test_working_day_counts():
    calendar = build()
    # September 2026: 30 days, 4 Sundays, Labor Day on the 7th.
    assert calendar.working_days_between(date(2026, 9, 1), date(2026, 9, 30)) == 25
    assert calendar.working_days_between(date(2026, 9, 10), date(2026, 9, 9)) == 0
    assert calendar.working_days_between(date(2026, 9, 4), date(2026, 9, 4)) == 1


def test_extra_closure_and_override():
    calendar = build(
        extra_dates=(date(2026, 9, 4),), working_overrides=(date(2026, 9, 6),)
    )
    assert calendar.is_working_day(date(2026, 9, 4)) is False
    assert calendar.holiday_name(date(2026, 9, 4)) == "Company closure"
    # An override beats every exclusion rule, Sundays included.
    assert calendar.is_working_day(date(2026, 9, 6)) is True


def test_non_working_days_are_explained():
    calendar = build()
    skipped = dict(calendar.non_working_days_between(date(2026, 9, 1), date(2026, 9, 8)))
    assert skipped[date(2026, 9, 6)] == "Sunday (closed)"
    assert skipped[date(2026, 9, 7)] == "Labor Day"


@pytest.mark.parametrize("year", range(2024, 2035))
def test_every_year_resolves_without_collisions(year):
    calendar = build()
    holidays = calendar.holidays_for_year(year)
    assert len(holidays) == len(MAJOR)
