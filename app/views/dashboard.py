"""The Executive Dashboard page.

Reading order, top to bottom:

1. controls -- refresh, the web-sales toggle, and when the data was last read;
2. the four KPIs for the selected period, with a period-over-period delta;
3. the projection for that period, when it is a month or a year;
4. every period at a glance, projections included;
5. the daily trend;
6. the agent breakout, collapsed until asked for.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.core import analytics, data_health, kpis, projections, targets
from app.core.calendar_rules import WorkingCalendar
from app.core.periods import MONTH, PERIOD_ORDER, build_periods
from app.data import repository
from app.data import schema as S
from app.settings import Settings
from app.ui import charts, components
from app.ui.components import Card
from app.ui.theme import (
    STATUS,
    delta_parts,
    format_compact,
    format_projection,
    format_value,
    md_escape,
)
from app.views import widgets

GROUP_COLUMNS = {
    "Agent": S.AGENT,
    "Category": S.CATEGORY,
    "Channel": S.CHANNEL,
}


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #


def _controls(settings: Settings, result) -> bool:
    """Refresh + web-sales toggle. Returns whether web sales are included."""
    left, right = st.columns([1, 2], vertical_alignment="center")

    with left:
        if st.button(
            "Refresh data",
            type="primary",
            help=repository.refresh_help(settings),
        ):
            repository.request_refresh()
            st.rerun()

    with right:
        include_web = st.checkbox(
            "Include web sales",
            value=True,
            key="include_web",
            help=(
                "Web sales are rows whose channel or agent matches "
                "data.web_sales in config.yaml. Unchecking removes them from "
                "every KPI, projection and breakdown on this page."
            ),
        )

    tz = settings.app.tzinfo()
    pills = [f"Last refreshed {repository.last_refresh_display(result, tz)}"]
    pills.append(
        f"{result.row_count:,} rows from {repository.source_description(settings, result)}"
    )
    if not include_web:
        pills.append("Web sales excluded")
    components.meta_strip(pills)
    return include_web


# --------------------------------------------------------------------------- #
# KPI cards
# --------------------------------------------------------------------------- #


def _kpi_cards(
    settings: Settings,
    frame: pd.DataFrame,
    period,
    projection_map: dict[str, projections.Projection],
) -> dict[str, targets.Attainment]:
    """The four KPI cards. Returns whatever targets were in force."""
    symbol = settings.app.currency_symbol
    current = kpis.compute_window(frame, period.start, period.end)
    prior = kpis.compute_window(frame, period.prior_start, period.prior_end)

    cards: list[Card] = []
    attainments: dict[str, targets.Attainment] = {}

    for definition in kpis.kpi_definitions(settings.data):
        value = current.get(definition.key)
        delta_text, delta_css = delta_parts(value, prior.get(definition.key))

        footnote = ""
        progress = None
        projection = projection_map.get(definition.key)

        attainment = targets.measure(
            settings.targets,
            definition.key,
            period,
            actual=value,
            projected=projection.projected if projection else None,
            basis=projection.basis if projection else None,
        )
        if attainment is not None:
            attainments[definition.key] = attainment
            # Against a target, the useful pair is how much is banked and where
            # the current pace lands -- not the raw projection on its own.
            goal = format_compact(
                attainment.target.value, definition.is_currency, symbol
            )
            projected_pct = attainment.projected_pct
            tail = (
                f" · pace {projected_pct:.0f}%" if projected_pct is not None else ""
            )
            footnote = f"{attainment.achieved_pct:.0f}% of {goal} target{tail}"
            progress = attainment.achieved_pct / 100.0
        elif projection is not None:
            projected = format_projection(
                projection.projected, definition.is_currency, symbol
            )
            footnote = f"Projected {projected} by {projection.basis.period_end:%b %d}"
            progress = projection.basis.completion

        cards.append(
            Card(
                label=definition.label,
                value=format_value(value, definition.is_currency, symbol),
                delta=delta_text,
                delta_class=delta_css,
                delta_note=period.comparison_label,
                footnote=footnote,
                progress=progress,
            )
        )
    components.kpi_grid(cards)
    return attainments


def _target_note(settings: Settings, attainments: dict[str, targets.Attainment]) -> None:
    """What the rest of the period needs -- the most actionable line on the page."""
    headline = attainments.get(kpis.PREMIUM)
    if headline is None:
        return

    symbol = settings.app.currency_symbol
    goal = format_value(headline.target.value, True, symbol)

    if headline.is_met:
        st.caption(
            md_escape(
                f"The {headline.target.label} of {goal} is already met — "
                f"{headline.achieved_pct:.0f}% booked."
            )
        )
        return

    required = headline.required_pace
    if required is None:
        st.caption(
            md_escape(
                f"{headline.achieved_pct:.0f}% of the {headline.target.label} "
                f"({goal}), with no working days left in the period."
            )
        )
        return

    current = headline.current_pace
    verdict = (
        "ahead of what the target needs"
        if headline.on_track
        else "short of what the target needs"
    )
    pace_note = (
        f" Current pace is {format_value(current, True, symbol)}/day, {verdict}."
        if current is not None
        else ""
    )
    st.caption(
        md_escape(
            f"To reach the {headline.target.label} of {goal}, the remaining "
            f"{headline.basis.remaining} working days need "
            f"{format_value(required, True, symbol)}/day.{pace_note}"
        )
    )


# --------------------------------------------------------------------------- #
# All-periods matrix
# --------------------------------------------------------------------------- #


def _matrix(
    settings: Settings,
    frame: pd.DataFrame,
    periods: dict,
    today: date,
    calendar: WorkingCalendar,
) -> pd.DataFrame:
    """Every period as a row, every KPI as a column, projections included."""
    definitions = kpis.kpi_definitions(settings.data)
    symbol = settings.app.currency_symbol
    rows: list[dict[str, str]] = []

    for key in PERIOD_ORDER:
        period = periods[key]
        values = kpis.compute_window(frame, period.start, period.end)
        rows.append(
            {
                "Period": period.label,
                **{
                    d.label: format_value(values.get(d.key), d.is_currency, symbol)
                    for d in definitions
                },
            }
        )

        if not settings.projection.enabled or key not in settings.projection.periods:
            continue
        basis = projections.build_basis(period, today, calendar, settings.projection)
        projected = projections.project(values, basis)
        if not projected:
            continue
        suffix = "month" if key == MONTH else "year"
        stem = period.label.split(" to")[0]
        rows.append(
            {
                "Period": f"{stem} projected ({suffix} end)",
                **{
                    d.label: format_projection(
                        projected[d.key].projected, d.is_currency, symbol
                    )
                    for d in definitions
                },
            }
        )

        # A target row sits directly under its projection, so the comparison is
        # a glance down the column rather than arithmetic.
        goals = {
            d.label: targets.resolve(settings.targets, d.key, period)
            for d in definitions
        }
        if any(goals.values()):
            rows.append(
                {
                    "Period": f"{stem} target",
                    **{
                        d.label: (
                            format_value(goals[d.label].value, d.is_currency, symbol)
                            if goals[d.label]
                            else "—"
                        )
                        for d in definitions
                    },
                }
            )

    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Breakdown
# --------------------------------------------------------------------------- #


def _breakdown(settings: Settings, frame: pd.DataFrame, period) -> None:
    definitions = kpis.kpi_definitions(settings.data)
    symbol = settings.app.currency_symbol

    group_label = widgets.choice(
        "Group by",
        list(GROUP_COLUMNS),
        key="breakdown_group",
        default="Agent",
        help="Rows of the pivot. Columns are always the four KPIs.",
    )
    column = GROUP_COLUMNS[group_label]

    window = S.slice_dates(frame, period.start, period.end)
    table = kpis.breakdown(window, column, settings.data)

    if table.empty:
        components.empty_state(
            "Nothing to break out",
            f"No rows fall inside {period.label.lower()} "
            f"({period.start:%b %d} - {period.end:%b %d}).",
        )
        return

    display = table.rename(columns={column: group_label})
    display[group_label] = display[group_label].replace("", "(blank)")

    # A totals row makes the pivot reconcile against the cards above it.
    totals = {group_label: "All"}
    for definition in definitions:
        totals[definition.label] = float(display[definition.label].sum())
    totals["% of Premium"] = 100.0 if totals[definitions[0].label] else 0.0
    display = pd.concat([display, pd.DataFrame([totals])], ignore_index=True)

    config = {
        group_label: st.column_config.TextColumn(group_label, width="medium"),
        definitions[0].label: st.column_config.NumberColumn(
            definitions[0].label, format=f"{symbol}%,.0f"
        ),
        "% of Premium": st.column_config.ProgressColumn(
            "% of Premium", format="%.1f%%", min_value=0.0, max_value=100.0
        ),
    }
    for definition in definitions[1:]:
        config[definition.label] = st.column_config.NumberColumn(
            definition.label, format="%,.0f"
        )

    widgets.dataframe(display, hide_index=True, column_config=config)

    st.download_button(
        "Download this breakdown (CSV)",
        data=table.to_csv(index=False).encode("utf-8"),
        file_name=(
            f"{group_label.lower()}_{period.key}_"
            f"{period.start:%Y%m%d}_{period.end:%Y%m%d}.csv"
        ),
        mime="text/csv",
    )


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def _alert_banner(
    settings: Settings, result, frame: pd.DataFrame, today: date
) -> None:
    """One line saying whether anything needs attention.

    Data health is checked first and reported on its own. A stalled feed makes
    every moving average look like a collapse, so leading with "premium is down
    40%" when the real answer is "the export died on Tuesday" would send people
    after the wrong problem.

    Only the book-wide performance rules are evaluated here -- the per-agent
    sweep belongs to the Analytics page and costs more than a dashboard rerun
    should.
    """
    if not settings.analytics.enabled:
        return

    calendar = WorkingCalendar.from_settings(settings.calendar)
    health = data_health.evaluate(
        result, frame, today, calendar, settings.analytics.data_health, settings.data
    )
    health_problems = [a for a in health if a.severity.is_alert]

    if data_health.is_compromised(health):
        worst = health_problems[0]
        components.alert_card(
            "Data problem — figures below may be wrong",
            worst.message,
            worst.severity.label,
            STATUS.get(worst.severity.value, STATUS["no_data"]),
            scope="Data",
        )
        return

    context = analytics.build_context(
        frame, settings.analytics, settings.data, calendar, today
    )
    alerts = [a for a in analytics.evaluate(context) if a.severity.is_alert]

    for alert in health_problems:
        components.alert_card(
            alert.rule,
            alert.message,
            alert.severity.label,
            STATUS.get(alert.severity.value, STATUS["no_data"]),
            scope="Data",
        )

    if not alerts:
        return

    worst = alerts[0].severity
    critical = sum(1 for a in alerts if a.severity is analytics.Severity.CRITICAL)
    warning = len(alerts) - critical
    parts = []
    if critical:
        parts.append(f"{critical} critical")
    if warning:
        parts.append(f"{warning} warning")

    # Plain text only: the card escapes its content, so markdown would show
    # up as literal asterisks.
    tail = (
        f" (+{len(alerts) - 1} more — see the Analytics page.)"
        if len(alerts) > 1
        else " See the Analytics page for the detail."
    )
    components.alert_card(
        f"{' and '.join(parts)} on moving averages",
        alerts[0].message + tail,
        worst.label,
        STATUS.get(worst.value, STATUS["no_data"]),
    )


def render(settings: Settings) -> None:
    result = repository.get_dataset(settings)
    today = settings.app.today()
    calendar = WorkingCalendar.from_settings(settings.calendar)
    periods = build_periods(today)

    components.masthead(settings.app.title, settings.app.organization)
    components.subhead(
        f"{today:%A, %B %d, %Y} · all figures in {settings.app.timezone}"
    )

    include_web = _controls(settings, result)

    if result.warnings:
        with st.expander(f"Data notices ({len(result.warnings)})", expanded=False):
            for warning in result.warnings[:25]:
                st.write(f"- {warning}")
            if len(result.warnings) > 25:
                st.caption(f"…and {len(result.warnings) - 25} more. See Diagnostics.")

    if result.frame.empty:
        components.empty_state(
            "No data loaded",
            "No workbook rows were read. Open **Diagnostics** to see which files "
            "were found and how their columns resolved, then adjust "
            "`data.sources` in `config/config.yaml`.",
        )
        return

    frame = kpis.apply_web_filter(result.frame, include_web)

    _alert_banner(settings, result, frame, today)

    period_key = widgets.choice(
        "Reporting period",
        list(PERIOD_ORDER),
        key="period",
        default=MONTH,
        format_func=lambda key: periods[key].label,
    )
    period = periods[period_key]

    projection_map: dict[str, projections.Projection] = {}
    if settings.projection.enabled and period_key in settings.projection.periods:
        basis = projections.build_basis(period, today, calendar, settings.projection)
        projection_map = projections.project(
            kpis.compute_window(frame, period.start, period.end), basis
        )

    window_note = (
        f"{period.start:%b %d}"
        if period.start == period.end
        else f"{period.start:%b %d} – {period.end:%b %d, %Y}"
    )
    components.section(period.label, window_note)
    attainments = _kpi_cards(settings, frame, period, projection_map)

    if projection_map:
        basis = next(iter(projection_map.values())).basis
        closed = "including today" if basis.counts_today else "completed days only"
        st.caption(
            f"Projection basis: {basis.elapsed} of {basis.total} working days "
            f"elapsed ({closed}), {basis.remaining} remaining through "
            f"{basis.period_end:%b %d}. Sundays and observed holidays are excluded."
        )

    _target_note(settings, attainments)

    components.section("All periods", "actuals and full-period projections")
    widgets.dataframe(
        _matrix(settings, frame, periods, today, calendar), hide_index=True
    )

    if period.days > 1:
        components.section("Daily trend", period.label.lower())
        charts.render(
            charts.daily_trend(
                kpis.daily_series(frame, period.start, period.end),
                currency_symbol=settings.app.currency_symbol,
                title=f"Premium by day · {period.label}",
            )
        )

    components.section("Breakdown", "pivot the selected period")
    with st.expander("Expand metrics by agent", expanded=False):
        _breakdown(settings, frame, period)
