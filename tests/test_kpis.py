"""KPI aggregation, the web-sales toggle, and the agent pivot."""

from datetime import date

import pandas as pd
import pytest

from app.core import kpis
from app.data import schema as S
from app.settings import CategorySettings, DataSettings


@pytest.fixture
def settings() -> DataSettings:
    return DataSettings(
        category_1=CategorySettings("category_1", "Life", ("Term Life",)),
        category_2=CategorySettings("category_2", "Health", ("Medicare",)),
    )


@pytest.fixture
def frame() -> pd.DataFrame:
    rows = [
        # date,        agent,   category_key,      premium, sales, is_web
        ("2026-09-01", "Dana",  S.CATEGORY_1,      1000.0,  1.0,   False),
        ("2026-09-01", "Dana",  S.CATEGORY_2,       500.0,  1.0,   False),
        ("2026-09-02", "Marc",  S.CATEGORY_1,       750.0,  2.0,   False),
        ("2026-09-02", "Web",   S.CATEGORY_2,       250.0,  1.0,   True),
        ("2026-09-03", "Marc",  S.CATEGORY_OTHER,  2000.0,  1.0,   False),
    ]
    frame = pd.DataFrame(
        rows, columns=[S.DATE, S.AGENT, S.CATEGORY_KEY, S.PREMIUM, S.SALES, S.IS_WEB]
    )
    frame[S.DATE] = pd.to_datetime(frame[S.DATE])
    frame[S.CATEGORY] = frame[S.CATEGORY_KEY]
    frame[S.CHANNEL] = ""
    return frame


def test_totals_include_uncategorised_rows(frame):
    values = kpis.compute(frame)
    assert values.premium == 4500.0
    assert values.sales == 6.0            # the "other" row still counts as a sale
    assert values.category_1 == 3.0
    assert values.category_2 == 2.0


def test_window_is_inclusive_at_both_ends(frame):
    values = kpis.compute_window(frame, date(2026, 9, 1), date(2026, 9, 2))
    assert values.premium == 2500.0
    assert values.sales == 5.0
    assert kpis.compute_window(frame, date(2026, 9, 9), date(2026, 9, 9)).premium == 0.0


def test_excluding_web_sales_removes_them_from_every_kpi(frame):
    filtered = kpis.apply_web_filter(frame, include_web=False)
    values = kpis.compute(filtered)
    assert values.premium == 4250.0
    assert values.sales == 5.0
    assert values.category_2 == 1.0       # the web row was the second cat-2 sale
    # And the toggle is a no-op the other way.
    assert kpis.compute(kpis.apply_web_filter(frame, True)).premium == 4500.0


def test_empty_frame_yields_zeroes():
    values = kpis.compute(S.empty_frame())
    assert values.as_dict() == {
        "premium": 0.0,
        "sales": 0.0,
        "category_1": 0.0,
        "category_2": 0.0,
    }


def test_kpi_labels_follow_configuration(settings):
    labels = [definition.label for definition in kpis.kpi_definitions(settings)]
    assert labels == ["Premium", "Total Sales", "Life Sales", "Health Sales"]


def test_breakdown_is_a_pivot_sorted_by_premium(frame, settings):
    table = kpis.breakdown(frame, S.AGENT, settings)
    assert list(table.columns) == [
        S.AGENT,
        "Premium",
        "Total Sales",
        "Life Sales",
        "Health Sales",
        "% of Premium",
    ]
    assert list(table[S.AGENT]) == ["Marc", "Dana", "Web"]
    # The pivot must reconcile against the headline KPIs.
    assert table["Premium"].sum() == kpis.compute(frame).premium
    assert table["Total Sales"].sum() == kpis.compute(frame).sales
    assert round(table["% of Premium"].sum(), 6) == 100.0


def test_breakdown_of_an_empty_frame_has_the_right_shape(settings):
    table = kpis.breakdown(S.empty_frame(), S.AGENT, settings)
    assert table.empty
    assert "% of Premium" in table.columns


def test_daily_series_fills_missing_days(frame):
    series = kpis.daily_series(frame, date(2026, 9, 1), date(2026, 9, 5))
    assert len(series) == 5
    assert series[S.PREMIUM].iloc[0] == 1500.0
    assert series[S.PREMIUM].iloc[3] == 0.0        # nothing booked on the 4th
    assert series[S.PREMIUM].sum() == 4500.0
