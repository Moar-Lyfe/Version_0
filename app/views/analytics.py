"""The Analytics page: what is quietly getting worse.

The Dashboard reports the current period. This page watches the *shape* of each
KPI over 15, 30, 45 and 90-day trailing windows and says plainly when one is
slipping -- against a number you set, or against the metric's own history when
nobody has set one.

Reading order: what is wrong now, then the moving averages that explain it, then
the agents inside the number, then the rules doing the judging.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.core import analytics, data_health, kpis, targets
from app.core.analytics import OVERALL, Alert, Severity
from app.core.calendar_rules import WorkingCalendar
from app.core.periods import MONTH, YEAR, build_periods
from app.data import repository
from app.settings import Settings
from app.ui import charts, components
from app.ui.theme import STATUS, format_currency, md_escape
from app.views import widgets

# How much history the explorer chart shows by default.
CHART_DAYS = 240


def _severity_colour(severity: Severity) -> str:
    return STATUS.get(severity.value, STATUS["no_data"])


def _rate(value: float | None, is_currency: bool, symbol: str) -> str:
    """A per-working-day rate, which is what every average on this page is."""
    if value is None:
        return "—"
    if is_currency:
        return f"{format_currency(value, symbol)}/day"
    return f"{value:,.2f}/day"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _counts_phrase(alerts: list[Alert]) -> str:
    counts = analytics.summarise(alerts)
    parts = [
        f"{counts[severity]} {severity.label.lower()}"
        for severity in (Severity.CRITICAL, Severity.WARNING, Severity.NO_DATA, Severity.OK)
        if counts[severity]
    ]
    return ", ".join(parts) or "nothing to report"


def _data_health(health: list[Alert]) -> None:
    """Data-quality verdicts, above everything they would otherwise distort."""
    problems = [alert for alert in health if alert.severity.is_alert]
    if not problems:
        return

    components.section("Data health", "checks on the feed, not the numbers")
    for alert in problems:
        components.alert_card(
            alert.rule,
            alert.message,
            alert.severity.label,
            _severity_colour(alert.severity),
            scope="Data",
        )
    if data_health.is_compromised(health):
        st.warning(
            "**Read the performance alerts below with that in mind.** A feed that "
            "has stopped updating turns recent days into zeros, which every "
            "moving average reads as a collapse in production. Fix the data "
            "first, then judge the numbers."
        )


def _summary(
    alerts: list[Alert],
    agent_alerts: list[Alert],
    health: list[Alert],
    settings: Settings,
) -> None:
    """Book-wide, per-agent and data-health counts, kept apart.

    Rolled together they read as a contradiction: "5 critical" beside a panel
    saying every rule is on track, because the criticals were all agent-level.
    """
    basis = (
        "per working day"
        if settings.analytics.basis == analytics.WORKING_DAYS
        else "per calendar day"
    )
    pills = []
    problems = [a for a in health if a.severity.is_alert]
    if problems:
        pills.append(f"Data: {_counts_phrase(problems)}")
    elif health:
        pills.append("Data: healthy")
    pills.append(f"Book-wide: {_counts_phrase(alerts)}")
    if settings.analytics.monitor_agents:
        pills.append(
            f"Agents: {_counts_phrase(agent_alerts)}"
            if agent_alerts
            else "Agents: none slipping"
        )
    pills.append(f"Averages {basis}")
    pills.append(f"Windows {', '.join(f'{w}d' for w in settings.analytics.windows)}")
    components.meta_strip(pills)


def _alert_list(alerts: list[Alert], show_ok: bool) -> None:
    shown = [a for a in alerts if show_ok or a.severity is not Severity.OK]
    if not shown:
        components.alert_card(
            "Every rule is on track",
            "No moving average is below its threshold or its baseline.",
            Severity.OK.label,
            STATUS["ok"],
        )
        return
    for alert in shown:
        components.alert_card(
            alert.rule,
            alert.message,
            alert.severity.label,
            _severity_colour(alert.severity),
            scope="" if alert.scope == OVERALL else alert.scope,
        )


def _rule_table(alerts: list[Alert], settings: Settings) -> pd.DataFrame:
    symbol = settings.app.currency_symbol
    rows = []
    for alert in alerts:
        rows.append(
            {
                "Status": alert.severity.label,
                "Rule": alert.rule,
                "Metric": alert.metric_label,
                "Window": f"{alert.window}d",
                "Kind": alert.kind,
                "Current": _rate(alert.current, alert.is_currency, symbol),
                "Compared with": alert.reference_label,
                "Reference": _rate(alert.reference, alert.is_currency, symbol),
                "Shortfall": (
                    "—" if alert.shortfall_pct is None else f"{alert.shortfall_pct:+.1f}%"
                ),
            }
        )
    return pd.DataFrame(rows)


def _explorer(
    settings: Settings,
    context: analytics.AnalyticsContext,
    alerts: list[Alert],
) -> None:
    definitions = kpis.kpi_definitions(settings.data)
    labels = {d.label: d for d in definitions}

    chosen = widgets.choice(
        "Metric",
        list(labels),
        key="analytics_metric",
        default=definitions[0].label,
    )
    definition = labels[chosen]

    all_windows = list(settings.analytics.windows)
    selected = st.multiselect(
        "Windows",
        options=all_windows,
        default=all_windows,
        format_func=lambda w: f"{w}-day",
        key="analytics_windows",
        help="Trailing averages. Colours follow the window, so hiding one does "
        "not recolour the rest.",
    )
    if not selected:
        selected = all_windows

    averages = context.averages(definition.key)
    latest = averages.tail(1)

    palette = charts.window_palette(len(all_windows))
    chips = []
    for index, window in enumerate(all_windows):
        if window not in selected or window not in latest.columns:
            continue
        value = latest[window].iloc[0] if not latest.empty else None
        chips.append(
            (
                f"{window}-day",
                _rate(
                    None if value is None or pd.isna(value) else float(value),
                    definition.is_currency,
                    settings.app.currency_symbol,
                ),
                palette[index],
            )
        )
    components.series_chips(chips)

    # A threshold rule on this metric gets drawn as a reference line, so the
    # alert and the picture explaining it are the same object.
    reference = reference_label = None
    for rule in analytics.resolved_rules(settings.analytics):
        if rule.metric == definition.key and rule.is_threshold:
            reference = rule.critical if rule.critical is not None else rule.warn
            reference_label = f"{rule.name} ({'floor' if rule.operator == 'min' else 'ceiling'})"
            break

    # Failing that, a period target implies a daily rate, which is the more
    # meaningful line to hold a moving average against.
    if reference is None and settings.targets.has_any():
        calendar = WorkingCalendar.from_settings(settings.calendar)
        periods = build_periods(settings.app.today())
        working = {
            key: calendar.working_days_between(
                periods[key].full_start, periods[key].full_end
            )
            for key in (MONTH, YEAR)
            if periods[key].full_start and periods[key].full_end
        }
        pace = targets.daily_pace_target(
            settings.targets, definition.key, periods, working
        )
        if pace is not None:
            reference, reference_label = pace[0], pace[1]

    charts.render(
        charts.moving_average_chart(
            averages.tail(CHART_DAYS),
            windows=selected,
            all_windows=all_windows,
            currency_symbol=settings.app.currency_symbol,
            is_currency=definition.is_currency,
            title=f"{definition.label} · trailing averages",
            reference=reference,
            reference_label=reference_label or "",
        )
    )

    relevant = [a for a in alerts if a.metric == definition.key]
    if relevant:
        st.caption(
            md_escape(
                "Rules watching this metric: "
                + " · ".join(f"{a.rule} — {a.severity.label}" for a in relevant)
            )
        )

    with st.expander("Show the averages as a table", expanded=False):
        table = averages[[w for w in selected if w in averages.columns]].dropna(how="all")
        table = table.tail(60).iloc[::-1].round(2)
        table.columns = [f"{w}-day" for w in table.columns]
        table.index = table.index.strftime("%Y-%m-%d")
        widgets.dataframe(table.rename_axis("Date").reset_index(), hide_index=True)
        st.download_button(
            "Download the full series (CSV)",
            data=averages.round(4).to_csv().encode("utf-8"),
            file_name=f"moving_averages_{definition.key}.csv",
            mime="text/csv",
        )


def _agents(agent_alerts: list[Alert], settings: Settings) -> None:
    if not settings.analytics.monitor_agents:
        st.caption(
            "Per-agent monitoring is off. Set `analytics.monitor_agents: true` "
            "in config.yaml to switch it on."
        )
        return
    if not agent_alerts:
        components.alert_card(
            "No agent is slipping",
            "Every agent above the minimum volume is holding their own baseline.",
            Severity.OK.label,
            STATUS["ok"],
        )
        return

    symbol = settings.app.currency_symbol
    rows = [
        {
            "Status": alert.severity.label,
            "Agent": alert.scope,
            "Metric": alert.metric_label,
            "Window": f"{alert.window}d",
            "Current": _rate(alert.current, alert.is_currency, symbol),
            "Baseline": _rate(alert.reference, alert.is_currency, symbol),
            "Shortfall": f"{alert.shortfall_pct:+.1f}%",
        }
        for alert in agent_alerts
    ]
    widgets.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption(
        f"Only agents with at least {settings.analytics.agent_min_sales:g} sales in "
        "the baseline window are judged, and only those slipping are listed. "
        "Thresholds are not applied per agent — a floor written for the whole "
        "book says nothing about one person."
    )


def _rules_reference(settings: Settings) -> None:
    rules = analytics.resolved_rules(settings.analytics)
    using_preset = not [r for r in settings.analytics.rules if r.enabled]

    if using_preset:
        st.caption(
            f"No rules configured, so the **{settings.analytics.preset}** preset is "
            "running. It needs no targets: each metric is judged against its own "
            "90-day baseline. Add `analytics.rules` in `config/config.yaml` to "
            "define your own."
        )
    else:
        st.caption(
            f"{len(rules)} rule(s) from `config/config.yaml`. "
            f"Presets available: {', '.join(sorted(analytics.PRESETS))}."
        )

    rows = []
    for rule in rules:
        if rule.is_threshold:
            judged = (
                f"{'floor' if rule.operator == 'min' else 'ceiling'} "
                f"warn {rule.warn if rule.warn is not None else '—'} / "
                f"critical {rule.critical if rule.critical is not None else '—'}"
            )
        elif rule.kind == "relative":
            judged = f"vs its own {rule.baseline}-day baseline"
        else:
            judged = f"vs itself {rule.lookback_days} days ago"
        rows.append(
            {
                "Rule": rule.name,
                "Metric": rule.metric,
                "Kind": rule.kind,
                "Window": f"{rule.window}d",
                "Judged against": judged,
                "Warn": (
                    f"{rule.warn_pct:g}%" if rule.warn_pct is not None else
                    (f"{rule.warn:g}" if rule.warn is not None else "—")
                ),
                "Critical": (
                    f"{rule.critical_pct:g}%" if rule.critical_pct is not None else
                    (f"{rule.critical:g}" if rule.critical is not None else "—")
                ),
            }
        )
    widgets.dataframe(pd.DataFrame(rows), hide_index=True)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def load_alerts(settings: Settings, frame: pd.DataFrame, today, result=None):
    """Build the context and evaluate everything. Shared with the Dashboard."""
    calendar = WorkingCalendar.from_settings(settings.calendar)
    context = analytics.build_context(
        frame, settings.analytics, settings.data, calendar, today
    )
    alerts = analytics.evaluate(context)
    agent_alerts = analytics.evaluate_agents(context, frame, calendar)
    health = (
        data_health.evaluate(
            result,
            frame,
            today,
            calendar,
            settings.analytics.data_health,
            settings.data,
        )
        if result is not None
        else []
    )
    return context, alerts, agent_alerts, health


def render(settings: Settings) -> None:
    components.masthead("Analytics", settings.app.organization)
    components.subhead(
        "Moving-average monitoring — what is slipping, and against what."
    )

    if not settings.analytics.enabled:
        components.empty_state(
            "Analytics disabled",
            "Set `analytics.enabled: true` in `config/config.yaml` to switch this "
            "page on.",
        )
        return

    result = repository.get_dataset(settings)
    today = settings.app.today()

    include_web = st.checkbox(
        "Include web sales",
        value=st.session_state.get("include_web", True),
        key="analytics_include_web",
        help="Matches the Dashboard toggle. Excluding web sales re-evaluates "
        "every rule on the remaining rows.",
    )

    if result.frame.empty:
        components.empty_state(
            "No data loaded",
            "Nothing to analyse. Open **Diagnostics** to see which files were "
            "found and how their columns resolved.",
        )
        return

    frame = kpis.apply_web_filter(result.frame, include_web)
    context, alerts, agent_alerts, health = load_alerts(
        settings, frame, today, result
    )

    _summary(alerts, agent_alerts, health, settings)
    _data_health(health)

    if context.history_days < settings.analytics.min_history_days:
        st.warning(
            f"Only {context.history_days} day(s) of history are loaded. Moving "
            f"averages need at least {settings.analytics.min_history_days} days, "
            "and a 90-day window needs 90."
        )

    components.section("Alerts", f"{len(analytics.resolved_rules(settings.analytics))} rule(s)")
    show_ok = st.toggle(
        "Show rules that are on track",
        value=False,
        key="analytics_show_ok",
        help="Off by default so the page shows only what needs attention.",
    )
    _alert_list(alerts, show_ok)

    with st.expander("Every rule, as a table", expanded=False):
        widgets.dataframe(_rule_table(alerts, settings), hide_index=True)

    components.section("Moving averages", "trailing, per working day")
    _explorer(settings, context, alerts)

    components.section("Agents to watch", "judged against their own baseline")
    _agents(agent_alerts, settings)

    components.section("Rules", "what is doing the judging")
    _rules_reference(settings)
