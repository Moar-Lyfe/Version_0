"""The archive of what was reported, and restatement detection."""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.core import snapshots
from app.data import schema as S
from app.settings import CategorySettings, DataSettings, SnapshotSettings

TODAY = date(2026, 9, 4)


@pytest.fixture
def data_settings() -> DataSettings:
    return DataSettings(
        category_1=CategorySettings("category_1", "Life", ("Term Life",)),
        category_2=CategorySettings("category_2", "Health", ("Medicare",)),
    )


@pytest.fixture
def settings(tmp_path) -> SnapshotSettings:
    return SnapshotSettings(directory=str(tmp_path / "snapshots"), daily_days=30)


def make_frame(daily: dict[date, float]) -> pd.DataFrame:
    rows = [
        {
            S.DATE: pd.Timestamp(day),
            S.AGENT: "Dana",
            S.CATEGORY: "Term Life",
            S.CATEGORY_KEY: S.CATEGORY_1,
            S.PREMIUM: float(value),
            S.SALES: 1.0,
            S.CHANNEL: "Referral",
            S.POLICY_ID: "",
            S.IS_WEB: False,
            S.SOURCE_FILE: "test",
            S.SOURCE_SHEET: "0",
            S.SOURCE_ROW: 2,
        }
        for day, value in sorted(daily.items())
    ]
    return pd.DataFrame(rows)


def flat(end: date, days: int, value: float = 100.0) -> pd.DataFrame:
    return make_frame({end - timedelta(days=offset): value for offset in range(days)})


# --------------------------------------------------------------------------- #
# Taking a snapshot
# --------------------------------------------------------------------------- #


def test_a_snapshot_records_every_period_and_metric(settings, data_settings):
    frame = flat(TODAY, 120)
    period_path, daily_path = snapshots.take(frame, data_settings, settings, TODAY)

    assert period_path.exists() and daily_path.exists()
    recorded = snapshots.as_reported(settings, TODAY)
    # Five reporting windows x four KPIs.
    assert len(recorded) == 20
    assert set(recorded["period"]) == {
        "today", "yesterday", "rolling_7", "month", "year"
    }


def test_the_recorded_figures_match_what_was_computed(settings, data_settings):
    frame = flat(TODAY, 120, value=250.0)
    snapshots.take(frame, data_settings, settings, TODAY)

    rows = snapshots.as_reported(settings, TODAY, period="today")
    premium = rows.loc[rows["metric"] == "premium", "value"].iloc[0]
    assert premium == pytest.approx(250.0)


def test_re_running_the_same_day_replaces_rather_than_duplicates(
    settings, data_settings
):
    """A snapshot step must be safe to run twice."""
    snapshots.take(flat(TODAY, 120), data_settings, settings, TODAY)
    snapshots.take(flat(TODAY, 120), data_settings, settings, TODAY)

    assert len(snapshots.as_reported(settings, TODAY)) == 20
    assert snapshots.snapshot_dates(settings) == [TODAY]


def test_successive_days_accumulate(settings, data_settings):
    yesterday = TODAY - timedelta(days=1)
    snapshots.take(flat(yesterday, 120), data_settings, settings, yesterday)
    snapshots.take(flat(TODAY, 120), data_settings, settings, TODAY)

    assert snapshots.snapshot_dates(settings) == [yesterday, TODAY]


def test_nothing_recorded_yet_reads_as_empty(settings):
    assert snapshots.snapshot_dates(settings) == []
    assert snapshots.history(settings).empty
    assert snapshots.as_reported(settings, TODAY).empty


# --------------------------------------------------------------------------- #
# Restatements
# --------------------------------------------------------------------------- #


def test_a_changed_closed_day_is_detected(settings, data_settings):
    yesterday = TODAY - timedelta(days=1)
    original = flat(TODAY, 60, value=100.0)
    snapshots.take(original, data_settings, settings, yesterday)

    # A row for a closed day is corrected after the fact.
    restated = original.copy()
    target_day = pd.Timestamp(TODAY - timedelta(days=10))
    restated.loc[restated[S.DATE] == target_day, S.PREMIUM] = 500.0

    found = snapshots.find_restatements(restated, settings, TODAY)
    premium = [r for r in found if r.metric == "premium"]
    assert len(premium) == 1
    assert premium[0].day == TODAY - timedelta(days=10)
    assert premium[0].was == pytest.approx(100.0)
    assert premium[0].now == pytest.approx(500.0)
    assert premium[0].delta == pytest.approx(400.0)
    assert premium[0].delta_pct == pytest.approx(400.0)


def test_unchanged_data_reports_nothing(settings, data_settings):
    yesterday = TODAY - timedelta(days=1)
    frame = flat(TODAY, 60)
    snapshots.take(frame, data_settings, settings, yesterday)
    assert snapshots.find_restatements(frame, settings, TODAY) == []


def test_only_earlier_snapshots_are_compared_against(settings, data_settings):
    """Today's own snapshot must not become the thing today is compared with."""
    frame = flat(TODAY, 60)
    snapshots.take(frame, data_settings, settings, TODAY)

    changed = frame.copy()
    changed.loc[
        changed[S.DATE] == pd.Timestamp(TODAY - timedelta(days=5)), S.PREMIUM
    ] = 999.0
    assert snapshots.find_restatements(changed, settings, TODAY) == []


def test_no_archive_means_no_findings(settings):
    assert snapshots.find_restatements(flat(TODAY, 60), settings, TODAY) == []


def test_float_noise_is_not_a_restatement(settings, data_settings):
    yesterday = TODAY - timedelta(days=1)
    frame = flat(TODAY, 60, value=100.0)
    snapshots.take(frame, data_settings, settings, yesterday)

    jittered = frame.copy()
    jittered[S.PREMIUM] = jittered[S.PREMIUM] + 0.001
    assert snapshots.find_restatements(jittered, settings, TODAY) == []


def test_a_new_late_entry_on_a_closed_day_is_a_restatement(settings, data_settings):
    yesterday = TODAY - timedelta(days=1)
    frame = flat(TODAY, 60, value=100.0)
    snapshots.take(frame, data_settings, settings, yesterday)

    late = pd.concat(
        [frame, make_frame({TODAY - timedelta(days=8): 400.0})], ignore_index=True
    )
    found = [r for r in snapshots.find_restatements(late, settings, TODAY)
             if r.metric == "premium"]
    assert len(found) == 1
    assert found[0].now == pytest.approx(500.0)


def test_a_corrupt_archive_does_not_crash(settings, data_settings):
    directory = settings.resolved_directory()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / snapshots.DAILY_FILE).write_text("not,a,valid\ncsv\"", encoding="utf-8")
    assert snapshots.find_restatements(flat(TODAY, 60), settings, TODAY) == []


def test_daily_rows_carry_a_row_count(settings):
    frame = flat(TODAY, 10)
    rows = snapshots.daily_rows(frame, TODAY, days=10)
    assert list(rows["rows"])[-1] == 1
    assert set(rows.columns) >= {"as_of", "date", "premium", "sales", "rows"}
