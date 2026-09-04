"""Reporting windows.

One place defines what "Today", "Yesterday", "Rolling 7", "Month to date" and
"Year to date" mean, along with the comparable prior window used for deltas and
the full calendar window used for projections.
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, timedelta

TODAY = "today"
YESTERDAY = "yesterday"
ROLLING_7 = "rolling_7"
MONTH = "month"
YEAR = "year"

PERIOD_ORDER = (TODAY, YESTERDAY, ROLLING_7, MONTH, YEAR)


@dataclass(frozen=True)
class Period:
    """An inclusive date window plus everything the UI needs to describe it."""

    key: str
    label: str
    start: date
    end: date
    # Equivalent earlier window, used for period-over-period deltas.
    prior_start: date
    prior_end: date
    # Full calendar window this period will eventually fill (month/year only).
    full_start: date | None = None
    full_end: date | None = None
    comparison_label: str = ""

    @property
    def is_projectable(self) -> bool:
        return self.full_start is not None and self.full_end is not None

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def _clamp_day_of_month(year: int, month: int, day: int) -> date:
    last = _calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _shift_year(day: date, years: int) -> date:
    """Same calendar date N years away, clamping Feb 29 onto Feb 28."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def build_period(key: str, today: date) -> Period:
    """Construct one reporting window anchored on ``today``."""
    if key == TODAY:
        yesterday = today - timedelta(days=1)
        return Period(
            key=key,
            label="Today",
            start=today,
            end=today,
            prior_start=yesterday,
            prior_end=yesterday,
            comparison_label="vs yesterday",
        )

    if key == YESTERDAY:
        yesterday = today - timedelta(days=1)
        before = today - timedelta(days=2)
        return Period(
            key=key,
            label="Yesterday",
            start=yesterday,
            end=yesterday,
            prior_start=before,
            prior_end=before,
            comparison_label="vs prior day",
        )

    if key == ROLLING_7:
        start = today - timedelta(days=6)
        return Period(
            key=key,
            label="Rolling 7 days",
            start=start,
            end=today,
            prior_start=start - timedelta(days=7),
            prior_end=today - timedelta(days=7),
            comparison_label="vs prior 7 days",
        )

    if key == MONTH:
        start = today.replace(day=1)
        last_day = _calendar.monthrange(today.year, today.month)[1]
        prior_month_end = start - timedelta(days=1)
        prior_start = prior_month_end.replace(day=1)
        return Period(
            key=key,
            label="Month to date",
            start=start,
            end=today,
            # Same slice of the previous month, so the comparison is like-for-like.
            prior_start=prior_start,
            prior_end=_clamp_day_of_month(
                prior_start.year, prior_start.month, today.day
            ),
            full_start=start,
            full_end=date(today.year, today.month, last_day),
            comparison_label="vs same point last month",
        )

    if key == YEAR:
        start = date(today.year, 1, 1)
        return Period(
            key=key,
            label="Year to date",
            start=start,
            end=today,
            prior_start=date(today.year - 1, 1, 1),
            prior_end=_shift_year(today, -1),
            full_start=start,
            full_end=date(today.year, 12, 31),
            comparison_label="vs same point last year",
        )

    raise ValueError(f"Unknown period key: {key!r}")


def build_periods(today: date, keys: tuple[str, ...] = PERIOD_ORDER) -> dict[str, Period]:
    return {key: build_period(key, today) for key in keys}
