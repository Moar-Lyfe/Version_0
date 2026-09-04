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

# Categorical slots 1-4, stepped for each surface. Assigned in a fixed order and
# never cycled, so a window keeps its colour however many are on screen.
CATEGORICAL_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
CATEGORICAL_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500")

_SURFACE_LIGHT = "#ffffff"
_SURFACE_DARK = "#1a1a19"
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


def window_palette(count: int, dark: bool | None = None) -> list[str]:
    """Colours for ``count`` moving-average windows, in fixed slot order.

    Assignment follows the window, not its rank in the current selection: hiding
    the 30-day line must not repaint the 45-day one.
    """
    if dark is None:
        dark = _is_dark()
    ramp = CATEGORICAL_DARK if dark else CATEGORICAL_LIGHT
    # Past four windows, wrapping would make two series share a hue. Fold the
    # extras onto the last slot rather than inventing colours; the caller is
    # expected to keep the on-screen set small.
    return [ramp[min(i, len(ramp) - 1)] for i in range(count)]


def moving_average_chart(
    averages: pd.DataFrame,
    *,
    windows: list[int],
    all_windows: list[int],
    currency_symbol: str = "$",
    is_currency: bool = True,
    title: str = "Moving averages",
    reference: float | None = None,
    reference_label: str = "",
) -> alt.Chart | None:
    """Trailing moving averages of one metric, one line per window.

    A shared crosshair reads every window at the hovered date, so the question
    the chart exists to answer -- "is the short window pulling away from the
    long one?" -- is answered by pointing at a day.
    """
    if averages is None or averages.empty or not windows:
        return None

    columns = [w for w in windows if w in averages.columns]
    if not columns:
        return None

    frame = averages[columns].dropna(how="all")
    if len(frame) < 2:
        return None

    dark = _is_dark()
    grid = _GRID_DARK if dark else _GRID_LIGHT
    text = _TEXT_DARK if dark else _TEXT_LIGHT

    palette = window_palette(len(all_windows), dark)
    colour_of = {w: palette[i] for i, w in enumerate(all_windows)}
    labels = {w: f"{w}-day" for w in all_windows}

    long = (
        frame.reset_index()
        .melt(id_vars=[S.DATE], var_name="window", value_name="value")
        .dropna(subset=["value"])
    )
    long["series"] = long["window"].map(labels)

    domain = [labels[w] for w in all_windows if w in columns]
    scheme = [colour_of[w] for w in all_windows if w in columns]
    value_format = f"{currency_symbol},.0f" if is_currency else ",.2f"

    x = alt.X(
        f"{S.DATE}:T",
        title=None,
        axis=alt.Axis(
            grid=False,
            labelColor=text,
            tickColor=grid,
            domainColor=grid,
            labelFontSize=10,
            labelAngle=0,
            format="%b %d",
            labelOverlap=True,
        ),
    )
    y = alt.Y(
        "value:Q",
        title=None,
        scale=alt.Scale(zero=False, nice=True),
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
    colour = alt.Color(
        "series:N",
        title=None,
        scale=alt.Scale(domain=domain, range=scheme),
        legend=alt.Legend(
            orient="top",
            direction="horizontal",
            labelColor=text,
            labelFontSize=10,
            symbolStrokeWidth=3,
            symbolType="stroke",
        ),
    )

    base = alt.Chart(long)
    lines = base.mark_line(strokeWidth=2, interpolate="monotone").encode(
        x=x, y=y, color=colour
    )

    hover = alt.selection_point(
        fields=[S.DATE], nearest=True, on="pointermove", empty=False, clear="pointerout"
    )
    # A 2px surface ring keeps the highlighted markers legible where lines cross.
    markers = base.mark_point(
        size=60, filled=True, stroke=_SURFACE_DARK if dark else _SURFACE_LIGHT,
        strokeWidth=2,
    ).encode(
        x=x,
        y=y,
        color=colour,
        opacity=alt.condition(hover, alt.value(1.0), alt.value(0.0)),
    )
    crosshair = (
        base.transform_pivot("series", value="value", groupby=[S.DATE])
        .mark_rule(color=grid, strokeWidth=1)
        .encode(
            x=x,
            opacity=alt.condition(hover, alt.value(0.85), alt.value(0.0)),
            tooltip=[alt.Tooltip(f"{S.DATE}:T", title="Date", format="%b %d, %Y")]
            + [
                alt.Tooltip(f"{name}:Q", title=name, format=value_format)
                for name in domain
            ],
        )
        .add_params(hover)
    )

    layers = [lines, markers, crosshair]

    if reference is not None:
        marker = (
            alt.Chart(pd.DataFrame({"value": [reference]}))
            .mark_rule(color=theme.STATUS["critical"], strokeDash=[5, 4], strokeWidth=1.5)
            .encode(y=alt.Y("value:Q"))
        )
        layers.insert(0, marker)
        if reference_label:
            layers.append(
                alt.Chart(pd.DataFrame({"value": [reference], "label": [reference_label]}))
                .mark_text(
                    align="left", baseline="bottom", dx=4, dy=-3, fontSize=10,
                    color=theme.STATUS["critical"],
                )
                .encode(y=alt.Y("value:Q"), text="label:N")
            )

    return (
        alt.layer(*layers)
        .properties(
            height=260,
            title=alt.TitleParams(
                title, fontSize=12, color=text, anchor="start", dy=-4
            ),
        )
        .configure_view(strokeWidth=0)
        .configure(background="transparent")
        .configure_legend(labelColor=text, titleColor=text)
    )
