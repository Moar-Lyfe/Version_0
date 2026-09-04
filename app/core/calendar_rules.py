"""Working-day calendar.

A working day is any date that is not an excluded weekday (Sunday by default),
not an observed holiday, and not a one-off closure -- unless it appears in
``working_overrides``, which always wins.

Holidays are computed rather than hard-coded so the calendar stays correct in
future years without anyone editing a list. Only the observances named in
``calendar.holidays.observed`` are applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from app.settings import CalendarSettings

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAY_INDEX = {name.lower(): index for index, name in enumerate(WEEKDAY_NAMES)}
# Common shorthand so "Sun" and "sunday" both work.
_WEEKDAY_INDEX.update({name[:3].lower(): index for index, name in enumerate(WEEKDAY_NAMES)})

HOLIDAY_LABELS = {
    "new_years_day": "New Year's Day",
    "mlk_day": "Martin Luther King Jr. Day",
    "presidents_day": "Presidents' Day",
    "good_friday": "Good Friday",
    "memorial_day": "Memorial Day",
    "juneteenth": "Juneteenth",
    "independence_day": "Independence Day",
    "labor_day": "Labor Day",
    "columbus_day": "Columbus Day",
    "veterans_day": "Veterans Day",
    "thanksgiving": "Thanksgiving Day",
    "day_after_thanksgiving": "Day After Thanksgiving",
    "christmas_eve": "Christmas Eve",
    "christmas_day": "Christmas Day",
    "new_years_eve": "New Year's Eve",
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The *n*-th ``weekday`` (0=Monday) of ``month``; ``n`` is 1-based."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The final ``weekday`` of ``month``."""
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous / Meeus algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    j = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * j) // 451
    month, day = divmod(h + j - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holiday_dates(year: int, keys: tuple[str, ...]) -> dict[str, date]:
    """Unshifted dates for the requested observances in ``year``."""
    builders = {
        "new_years_day": lambda: date(year, 1, 1),
        "mlk_day": lambda: _nth_weekday(year, 1, 0, 3),
        "presidents_day": lambda: _nth_weekday(year, 2, 0, 3),
        "good_friday": lambda: _easter(year) - timedelta(days=2),
        "memorial_day": lambda: _last_weekday(year, 5, 0),
        "juneteenth": lambda: date(year, 6, 19),
        "independence_day": lambda: date(year, 7, 4),
        "labor_day": lambda: _nth_weekday(year, 9, 0, 1),
        "columbus_day": lambda: _nth_weekday(year, 10, 0, 2),
        "veterans_day": lambda: date(year, 11, 11),
        "thanksgiving": lambda: _nth_weekday(year, 11, 3, 4),
        "day_after_thanksgiving": lambda: _nth_weekday(year, 11, 3, 4)
        + timedelta(days=1),
        "christmas_eve": lambda: date(year, 12, 24),
        "christmas_day": lambda: date(year, 12, 25),
        "new_years_eve": lambda: date(year, 12, 31),
    }
    return {key: builders[key]() for key in keys if key in builders}


@dataclass(frozen=True)
class WorkingCalendar:
    """Answers "is this a working day?" and "how many between these dates?"."""

    excluded_weekdays: frozenset[int]
    observed_keys: tuple[str, ...]
    shift_to_next_working_day: bool
    extra_closures: frozenset[date]
    working_overrides: frozenset[date]

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_settings(cls, settings: CalendarSettings) -> "WorkingCalendar":
        excluded = {
            _WEEKDAY_INDEX[name.strip().lower()]
            for name in settings.exclude_weekdays
            if name.strip().lower() in _WEEKDAY_INDEX
        }
        return cls(
            excluded_weekdays=frozenset(excluded),
            observed_keys=tuple(
                key for key in settings.holidays.observed if key in HOLIDAY_LABELS
            ),
            shift_to_next_working_day=settings.holidays.shift_to_next_working_day,
            extra_closures=frozenset(settings.holidays.extra_dates),
            working_overrides=frozenset(settings.holidays.working_overrides),
        )

    # -- holidays ---------------------------------------------------------- #

    def _is_weekday_excluded(self, day: date) -> bool:
        return day.weekday() in self.excluded_weekdays

    @lru_cache(maxsize=64)
    def holidays_for_year(self, year: int) -> dict[date, str]:
        """Observed holiday dates in ``year``, after any shifting."""
        resolved: dict[date, str] = {}
        # A holiday landing on a non-working day costs the business nothing, so
        # roll it forward to the next day that would otherwise be worked.
        for key, day in holiday_dates(year, self.observed_keys).items():
            if self.shift_to_next_working_day:
                guard = 0
                while self._is_weekday_excluded(day) or day in resolved:
                    day += timedelta(days=1)
                    guard += 1
                    if guard > 7:  # pathological config; stop rolling
                        break
            resolved.setdefault(day, HOLIDAY_LABELS[key])
        return resolved

    def holiday_name(self, day: date) -> str | None:
        if day in self.extra_closures:
            return "Company closure"
        return self.holidays_for_year(day.year).get(day)

    # -- working days ------------------------------------------------------ #

    def is_working_day(self, day: date) -> bool:
        if day in self.working_overrides:
            return True
        if self._is_weekday_excluded(day):
            return False
        if day in self.extra_closures:
            return False
        return day not in self.holidays_for_year(day.year)

    def working_days_between(self, start: date, end: date) -> int:
        """Inclusive count of working days in ``[start, end]``."""
        if end < start:
            return 0
        return sum(
            1
            for offset in range((end - start).days + 1)
            if self.is_working_day(start + timedelta(days=offset))
        )

    def next_working_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        for _ in range(370):
            if self.is_working_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def non_working_days_between(self, start: date, end: date) -> list[tuple[date, str]]:
        """Every skipped day in the window with the reason, for diagnostics."""
        out: list[tuple[date, str]] = []
        for offset in range(max((end - start).days + 1, 0)):
            day = start + timedelta(days=offset)
            if self.is_working_day(day):
                continue
            reason = self.holiday_name(day) or f"{WEEKDAY_NAMES[day.weekday()]} (closed)"
            out.append((day, reason))
        return out
