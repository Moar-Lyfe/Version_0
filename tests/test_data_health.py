"""Checks that separate a broken feed from a bad week."""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.core import data_health
from app.core.analytics import DATA, Severity
from app.core.calendar_rules import WorkingCalendar
from app.data import schema as S
from app.data.excel_loader import FileReport, LoadResult
from app.settings import (
    CalendarSettings,
    DataHealthSettings,
    DataSettings,
    HolidaySettings,
    SourceSettings,
)

TODAY = date(2026, 9, 4)  # a Friday


@pytest.fixture
def calendar() -> WorkingCalendar:
    return WorkingCalendar.from_settings(
        CalendarSettings(exclude_weekdays=("Sunday",), holidays=HolidaySettings())
    )


@pytest.fixture
def settings() -> DataHealthSettings:
    return DataHealthSettings()


def frame_through(newest: date, days: int = 200, rows_per_day: int = 8) -> pd.DataFrame:
    rows = []
    for offset in range(days):
        day = newest - timedelta(days=offset)
        for _ in range(rows_per_day):
            rows.append(
                {
                    S.DATE: pd.Timestamp(day),
                    S.AGENT: "Dana",
                    S.CATEGORY: "Term Life",
                    S.CATEGORY_KEY: S.CATEGORY_1,
                    S.PREMIUM: 100.0,
                    S.SALES: 1.0,
                    S.CHANNEL: "Referral",
                    S.POLICY_ID: "",
                    S.IS_WEB: False,
                    S.SOURCE_FILE: "test",
                    S.SOURCE_SHEET: "0",
                    S.SOURCE_ROW: 2,
                }
            )
    return pd.DataFrame(rows)


def load_result(frame: pd.DataFrame, error: str | None = None) -> LoadResult:
    report = FileReport(path="/data/book.xlsx", sheet="Production", error=error)
    return LoadResult(frame=frame, loaded_at=pd.Timestamp.now().to_pydatetime(),
                      files=[report])


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #


def test_current_data_is_fresh(calendar, settings):
    alert = data_health.check_freshness(frame_through(TODAY), TODAY, calendar, settings)
    assert alert.severity is Severity.OK
    assert alert.scope == DATA
    assert alert.unit == "days"


def test_a_weekend_gap_is_not_a_broken_feed(calendar, settings):
    """Monday morning: the newest row is Saturday's. Sunday is not a failure."""
    monday = date(2026, 9, 7)
    alert = data_health.check_freshness(
        frame_through(date(2026, 9, 5)), monday, calendar, settings
    )
    assert alert.severity is Severity.OK


def test_a_stalled_export_is_caught(calendar, settings):
    # Newest row six calendar days back -> five working days, past critical.
    alert = data_health.check_freshness(
        frame_through(TODAY - timedelta(days=6)), TODAY, calendar, settings
    )
    assert alert.severity is Severity.CRITICAL
    assert "Check the export" in alert.message


def test_freshness_escalates_with_the_gap(calendar, settings):
    warn = data_health.check_freshness(
        frame_through(TODAY - timedelta(days=3)), TODAY, calendar, settings
    )
    assert warn.severity is Severity.WARNING


def test_an_empty_book_is_critical_not_zero(calendar, settings):
    alert = data_health.check_freshness(S.empty_frame(), TODAY, calendar, settings)
    assert alert.severity is Severity.CRITICAL
    assert "not zero" in alert.message


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


def test_steady_volume_passes(calendar, settings):
    alert = data_health.check_volume(frame_through(TODAY), TODAY, calendar, settings)
    assert alert.severity is Severity.OK


def test_a_partial_export_is_caught_even_when_the_date_is_current(calendar, settings):
    """The case freshness cannot see: the file is on time but short."""
    full = frame_through(TODAY, days=200, rows_per_day=8)
    recent = full[S.DATE] >= pd.Timestamp(TODAY - timedelta(days=14))
    # Keep one row in four for the recent fortnight.
    thinned = full.loc[~recent | (full.groupby(recent).cumcount() % 4 == 0)]

    fresh = data_health.check_freshness(thinned, TODAY, calendar, settings)
    assert fresh.severity is Severity.OK  # the date is current...

    volume = data_health.check_volume(thinned, TODAY, calendar, settings)
    assert volume.severity is Severity.CRITICAL  # ...but the rows are not
    assert "partial export" in volume.message


def test_growth_in_volume_is_not_a_problem(calendar, settings):
    full = frame_through(TODAY, days=200, rows_per_day=8)
    recent = full.loc[full[S.DATE] >= pd.Timestamp(TODAY - timedelta(days=14))]
    alert = data_health.check_volume(
        pd.concat([full, recent], ignore_index=True), TODAY, calendar, settings
    )
    assert alert.severity is Severity.OK
    assert "above" in alert.message


def test_volume_needs_a_baseline_before_it_judges(calendar, settings):
    alert = data_health.check_volume(
        frame_through(TODAY, days=20), TODAY, calendar, settings
    )
    assert alert.severity is Severity.NO_DATA


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def test_a_source_matching_nothing_is_critical(tmp_path):
    data_settings = DataSettings(
        sources=(SourceSettings(name="production", path=str(tmp_path / "missing")),)
    )
    alert = data_health.check_sources(load_result(frame_through(TODAY)), data_settings)
    assert alert.severity is Severity.CRITICAL
    assert "matched no files" in alert.message


def test_an_unreadable_sheet_is_a_warning(tmp_path):
    (tmp_path / "book.xlsx").write_bytes(b"x")
    data_settings = DataSettings(
        sources=(SourceSettings(name="production", path=str(tmp_path)),)
    )
    result = load_result(frame_through(TODAY), error="Corrupt workbook")
    alert = data_health.check_sources(result, data_settings)
    assert alert.severity is Severity.WARNING
    assert "could not be read" in alert.message


def test_clean_sources_pass(tmp_path):
    (tmp_path / "book.xlsx").write_bytes(b"x")
    data_settings = DataSettings(
        sources=(SourceSettings(name="production", path=str(tmp_path)),)
    )
    alert = data_health.check_sources(load_result(frame_through(TODAY)), data_settings)
    assert alert.severity is Severity.OK


# --------------------------------------------------------------------------- #
# Together
# --------------------------------------------------------------------------- #


def test_evaluate_can_be_switched_off(calendar, tmp_path):
    result = load_result(frame_through(TODAY))
    off = DataHealthSettings(enabled=False)
    assert data_health.evaluate(
        result, result.frame, TODAY, calendar, off, DataSettings()
    ) == []


def test_data_problems_outrank_performance_problems(calendar, settings, tmp_path):
    """A critical data alert must sort above a critical performance alert.

    If the feed is wrong, what it says about performance is noise -- so the
    reader has to meet the data problem first.
    """
    from app.core.analytics import Alert

    data_settings = DataSettings(
        sources=(SourceSettings(name="gone", path=str(tmp_path / "missing")),)
    )
    result = load_result(frame_through(TODAY - timedelta(days=10)))
    health = data_health.evaluate(
        result, result.frame, TODAY, calendar, settings, data_settings
    )
    assert data_health.is_compromised(health)

    performance = Alert(
        rule="Premium vs baseline",
        metric="premium",
        metric_label="Premium",
        scope="Overall",
        kind="relative",
        window=15,
        severity=Severity.CRITICAL,
        current=1.0,
        reference=2.0,
        reference_label="90-day baseline",
        shortfall_pct=50.0,
        message="down",
        is_currency=True,
    )
    ordered = sorted(health + [performance], key=lambda a: a.sort_key)
    assert ordered[0].scope == DATA


def test_healthy_data_is_not_compromised(calendar, settings, tmp_path):
    (tmp_path / "book.xlsx").write_bytes(b"x")
    data_settings = DataSettings(
        sources=(SourceSettings(name="production", path=str(tmp_path)),)
    )
    result = load_result(frame_through(TODAY))
    health = data_health.evaluate(
        result, result.frame, TODAY, calendar, settings, data_settings
    )
    assert not data_health.is_compromised(health)
    assert all(a.severity is Severity.OK for a in health)
