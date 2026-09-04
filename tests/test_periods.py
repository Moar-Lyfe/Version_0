"""Period windows, including the comparison ranges the deltas rely on."""

from datetime import date

import pytest

from app.core.periods import (
    MONTH,
    PERIOD_ORDER,
    ROLLING_7,
    TODAY,
    YEAR,
    YESTERDAY,
    build_period,
    build_periods,
)

TODAY_DATE = date(2026, 9, 4)  # a Friday


def test_today_and_yesterday_are_single_days():
    today = build_period(TODAY, TODAY_DATE)
    assert today.start == today.end == TODAY_DATE
    assert today.prior_start == today.prior_end == date(2026, 9, 3)

    yesterday = build_period(YESTERDAY, TODAY_DATE)
    assert yesterday.start == yesterday.end == date(2026, 9, 3)
    assert yesterday.prior_start == date(2026, 9, 2)


def test_rolling_7_includes_today_and_six_days_back():
    period = build_period(ROLLING_7, TODAY_DATE)
    assert period.start == date(2026, 8, 29)
    assert period.end == TODAY_DATE
    assert period.days == 7
    # The comparison window is the seven days immediately before it.
    assert (period.prior_start, period.prior_end) == (date(2026, 8, 22), date(2026, 8, 28))
    assert period.is_projectable is False


def test_month_to_date_and_its_full_window():
    period = build_period(MONTH, TODAY_DATE)
    assert (period.start, period.end) == (date(2026, 9, 1), TODAY_DATE)
    assert (period.full_start, period.full_end) == (date(2026, 9, 1), date(2026, 9, 30))
    # Same slice of the previous month, so the delta is like-for-like.
    assert (period.prior_start, period.prior_end) == (date(2026, 8, 1), date(2026, 8, 4))
    assert period.is_projectable is True


def test_month_comparison_clamps_to_a_shorter_month():
    # March 31 has no counterpart in February; clamp rather than overflow.
    period = build_period(MONTH, date(2026, 3, 31))
    assert period.prior_end == date(2026, 2, 28)


def test_year_to_date_compares_against_the_same_point_last_year():
    period = build_period(YEAR, TODAY_DATE)
    assert (period.start, period.end) == (date(2026, 1, 1), TODAY_DATE)
    assert (period.full_start, period.full_end) == (date(2026, 1, 1), date(2026, 12, 31))
    assert (period.prior_start, period.prior_end) == (date(2025, 1, 1), date(2025, 9, 4))


def test_leap_day_year_comparison_clamps():
    period = build_period(YEAR, date(2028, 2, 29))
    assert period.prior_end == date(2027, 2, 28)


def test_build_periods_covers_every_key():
    periods = build_periods(TODAY_DATE)
    assert tuple(periods) == PERIOD_ORDER
    assert all(p.contains(p.end) for p in periods.values())


def test_unknown_period_is_rejected():
    with pytest.raises(ValueError):
        build_period("quarter", TODAY_DATE)
