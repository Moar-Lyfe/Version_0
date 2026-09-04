"""Targets: attainment, the pace they imply, and what "no target" means."""

from datetime import date

import pytest

from app.core import targets
from app.core.calendar_rules import WorkingCalendar
from app.core.periods import MONTH, ROLLING_7, YEAR, build_period, build_periods
from app.core.projections import build_basis
from app.settings import (
    CalendarSettings,
    HolidaySettings,
    ProjectionSettings,
    TargetSettings,
)

TODAY = date(2026, 9, 4)


@pytest.fixture
def calendar() -> WorkingCalendar:
    return WorkingCalendar.from_settings(
        CalendarSettings(exclude_weekdays=("Sunday",), holidays=HolidaySettings())
    )


@pytest.fixture
def settings() -> TargetSettings:
    return TargetSettings(
        values={"premium": {"month": 700_000.0, "year": 8_500_000.0}},
        overrides={"premium": {"2026-12": 950_000.0}},
    )


def month_basis(calendar, today=TODAY):
    return build_basis(
        build_period(MONTH, today), today, calendar, ProjectionSettings()
    )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def test_a_month_target_resolves(settings):
    target = targets.resolve(settings, "premium", build_period(MONTH, TODAY))
    assert target.value == 700_000.0
    assert target.label == "September target"
    assert target.is_override is False


def test_a_month_override_wins(settings):
    target = targets.resolve(settings, "premium", build_period(MONTH, date(2026, 12, 3)))
    assert target.value == 950_000.0
    assert target.is_override is True
    assert target.source == "month override"


def test_an_override_for_another_month_is_ignored(settings):
    target = targets.resolve(settings, "premium", build_period(MONTH, date(2026, 11, 3)))
    assert target.value == 700_000.0


def test_a_metric_without_a_target_returns_none(settings):
    """Absent means "no target", never zero."""
    assert targets.resolve(settings, "sales", build_period(MONTH, TODAY)) is None


def test_only_month_and_year_carry_targets(settings):
    assert targets.resolve(settings, "premium", build_period(ROLLING_7, TODAY)) is None


def test_targets_can_be_switched_off():
    off = TargetSettings(enabled=False, values={"premium": {"month": 1.0}})
    assert targets.resolve(off, "premium", build_period(MONTH, TODAY)) is None
    assert off.has_any() is False


# --------------------------------------------------------------------------- #
# Attainment
# --------------------------------------------------------------------------- #


def test_attainment_measures_booked_and_projected(settings, calendar):
    basis = month_basis(calendar)
    result = targets.measure(
        settings, "premium", build_period(MONTH, TODAY),
        actual=140_000.0, projected=770_000.0, basis=basis,
    )
    assert result.achieved_pct == pytest.approx(20.0)
    assert result.projected_pct == pytest.approx(110.0)
    assert result.on_track is True
    assert result.is_met is False
    assert result.gap == pytest.approx(560_000.0)


def test_required_pace_uses_the_working_days_left(settings, calendar):
    basis = month_basis(calendar)
    result = targets.measure(
        settings, "premium", build_period(MONTH, TODAY), actual=140_000.0, basis=basis
    )
    # This fixture observes no holidays, so September 2026 is 30 days less
    # four Sundays = 26 working days; 4 elapsed, 22 to go.
    assert basis.remaining == 22
    assert result.required_pace == pytest.approx(560_000.0 / 22)


def test_a_met_target_needs_nothing_more(settings, calendar):
    result = targets.measure(
        settings, "premium", build_period(MONTH, TODAY),
        actual=800_000.0, projected=900_000.0, basis=month_basis(calendar),
    )
    assert result.is_met is True
    assert result.gap == 0.0
    assert result.required_pace == 0.0
    assert result.achieved_pct > 100


def test_a_closed_period_has_no_pace_to_set(settings, calendar):
    """Nothing left to run is not the same answer as "nothing more needed"."""
    closed = build_period(MONTH, date(2026, 9, 30))
    basis = build_basis(closed, date(2026, 9, 30), calendar, ProjectionSettings())
    result = targets.measure(
        settings, "premium", closed, actual=500_000.0, basis=basis
    )
    assert basis.remaining == 0
    assert result.is_met is False
    assert result.required_pace is None


def test_current_pace_is_per_elapsed_working_day(settings, calendar):
    basis = month_basis(calendar)
    result = targets.measure(
        settings, "premium", build_period(MONTH, TODAY), actual=140_000.0, basis=basis
    )
    assert result.current_pace == pytest.approx(140_000.0 / 4)


def test_without_a_projection_on_track_is_unknown(settings, calendar):
    result = targets.measure(
        settings, "premium", build_period(MONTH, TODAY),
        actual=1.0, basis=month_basis(calendar),
    )
    assert result.on_track is None
    assert result.projected_pct is None


def test_measure_returns_none_when_there_is_no_target(settings, calendar):
    assert targets.measure(
        settings, "sales", build_period(MONTH, TODAY), actual=1.0
    ) is None


# --------------------------------------------------------------------------- #
# Daily pace, for the moving-average chart
# --------------------------------------------------------------------------- #


def test_daily_pace_prefers_the_month_target(settings, calendar):
    periods = build_periods(TODAY)
    working = {MONTH: 25, YEAR: 307}
    pace, label = targets.daily_pace_target(settings, "premium", periods, working)
    assert pace == pytest.approx(700_000.0 / 25)
    assert label == "month target pace"


def test_daily_pace_falls_back_to_the_year(calendar):
    settings = TargetSettings(values={"premium": {"year": 8_500_000.0}})
    pace, label = targets.daily_pace_target(
        settings, "premium", build_periods(TODAY), {MONTH: 25, YEAR: 307}
    )
    assert pace == pytest.approx(8_500_000.0 / 307)
    assert label == "year target pace"


def test_no_target_means_no_line(settings):
    assert targets.daily_pace_target(
        settings, "sales", build_periods(TODAY), {MONTH: 25, YEAR: 307}
    ) is None


def test_zero_working_days_produces_no_line(settings):
    assert targets.daily_pace_target(
        settings, "premium", build_periods(TODAY), {MONTH: 0, YEAR: 0}
    ) is None
