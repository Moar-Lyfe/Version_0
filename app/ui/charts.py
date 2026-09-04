"""The one chart on the dashboard: daily production for the selected period.

Deliberately a single series -- premium per day -- so it needs no legend and no
categorical palette. Bars while the window is short enough for one mark per day
to stay legible, a line once the year view makes bars hairline-thin.

Colours come from the reference data-viz palette (categorical slot 1), stepped
separately for the light and dark surfaces rather than flipped automatically.
"""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from app.data import schema as S
from app.ui import theme

# Slot 1 of the validated categorical palette, stepped per surface. The light
# step is the app's accent, so the chart and the primary buttons agree.
SERIES_LIGHT = theme.ACCENT
SERIES_DARK = "#3987e5"

_GRID_LIGHT = "#e6e5e1"
_GRID_DARK = "#3a3a38"
_TEXT_LIGHT = "#52514e"
_TEXT_DARK = "#c3c2b7"

# Past this many points a bar per day is thinner than the gap between them.
BAR_LIMIT = 45
# Share of each day's band the bar fills; the remainder is the surface gap.
BAR_BAND_FRACTION = 0.55


def _is_dark() -> bool:
    try:
        theme = getattr(st.context, "theme", None)
        if theme is not None and getattr(theme, "type", None):
            return str(theme.type).lower() == "dark"
    except Exception:  # noqa: BLE001 - theme introspection is best-effort
        pass
    return str(st.get_option("theme.base") or "light").lower() == "dark"


def daily_trend(
    series: pd.DataFrame,
    *,
    currency_symbol: str = "$",
    title: str = "Premium by day",
) -> alt.Chart | None:
    """Build the trend chart, or ``None`` when there is nothing worth drawing."""
    if series is None or series.empty or len(series) < 2:
        return None

    dark = _is_dark()
    color = SERIES_DARK if dark else SERIES_LIGHT
    grid = _GRID_DARK if dark else _GRID_LIGHT
    text = _TEXT_DARK if dark else _TEXT_LIGHT

    frame = series.reset_index().rename(columns={"index": S.DATE})
    frame = frame[[S.DATE, S.PREMIUM, S.SALES]]

    tooltip = [
        alt.Tooltip(f"{S.DATE}:T", title="Date", format="%a %b %d, %Y"),
        alt.Tooltip(
            f"{S.PREMIUM}:Q", title="Premium", format=f"{currency_symbol},.0f"
        ),
        alt.Tooltip(f"{S.SALES}:Q", title="Sales", format=",.0f"),
    ]

    axis_kwargs = dict(
        grid=False,
        labelColor=text,
        tickColor=grid,
        domainColor=grid,
        labelFontSize=10,
        labelAngle=0,
        format="%b %d",
    )
    y = alt.Y(
        f"{S.PREMIUM}:Q",
        title=None,
        axis=alt.Axis(
            grid=True,
            gridColor=grid,
            gridOpacity=0.7,
            domain=False,
            ticks=False,
            labelColor=text,
            labelFontSize=10,
            format="~s",
        ),
    )

    if len(frame) <= BAR_LIMIT:
        # One band per day. A continuous temporal scale would spread a handful
        # of days into hairline bars and repeat the same date label across every
        # tick, so short windows get a discrete axis instead.
        chart = (
            alt.Chart(frame)
            .mark_bar(
                color=color,
                cornerRadiusTopLeft=4,
                cornerRadiusTopRight=4,
                # A fraction of the band rather than a pixel width: bars stay
                # thin on a wide desktop and never collide on a phone.
                width=alt.RelativeBandSize(BAR_BAND_FRACTION),
            )
            .encode(
                x=alt.X(
                    f"yearmonthdate({S.DATE}):O",
                    title=None,
                    axis=alt.Axis(labelOverlap="greedy", **axis_kwargs),
                ),
                y=y,
                tooltip=tooltip,
            )
        )
    else:
        chart = (
            alt.Chart(frame)
            .mark_area(
                color=color,
                opacity=0.16,
                line={"color": color, "strokeWidth": 2},
                interpolate="monotone",
            )
            .encode(
                x=alt.X(
                    f"{S.DATE}:T",
                    title=None,
                    axis=alt.Axis(labelOverlap=True, **axis_kwargs),
                ),
                y=y,
                tooltip=tooltip,
            )
        )

    return (
        chart.properties(
            height=170,
            title=alt.TitleParams(
                title, fontSize=12, color=text, anchor="start", dy=-4
            ),
        )
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
    )


def render(chart: alt.Chart | None) -> None:
    """Draw the chart, tolerating Streamlit's shifting width API."""
    if chart is None:
        return
    try:
        st.altair_chart(chart, width="stretch")
    except TypeError:
        st.altair_chart(chart, use_container_width=True)
