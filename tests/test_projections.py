"""Straight-line projection maths against the working calendar."""

from datetime import date

import pytest

from app.core.calendar_rules import WorkingCalendar
from app.core.kpis import KpiValues
from app.core.periods import MONTH, ROLLING_7, YEAR, build_period
from app.core.projections import build_basis, project
from app.settings import CalendarSettings, HolidaySettings, ProjectionSettings


@pytest.fixture
def calendar() -> WorkingCalendar:
    return WorkingCalendar.from_settings(
        CalendarSettings(
            exclude_weekdays=("Sunday",),
            holidays=HolidaySettings(observed=("labor_day",)),
        )
    )


def test_rolling_windows_are_not_projectable(calendar):
    period = build_period(ROLLING_7, date(2026, 9, 4))
    assert build_basis(period, date(2026, 9, 4), calendar, ProjectionSettings()) is None


def test_month_basis_counts_only_working_days(calendar):
    period = build_period(MONTH, date(2026, 9, 4))
    basis = build_basis(period, date(2026, 9, 4), calendar, ProjectionSettings())
    # Sep 1-4 with no Sunday and no holiday in the way.
    assert basis.elapsed == 4
    # 30 days minus 4 Sundays minus Labor Day.
    assert basis.total == 25
    assert basis.remaining == 21
    assert round(basis.completion, 4) == 0.16


def test_excluding_today_drops_one_elapsed_day(calendar):
    period = build_period(MONTH, date(2026, 9, 4))
    settings = ProjectionSettings(count_today_as_elapsed=False)
    basis = build_basis(period, date(2026, 9, 4), calendar, settings)
    assert basis.elapsed == 3
    assert basis.counts_today is False


def test_projection_scales_the_daily_pace_over_the_period(calendar):
    period = build_period(MONTH, date(2026, 9, 4))
    basis = build_basis(period, date(2026, 9, 4), calendar, ProjectionSettings())
    values = KpiValues(premium=100_000.0, sales=40.0, category_1=25.0, category_2=12.0)

    projected = project(values, basis)
    assert projected["premium"].daily_pace == 25_000.0
    assert projected["premium"].projected == 625_000.0     # 25,000 x 25 days
    assert projected["premium"].actual == 100_000.0
    assert projected["premium"].remaining == 525_000.0
    assert projected["sales"].projected == pytest.approx(250.0)


def test_a_closed_period_projects_to_its_actual(calendar):
    # Standing in January, December's month is fully elapsed.
    period = build_period(MONTH, date(2026, 12, 31))
    basis = build_basis(period, date(2027, 1, 15), calendar, ProjectionSettings())
    assert basis.elapsed == basis.total
    projected = project(KpiValues(premium=50_000.0), basis)
    assert projected["premium"].projected == pytest.approx(50_000.0)
    assert projected["premium"].remaining == 0.0


def test_zero_elapsed_days_produces_no_projection(calendar):
    # Jan 1 is not observed here, but it is a Sunday-free Thursday in 2026;
    # force the degenerate case with a period whose elapsed count is zero.
    period = build_period(YEAR, date(2026, 1, 1))
    settings = ProjectionSettings(count_today_as_elapsed=False)
    basis = build_basis(period, date(2026, 1, 1), calendar, settings)
    assert basis.elapsed == 0
    assert basis.is_valid is False
    assert project(KpiValues(premium=1.0), basis) == {}


def test_no_basis_means_no_projection():
    assert project(KpiValues(premium=1.0), None) == {}
