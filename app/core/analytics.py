"""Moving-average monitoring and alerting.

The dashboard answers "how are we doing?". This layer answers "is anything
quietly getting worse?" -- by tracking each KPI as a trailing moving average
over several windows and raising an alert when one of them slips.

**Averages are per working day.** A 15-day window holding three Sundays is not
penalised against one holding two, and a window containing a holiday is not read
as a slowdown. That is the same working calendar the projections use, so the two
features can never disagree about what a day is worth.

**Alerts come in three kinds**, covering both ways a metric can be judged:

``threshold``
    *Defined.* You state the number the average must hold. Use it where the
    business has a real floor -- "at least two Category 1 sales a day".

``relative``
    *Undefined.* No number needed. The short window is judged against the
    metric's own longer baseline, so the rule calibrates itself, survives
    growth, and works on a metric nobody has set a target for.

``trend``
    The same window against where it stood N days ago -- a slide that a stable
    long baseline would mask.

Every rule reads its numbers out of the same moving-average frame the charts
draw, so an alert and the line that explains it can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

import pandas as pd

from app.core import kpis
from app.core.calendar_rules import WorkingCalendar
from app.data import schema as S
from app.settings import AlertRuleSettings, AnalyticsSettings, DataSettings

WORKING_DAYS = "working_days"
CALENDAR_DAYS = "calendar_days"

OVERALL = "Overall"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    NO_DATA = "no_data"
    OK = "ok"

    @property
    def rank(self) -> int:
        """Sort order: worst first, so a table reads top-down by urgency."""
        return {"critical": 0, "warning": 1, "no_data": 2, "ok": 3}[self.value]

    @property
    def label(self) -> str:
        return {
            "critical": "Critical",
            "warning": "Warning",
            "no_data": "Not enough data",
            "ok": "On track",
        }[self.value]

    @property
    def is_alert(self) -> bool:
        return self in (Severity.CRITICAL, Severity.WARNING)


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
#
# A preset is what runs when nobody has written any rules: every KPI watched
# against its own 90-day baseline, which needs no targets and no maintenance.

def _relative(name, metric, window, warn, critical, baseline=90):
    return AlertRuleSettings(
        name=name,
        metric=metric,
        kind="relative",
        window=window,
        baseline=baseline,
        warn_pct=warn,
        critical_pct=critical,
    )


PRESETS: dict[str, tuple[AlertRuleSettings, ...]] = {
    # Catches a sustained slide without firing on an ordinary quiet week.
    "standard": (
        _relative("Premium vs 90-day baseline", "premium", 15, 10.0, 20.0),
        _relative("Sales vs 90-day baseline", "sales", 15, 10.0, 20.0),
        _relative("Category 1 vs 90-day baseline", "category_1", 30, 12.5, 25.0),
        _relative("Category 2 vs 90-day baseline", "category_2", 30, 12.5, 25.0),
    ),
    # Tighter tolerances and a second, shorter window on the headline numbers.
    "sensitive": (
        _relative("Premium vs 90-day baseline", "premium", 15, 5.0, 12.5),
        _relative("Premium 30-day drift", "premium", 30, 5.0, 10.0),
        _relative("Sales vs 90-day baseline", "sales", 15, 5.0, 12.5),
        _relative("Sales 30-day drift", "sales", 30, 5.0, 10.0),
        _relative("Category 1 vs 90-day baseline", "category_1", 15, 10.0, 20.0),
        _relative("Category 2 vs 90-day baseline", "category_2", 15, 10.0, 20.0),
        AlertRuleSettings(
            name="Premium 45-day trend",
            metric="premium",
            kind="trend",
            window=45,
            lookback_days=45,
            warn_pct=7.5,
            critical_pct=15.0,
        ),
    ),
    # Premium only, for a screen that should stay very quiet.
    "minimal": (_relative("Premium vs 90-day baseline", "premium", 30, 12.5, 25.0),),
    "none": (),
}


def resolved_rules(settings: AnalyticsSettings) -> tuple[AlertRuleSettings, ...]:
    """Configured rules if there are any, otherwise the named preset."""
    explicit = tuple(rule for rule in settings.rules if rule.enabled)
    if explicit:
        return explicit
    return PRESETS.get(settings.preset, PRESETS["standard"])


def required_windows(settings: AnalyticsSettings) -> tuple[int, ...]:
    """Every window the page or the rules need, so each is computed once."""
    windows = set(settings.windows)
    for rule in resolved_rules(settings):
        windows.add(rule.window)
        if rule.kind == "relative":
            windows.add(rule.baseline)
    return tuple(sorted(w for w in windows if w > 0))


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #


def day_weights(
    index: pd.DatetimeIndex, calendar: WorkingCalendar, basis: str = WORKING_DAYS
) -> pd.Series:
    """How much each date counts toward its window's divisor.

    One per working day under the default basis, one per calendar day otherwise.
    Computed once per index and shared by every metric and every agent -- the
    calendar lookup is the expensive part of this layer.
    """
    if basis == CALENDAR_DAYS:
        return pd.Series(1.0, index=index)
    return pd.Series(
        [1.0 if calendar.is_working_day(ts.date()) else 0.0 for ts in index],
        index=index,
    )


def moving_averages(
    daily: pd.DataFrame,
    metric: str,
    windows: tuple[int, ...],
    weights: pd.Series,
    valid_from: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Trailing per-day averages of ``metric``, one column per window.

    Each point is the window's total divided by the number of *countable* days
    it contains, and is ``NaN`` unless the whole window is answerable.

    Two things can make it unanswerable, and both must be caught. The obvious
    one is too few rows, which ``min_periods`` handles. The other is subtler and
    matters more: the daily series is padded with zeros back before the first
    day of real data, so a 90-day window over 40 days of history would happily
    average across 50 days of padding and report a confident, badly wrong
    baseline. ``valid_from`` is the first day the data actually covers, and any
    window reaching back past it is blanked -- "not enough data" rather than a
    number nobody should trust.
    """
    index = daily.index
    if metric not in daily.columns or len(index) == 0:
        return pd.DataFrame(index=index, columns=list(windows), dtype="float64")

    values = daily[metric].astype("float64")
    out: dict[int, pd.Series] = {}
    for window in windows:
        total = values.rolling(window, min_periods=window).sum()
        divisor = weights.rolling(window, min_periods=window).sum()
        series = total / divisor.where(divisor > 0)
        if valid_from is not None:
            window_start = index - pd.Timedelta(days=window - 1)
            series = series.where(window_start >= valid_from)
        out[window] = series
    return pd.DataFrame(out, index=index)


@dataclass
class AnalyticsContext:
    """Everything the rules need, with the averages computed once per metric."""

    daily: pd.DataFrame
    calendar: WorkingCalendar
    settings: AnalyticsSettings
    data_settings: DataSettings
    end: date
    history_days: int
    # First day the data actually covers; everything before it in `daily` is
    # zero padding and must never be averaged over.
    history_start: date | None = None
    weights: pd.Series | None = None
    _cache: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.weights is None:
            self.weights = day_weights(
                self.daily.index, self.calendar, self.settings.basis
            )

    @property
    def windows(self) -> tuple[int, ...]:
        return required_windows(self.settings)

    def averages(self, metric: str) -> pd.DataFrame:
        if metric not in self._cache:
            self._cache[metric] = moving_averages(
                self.daily,
                metric,
                self.windows,
                self.weights,
                valid_from=(
                    pd.Timestamp(self.history_start) if self.history_start else None
                ),
            )
        return self._cache[metric]

    def latest(self, metric: str, window: int) -> float | None:
        frame = self.averages(metric)
        if window not in frame.columns or frame.empty:
            return None
        value = frame[window].iloc[-1]
        return None if pd.isna(value) else float(value)

    def at_offset(self, metric: str, window: int, days_ago: int) -> float | None:
        """The same moving average as it stood ``days_ago`` days earlier."""
        frame = self.averages(metric)
        if window not in frame.columns or len(frame) <= days_ago:
            return None
        value = frame[window].iloc[-1 - days_ago]
        return None if pd.isna(value) else float(value)


def build_context(
    frame: pd.DataFrame,
    settings: AnalyticsSettings,
    data_settings: DataSettings,
    calendar: WorkingCalendar,
    today: date,
) -> AnalyticsContext:
    """Prepare the daily series the whole layer reads from."""
    if frame.empty:
        start = today
        first = None
        history_days = 0
        daily = kpis.daily_series(frame, start, today)
    else:
        first = frame[S.DATE].min().date()
        # Long enough for the longest window plus the longest trend lookback.
        span = max(required_windows(settings), default=90)
        lookback = max(
            (r.lookback_days for r in resolved_rules(settings) if r.kind == "trend"),
            default=0,
        )
        needed = today - timedelta(days=span + lookback + 30)
        start = min(first, needed)
        history_days = (today - first).days + 1
        daily = kpis.daily_series(frame, start, today)
    return AnalyticsContext(
        daily=daily,
        calendar=calendar,
        settings=settings,
        data_settings=data_settings,
        end=today,
        history_days=history_days,
        history_start=first,
    )


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Alert:
    """One rule's verdict on one scope."""

    rule: str
    metric: str
    metric_label: str
    scope: str
    kind: str
    window: int
    severity: Severity
    current: float | None
    reference: float | None
    reference_label: str
    shortfall_pct: float | None
    message: str
    is_currency: bool

    @property
    def sort_key(self) -> tuple:
        return (
            self.severity.rank,
            -(self.shortfall_pct or 0.0),
            self.scope != OVERALL,
            self.rule,
        )


def _metric_meta(metric: str, data_settings: DataSettings) -> tuple[str, bool]:
    """Display label and whether the metric is money."""
    for definition in kpis.kpi_definitions(data_settings):
        if definition.key == metric:
            return definition.label, definition.is_currency
    return metric, False


def _no_data(
    rule: AlertRuleSettings, scope: str, label: str, currency: bool, why: str
) -> Alert:
    return Alert(
        rule=rule.name,
        metric=rule.metric,
        metric_label=label,
        scope=scope,
        kind=rule.kind,
        window=rule.window,
        severity=Severity.NO_DATA,
        current=None,
        reference=None,
        reference_label="—",
        shortfall_pct=None,
        message=why,
        is_currency=currency,
    )


def _fmt(value: float, currency: bool) -> str:
    if currency:
        return f"${value:,.0f}/day"
    return f"{value:,.2f}/day".replace(".00/day", "/day")


def _evaluate_threshold(
    rule: AlertRuleSettings, current: float, label: str, currency: bool, scope: str
) -> Alert:
    """A **defined** rule: the average must stay above (or below) a set number."""
    breached_critical = breached_warning = False
    if rule.operator == "min":
        breached_critical = rule.critical is not None and current < rule.critical
        breached_warning = rule.warn is not None and current < rule.warn
    else:
        breached_critical = rule.critical is not None and current > rule.critical
        breached_warning = rule.warn is not None and current > rule.warn

    if breached_critical:
        severity, limit = Severity.CRITICAL, rule.critical
    elif breached_warning:
        severity, limit = Severity.WARNING, rule.warn
    else:
        severity = Severity.OK
        limit = rule.warn if rule.warn is not None else rule.critical

    gap = None
    if limit:
        gap = (limit - current) / abs(limit) * 100.0
        if rule.operator == "max":
            gap = -gap

    direction = "below" if rule.operator == "min" else "above"
    word = "floor" if rule.operator == "min" else "ceiling"
    if severity is Severity.OK:
        message = (
            f"{label} {rule.window}-day average is {_fmt(current, currency)}, "
            f"holding its {_fmt(limit, currency)} {word}."
            if limit
            else f"{label} {rule.window}-day average is {_fmt(current, currency)}."
        )
    else:
        message = (
            f"{label} {rule.window}-day average is {_fmt(current, currency)}, "
            f"{abs(gap):.1f}% {direction} the {_fmt(limit, currency)} {word}."
        )

    return Alert(
        rule=rule.name,
        metric=rule.metric,
        metric_label=label,
        scope=scope,
        kind=rule.kind,
        window=rule.window,
        severity=severity,
        current=current,
        reference=limit,
        reference_label=f"{word} {_fmt(limit, currency)}" if limit else "—",
        shortfall_pct=gap,
        message=message,
        is_currency=currency,
    )


def _evaluate_shortfall(
    rule: AlertRuleSettings,
    current: float,
    reference: float,
    reference_label: str,
    label: str,
    currency: bool,
    scope: str,
) -> Alert:
    """An **undefined** rule: judged against the metric's own history."""
    shortfall = (reference - current) / abs(reference) * 100.0

    if rule.critical_pct is not None and shortfall >= rule.critical_pct:
        severity = Severity.CRITICAL
    elif rule.warn_pct is not None and shortfall >= rule.warn_pct:
        severity = Severity.WARNING
    else:
        severity = Severity.OK

    if shortfall >= 0:
        movement = f"{shortfall:.1f}% below"
    else:
        movement = f"{abs(shortfall):.1f}% above"

    message = (
        f"{label} {rule.window}-day average is {_fmt(current, currency)}, "
        f"{movement} its {reference_label} of {_fmt(reference, currency)}."
    )

    return Alert(
        rule=rule.name,
        metric=rule.metric,
        metric_label=label,
        scope=scope,
        kind=rule.kind,
        window=rule.window,
        severity=severity,
        current=current,
        reference=reference,
        reference_label=reference_label,
        shortfall_pct=shortfall,
        message=message,
        is_currency=currency,
    )


def evaluate_rule(
    context: AnalyticsContext,
    rule: AlertRuleSettings,
    scope: str = OVERALL,
    scoped: AnalyticsContext | None = None,
) -> Alert:
    """Run one rule, returning a verdict even when it cannot be answered."""
    source = scoped or context
    label, currency = _metric_meta(rule.metric, context.data_settings)

    # Judged on the scope's own history: an agent who started last month has a
    # month of history, whatever the book behind them has.
    if source.history_days < context.settings.min_history_days:
        return _no_data(
            rule,
            scope,
            label,
            currency,
            f"Only {source.history_days} day(s) of history; this layer needs at "
            f"least {context.settings.min_history_days}.",
        )

    current = source.latest(rule.metric, rule.window)
    if current is None:
        return _no_data(
            rule,
            scope,
            label,
            currency,
            f"Not yet {rule.window} days of history for a {rule.window}-day average.",
        )

    if rule.is_threshold:
        return _evaluate_threshold(rule, current, label, currency, scope)

    if rule.kind == "relative":
        reference = source.latest(rule.metric, rule.baseline)
        reference_label = f"{rule.baseline}-day baseline"
    else:
        reference = source.at_offset(rule.metric, rule.window, rule.lookback_days)
        reference_label = f"level {rule.lookback_days} days ago"

    if reference is None:
        return _no_data(
            rule, scope, label, currency, f"Not enough history for the {reference_label}."
        )
    if reference == 0:
        return _no_data(
            rule,
            scope,
            label,
            currency,
            f"The {reference_label} is zero, so a percentage comparison says nothing.",
        )

    return _evaluate_shortfall(
        rule, current, reference, reference_label, label, currency, scope
    )


def evaluate(context: AnalyticsContext) -> list[Alert]:
    """Every rule against the whole book, worst first."""
    alerts = [evaluate_rule(context, rule) for rule in resolved_rules(context.settings)]
    return sorted(alerts, key=lambda alert: alert.sort_key)


def evaluate_agents(
    context: AnalyticsContext,
    frame: pd.DataFrame,
    calendar: WorkingCalendar,
) -> list[Alert]:
    """The same self-calibrating rules, one agent at a time.

    Only ``relative`` and ``trend`` rules are run per agent: a threshold written
    for the whole book says nothing about one person. Agents too small for a
    percentage swing to mean anything are skipped rather than shown as noise.
    """
    settings = context.settings
    if not settings.monitor_agents or frame.empty:
        return []

    rules = [rule for rule in resolved_rules(settings) if not rule.is_threshold]
    if not rules:
        return []

    baseline = max((rule.baseline for rule in rules), default=90)
    recent = S.slice_dates(
        frame, context.end - timedelta(days=baseline - 1), context.end
    )
    if recent.empty:
        return []

    volumes = recent.groupby(S.AGENT)[S.SALES].sum()
    agents = [
        str(agent)
        for agent, total in volumes.items()
        if float(total) >= settings.agent_min_sales
    ]

    index = context.daily.index
    per_agent = _daily_by_agent(frame, index)

    alerts: list[Alert] = []
    for agent in sorted(agents):
        entry = per_agent.get(agent)
        if entry is None:
            continue
        daily, first_active = entry
        scoped = AnalyticsContext(
            daily=daily,
            calendar=calendar,
            settings=settings,
            data_settings=context.data_settings,
            end=context.end,
            history_days=(context.end - first_active).days + 1,
            history_start=first_active,
            # The index is identical, so the calendar lookup is done once.
            weights=context.weights,
        )
        for rule in rules:
            alert = evaluate_rule(context, rule, scope=agent, scoped=scoped)
            # Only surface agents that are actually slipping; a page listing
            # every agent's "on track" row is a page nobody reads.
            if alert.severity.is_alert:
                alerts.append(alert)

    return sorted(alerts, key=lambda alert: alert.sort_key)


def _daily_by_agent(
    frame: pd.DataFrame, index: pd.DatetimeIndex
) -> dict[str, tuple[pd.DataFrame, date]]:
    """Daily KPI totals per agent, plus each agent's first active day.

    Built in a single pass: re-slicing the whole book once per agent is the
    expensive way to get the same answer.
    """
    working = frame.copy()
    working[kpis.CATEGORY_1] = working[S.SALES].where(
        working[S.CATEGORY_KEY] == S.CATEGORY_1, 0.0
    )
    working[kpis.CATEGORY_2] = working[S.SALES].where(
        working[S.CATEGORY_KEY] == S.CATEGORY_2, 0.0
    )
    grouped = working.groupby(
        [working[S.DATE].dt.normalize(), working[S.AGENT]], sort=False
    )[list(kpis.KPI_KEYS)].sum()

    out: dict[str, tuple[pd.DataFrame, date]] = {}
    for agent, chunk in grouped.groupby(level=1, sort=False):
        flat = chunk.droplevel(1)
        first = flat.index.min().date()
        daily = flat.reindex(index, fill_value=0.0).rename_axis(S.DATE)
        out[str(agent)] = (daily, first)
    return out


def summarise(alerts: list[Alert]) -> dict[Severity, int]:
    counts = {severity: 0 for severity in Severity}
    for alert in alerts:
        counts[alert.severity] += 1
    return counts


def worst(alerts: list[Alert]) -> Severity:
    return min((a.severity for a in alerts), key=lambda s: s.rank, default=Severity.OK)
