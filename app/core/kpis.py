"""The four headline KPIs and the agent-level breakout behind them.

Every number on the dashboard comes from this module, so a KPI and the pivot
row that explains it can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.data import schema as S
from app.settings import DataSettings

PREMIUM = "premium"
SALES = "sales"
CATEGORY_1 = "category_1"
CATEGORY_2 = "category_2"

KPI_KEYS = (PREMIUM, SALES, CATEGORY_1, CATEGORY_2)


@dataclass(frozen=True)
class KpiDefinition:
    key: str
    label: str
    is_currency: bool


def kpi_definitions(settings: DataSettings) -> tuple[KpiDefinition, ...]:
    """KPI metadata, with category labels taken from configuration."""
    return (
        KpiDefinition(PREMIUM, "Premium", True),
        KpiDefinition(SALES, "Total Sales", False),
        KpiDefinition(CATEGORY_1, f"{settings.category_1.label} Sales", False),
        KpiDefinition(CATEGORY_2, f"{settings.category_2.label} Sales", False),
    )


@dataclass(frozen=True)
class KpiValues:
    """One value per KPI for a single window."""

    premium: float = 0.0
    sales: float = 0.0
    category_1: float = 0.0
    category_2: float = 0.0

    def get(self, key: str) -> float:
        return float(getattr(self, key))

    def as_dict(self) -> dict[str, float]:
        return {key: self.get(key) for key in KPI_KEYS}


def apply_web_filter(frame: pd.DataFrame, include_web: bool) -> pd.DataFrame:
    """Drop web-originated rows when the dashboard toggle is off."""
    if include_web or frame.empty:
        return frame
    return frame.loc[~frame[S.IS_WEB].astype(bool)]


def compute(frame: pd.DataFrame) -> KpiValues:
    """Aggregate an already-filtered frame into the four KPIs."""
    if frame.empty:
        return KpiValues()
    keys = frame[S.CATEGORY_KEY]
    return KpiValues(
        premium=float(frame[S.PREMIUM].sum()),
        sales=float(frame[S.SALES].sum()),
        category_1=float(frame.loc[keys == S.CATEGORY_1, S.SALES].sum()),
        category_2=float(frame.loc[keys == S.CATEGORY_2, S.SALES].sum()),
    )


def compute_window(frame: pd.DataFrame, start: date, end: date) -> KpiValues:
    return compute(S.slice_dates(frame, start, end))


def breakdown(
    frame: pd.DataFrame,
    group_column: str,
    settings: DataSettings,
) -> pd.DataFrame:
    """Pivot-table style breakout: one row per group, one column per KPI.

    Sorted by premium so the biggest contributors sit at the top, which is how
    an exec reads it.
    """
    definitions = kpi_definitions(settings)
    columns = [definition.label for definition in definitions]

    if frame.empty or group_column not in frame.columns:
        return pd.DataFrame(columns=[group_column, *columns, "% of Premium"])

    working = frame.copy()
    working["_cat1"] = working[S.SALES].where(
        working[S.CATEGORY_KEY] == S.CATEGORY_1, 0.0
    )
    working["_cat2"] = working[S.SALES].where(
        working[S.CATEGORY_KEY] == S.CATEGORY_2, 0.0
    )

    grouped = (
        working.groupby(group_column, dropna=False)
        .agg(
            premium=(S.PREMIUM, "sum"),
            sales=(S.SALES, "sum"),
            category_1=("_cat1", "sum"),
            category_2=("_cat2", "sum"),
        )
        .reset_index()
    )

    total_premium = float(grouped["premium"].sum())
    grouped["share"] = (
        grouped["premium"] / total_premium * 100.0 if total_premium else 0.0
    )
    grouped = grouped.sort_values(
        ["premium", "sales"], ascending=False, kind="stable"
    ).reset_index(drop=True)

    grouped = grouped.rename(
        columns={
            group_column: group_column,
            "premium": definitions[0].label,
            "sales": definitions[1].label,
            "category_1": definitions[2].label,
            "category_2": definitions[3].label,
            "share": "% of Premium",
        }
    )
    return grouped[[group_column, *columns, "% of Premium"]]


def daily_series(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """All four KPIs per calendar day across the window, gaps filled with zero.

    One row per calendar day whether or not anything was booked, which is what
    the moving-average layer needs -- a missing day is a real zero, not an
    absence.
    """
    index = pd.date_range(start=start, end=end, freq="D")
    empty = pd.DataFrame(
        {key: 0.0 for key in KPI_KEYS}, index=index
    ).rename_axis(S.DATE)

    if frame.empty:
        return empty
    window = S.slice_dates(frame, start, end)
    if window.empty:
        return empty

    working = window.copy()
    working[CATEGORY_1] = working[S.SALES].where(
        working[S.CATEGORY_KEY] == S.CATEGORY_1, 0.0
    )
    working[CATEGORY_2] = working[S.SALES].where(
        working[S.CATEGORY_KEY] == S.CATEGORY_2, 0.0
    )

    grouped = (
        working.groupby(working[S.DATE].dt.normalize())[list(KPI_KEYS)]
        .sum()
        .reindex(index, fill_value=0.0)
    )
    return grouped.rename_axis(S.DATE)
