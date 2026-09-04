"""Moving averages, and the rules that judge them."""

from datetime import date, timedelta

import pandas as pd
import pytest

from app.core import analytics
from app.core.analytics import OVERALL, Severity
from app.core.calendar_rules import WorkingCalendar
from app.data import schema as S
from app.settings import (
    AlertRuleSettings,
    AnalyticsSettings,
    CalendarSettings,
    CategorySettings,
    DataSettings,
    HolidaySettings,
)

# 2026-01-01 is a Thursday, so 2026-01-04 and 2026-01-11 are Sundays.
SUNDAY = date(2026, 1, 4)


@pytest.fixture
def calendar() -> WorkingCalendar:
    """Sundays off, no holidays -- keeps the arithmetic checkable by hand."""
    return WorkingCalendar.from_settings(
        CalendarSettings(exclude_weekdays=("Sunday",), holidays=HolidaySettings())
    )


@pytest.fixture
def data_settings() -> DataSettings:
    return DataSettings(
        category_1=CategorySettings("category_1", "Life", ("Term Life",)),
        category_2=CategorySettings("category_2", "Health", ("Medicare",)),
    )


def make_frame(daily: dict[date, float], agent: str = "Dana") -> pd.DataFrame:
    """One canonical row per date carrying that day's premium and one sale."""
    rows = [
        {
            S.DATE: pd.Timestamp(day),
            S.AGENT: agent,
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


def flat_frame(
    end: date, days: int, value: float, agent: str = "Dana", calendar=None
) -> pd.DataFrame:
    """``value`` booked on every working day, nothing on Sundays.

    That is what a real book looks like, and it is the only shape whose
    working-day average is genuinely flat: a constant *calendar-day* figure
    averages differently depending on how many Sundays a window happens to
    catch.
    """
    calendar = calendar or _default_calendar()
    return make_frame(
        {
            end - timedelta(days=offset): value
            for offset in range(days)
            if calendar.is_working_day(end - timedelta(days=offset))
        },
        agent,
    )


def _default_calendar() -> WorkingCalendar:
    return WorkingCalendar.from_settings(
        CalendarSettings(exclude_weekdays=("Sunday",), holidays=HolidaySettings())
    )


def context(frame, calendar, data_settings, end, **overrides):
    settings = AnalyticsSettings(**overrides)
    return analytics.build_context(frame, settings, data_settings, calendar, end)


# --------------------------------------------------------------------------- #
# Averages
# --------------------------------------------------------------------------- #


def every_day_frame(end: date, days: int, value: float) -> pd.DataFrame:
    """A row on every calendar day, Sundays included -- to test the divisor."""
    return make_frame({end - timedelta(days=offset): value for offset in range(days)})


def test_average_divides_by_working_days(calendar, data_settings):
    # Seven days to 2026-01-10, one of which (Jan 4) is a Sunday.
    frame = every_day_frame(date(2026, 1, 10), 7, 70.0)
    ctx = context(frame, calendar, data_settings, date(2026, 1, 10), windows=(7,))

    # 7 x 70 spread over the 6 days the business was open.
    assert ctx.latest("premium", 7) == pytest.approx(490 / 6)


def test_calendar_basis_divides_by_every_day(calendar, data_settings):
    frame = every_day_frame(date(2026, 1, 10), 7, 70.0)
    ctx = context(
        frame,
        calendar,
        data_settings,
        date(2026, 1, 10),
        windows=(7,),
        basis=analytics.CALENDAR_DAYS,
    )
    assert ctx.latest("premium", 7) == pytest.approx(70.0)


def test_a_partial_window_is_not_averaged(calendar, data_settings):
    """Fewer days than the window is "no answer", not a smaller average."""
    frame = flat_frame(date(2026, 3, 31), 20, 100.0)
    ctx = context(frame, calendar, data_settings, date(2026, 3, 31), windows=(15, 90))

    assert ctx.latest("premium", 15) is not None
    assert ctx.latest("premium", 90) is None


def test_day_weights_follow_the_calendar(calendar):
    index = pd.date_range(date(2026, 1, 1), date(2026, 1, 7), freq="D")
    weights = analytics.day_weights(index, calendar)
    assert weights.sum() == 6.0
    assert weights.loc[pd.Timestamp(SUNDAY)] == 0.0


def test_a_missing_day_counts_as_zero(calendar, data_settings):
    """A day with no rows is a real zero, not a gap to skip over."""
    end = date(2026, 3, 31)
    every_day = flat_frame(end, 30, 100.0)
    ctx_full = context(every_day, calendar, data_settings, end, windows=(15,))

    thinned = every_day.loc[every_day[S.DATE] != pd.Timestamp(end - timedelta(days=1))]
    ctx_gap = context(thinned, calendar, data_settings, end, windows=(15,))

    assert ctx_gap.latest("premium", 15) < ctx_full.latest("premium", 15)


# --------------------------------------------------------------------------- #
# Threshold rules -- "defined"
# --------------------------------------------------------------------------- #


def threshold_rule(**kwargs) -> AlertRuleSettings:
    base = dict(
        name="Premium floor",
        metric="premium",
        kind="threshold",
        window=15,
        operator="min",
        warn=90.0,
        critical=80.0,
    )
    base.update(kwargs)
    return AlertRuleSettings(**base)


@pytest.mark.parametrize(
    "daily_value, expected",
    [
        (100.0, Severity.OK),        # above the warn floor
        (85.0, Severity.WARNING),    # under warn, over critical
        (70.0, Severity.CRITICAL),   # under both
    ],
)
def test_threshold_severities(calendar, data_settings, daily_value, expected):
    end = date(2026, 6, 30)
    frame = flat_frame(end, 120, daily_value)
    ctx = context(frame, calendar, data_settings, end, windows=(15,))

    alert = analytics.evaluate_rule(ctx, threshold_rule())
    assert alert.severity is expected
    assert alert.scope == OVERALL


def test_a_ceiling_rule_fires_on_the_way_up(calendar, data_settings):
    end = date(2026, 6, 30)
    frame = flat_frame(end, 120, 200.0)
    ctx = context(frame, calendar, data_settings, end, windows=(15,))

    rule = threshold_rule(operator="max", warn=100.0, critical=150.0)
    alert = analytics.evaluate_rule(ctx, rule)
    assert alert.severity is Severity.CRITICAL
    assert "above" in alert.message


# --------------------------------------------------------------------------- #
# Relative rules -- "undefined"
# --------------------------------------------------------------------------- #


def relative_rule(**kwargs) -> AlertRuleSettings:
    base = dict(
        name="Premium vs baseline",
        metric="premium",
        kind="relative",
        window=15,
        baseline=90,
        warn_pct=10.0,
        critical_pct=20.0,
    )
    base.update(kwargs)
    return AlertRuleSettings(**base)


def stepped_frame(end: date, history: int, recent_days: int, before: float, after: float):
    """Flat at ``before`` per working day, then stepping to ``after``."""
    calendar = _default_calendar()
    values = {}
    for offset in range(history):
        day = end - timedelta(days=offset)
        if not calendar.is_working_day(day):
            continue
        values[day] = after if offset < recent_days else before
    return make_frame(values)


def test_a_steady_book_raises_nothing(calendar, data_settings):
    end = date(2026, 6, 30)
    ctx = context(flat_frame(end, 200, 100.0), calendar, data_settings, end)

    alert = analytics.evaluate_rule(ctx, relative_rule())
    assert alert.severity is Severity.OK
    assert alert.shortfall_pct == pytest.approx(0.0, abs=0.01)


def test_a_recent_slide_is_caught_without_any_target(calendar, data_settings):
    """The point of an "undefined" rule: no threshold anywhere in sight."""
    end = date(2026, 6, 30)
    frame = stepped_frame(end, 200, recent_days=15, before=100.0, after=60.0)
    ctx = context(frame, calendar, data_settings, end)

    alert = analytics.evaluate_rule(ctx, relative_rule())
    assert alert.severity is Severity.CRITICAL
    assert alert.shortfall_pct > 20.0
    assert "below its 90-day baseline" in alert.message


def test_growth_is_not_an_alert(calendar, data_settings):
    end = date(2026, 6, 30)
    frame = stepped_frame(end, 200, recent_days=15, before=100.0, after=140.0)
    ctx = context(frame, calendar, data_settings, end)

    alert = analytics.evaluate_rule(ctx, relative_rule())
    assert alert.severity is Severity.OK
    assert alert.shortfall_pct < 0
    assert "above" in alert.message


def test_severity_tracks_the_size_of_the_shortfall(calendar, data_settings):
    end = date(2026, 6, 30)
    mild = stepped_frame(end, 200, 15, 100.0, 85.0)
    ctx = context(mild, calendar, data_settings, end)
    assert analytics.evaluate_rule(ctx, relative_rule()).severity is Severity.WARNING


# --------------------------------------------------------------------------- #
# Trend rules
# --------------------------------------------------------------------------- #


def test_trend_compares_the_window_with_its_own_past(calendar, data_settings):
    end = date(2026, 6, 30)
    frame = stepped_frame(end, 300, recent_days=30, before=100.0, after=70.0)
    ctx = context(frame, calendar, data_settings, end)

    rule = AlertRuleSettings(
        name="Premium slide",
        metric="premium",
        kind="trend",
        window=30,
        lookback_days=60,
        warn_pct=5.0,
        critical_pct=15.0,
    )
    alert = analytics.evaluate_rule(ctx, rule)
    assert alert.severity is Severity.CRITICAL
    assert "60 days ago" in alert.message


# --------------------------------------------------------------------------- #
# Unanswerable rules
# --------------------------------------------------------------------------- #


def test_too_little_history_reports_no_data_not_a_breach(calendar, data_settings):
    end = date(2026, 6, 30)
    ctx = context(flat_frame(end, 10, 100.0), calendar, data_settings, end)

    alert = analytics.evaluate_rule(ctx, relative_rule())
    assert alert.severity is Severity.NO_DATA
    assert alert.current is None
    assert "history" in alert.message


def test_a_window_longer_than_the_data_reports_no_data(calendar, data_settings):
    """The padding trap: 40 days of history cannot answer a 90-day baseline.

    The daily series is zero-padded backwards so every window has enough rows,
    which means a naive rolling mean would average across 50 days of padding and
    report a confidently wrong baseline instead of declining to answer.
    """
    end = date(2026, 6, 30)
    ctx = context(
        flat_frame(end, 40, 100.0), calendar, data_settings, end, min_history_days=10
    )
    assert ctx.latest("premium", 15) == pytest.approx(100.0)
    assert ctx.latest("premium", 90) is None

    alert = analytics.evaluate_rule(ctx, relative_rule())
    assert alert.severity is Severity.NO_DATA


def test_a_new_agent_is_not_judged_against_their_own_absence(calendar, data_settings):
    """Same trap one level down: a recent joiner has no 90-day baseline."""
    end = date(2026, 6, 30)
    established = flat_frame(end, 300, 100.0, agent="Established")
    newcomer = flat_frame(end, 20, 100.0, agent="Newcomer")
    frame = pd.concat([established, newcomer], ignore_index=True)

    settings = AnalyticsSettings(rules=(relative_rule(),), agent_min_sales=1.0)
    ctx = analytics.build_context(frame, settings, data_settings, calendar, end)

    # The newcomer raises nothing: unanswerable, not underperforming.
    assert analytics.evaluate_agents(ctx, frame, calendar) == []


def test_a_zero_baseline_is_not_a_percentage(calendar, data_settings):
    end = date(2026, 6, 30)
    frame = stepped_frame(end, 200, recent_days=15, before=0.0, after=0.0)
    ctx = context(frame, calendar, data_settings, end)

    alert = analytics.evaluate_rule(ctx, relative_rule())
    assert alert.severity is Severity.NO_DATA
    assert "zero" in alert.message


def test_an_empty_book_never_raises_a_false_alert(calendar, data_settings):
    end = date(2026, 6, 30)
    ctx = context(S.empty_frame(), calendar, data_settings, end)
    assert all(a.severity is Severity.NO_DATA for a in analytics.evaluate(ctx))


# --------------------------------------------------------------------------- #
# Rules, presets and windows
# --------------------------------------------------------------------------- #


def test_a_preset_runs_when_nothing_is_configured():
    assert analytics.resolved_rules(AnalyticsSettings(preset="standard"))
    assert analytics.resolved_rules(AnalyticsSettings(preset="none")) == ()
    # An unknown preset name falls back rather than leaving the page blind.
    assert analytics.resolved_rules(AnalyticsSettings(preset="nonsense"))


def test_configured_rules_replace_the_preset():
    rule = relative_rule(name="Mine")
    resolved = analytics.resolved_rules(
        AnalyticsSettings(preset="standard", rules=(rule,))
    )
    assert [r.name for r in resolved] == ["Mine"]


def test_disabled_rules_fall_back_to_the_preset():
    rule = relative_rule(name="Off", enabled=False)
    resolved = analytics.resolved_rules(
        AnalyticsSettings(preset="minimal", rules=(rule,))
    )
    assert [r.name for r in resolved] == ["Premium vs 90-day baseline"]


def test_every_window_a_rule_needs_is_computed():
    settings = AnalyticsSettings(
        windows=(15,), rules=(relative_rule(window=20, baseline=120),)
    )
    assert analytics.required_windows(settings) == (15, 20, 120)


def test_every_preset_is_internally_consistent():
    for name, rules in analytics.PRESETS.items():
        for rule in rules:
            assert rule.metric in ("premium", "sales", "category_1", "category_2"), name
            if rule.kind == "relative":
                assert rule.baseline > rule.window, f"{name}: {rule.name}"
            if rule.is_threshold:
                assert rule.warn is not None or rule.critical is not None
            else:
                assert rule.warn_pct is not None or rule.critical_pct is not None


# --------------------------------------------------------------------------- #
# Agents
# --------------------------------------------------------------------------- #


def test_agent_sweep_lists_only_the_ones_slipping(calendar, data_settings):
    end = date(2026, 6, 30)
    steady = flat_frame(end, 200, 100.0, agent="Steady")
    sliding = stepped_frame(end, 200, recent_days=15, before=100.0, after=50.0)
    sliding[S.AGENT] = "Sliding"
    frame = pd.concat([steady, sliding], ignore_index=True)

    settings = AnalyticsSettings(rules=(relative_rule(),), agent_min_sales=1.0)
    ctx = analytics.build_context(frame, settings, data_settings, calendar, end)
    alerts = analytics.evaluate_agents(ctx, frame, calendar)

    assert {alert.scope for alert in alerts} == {"Sliding"}


def test_small_agents_are_left_out_of_the_percentages(calendar, data_settings):
    end = date(2026, 6, 30)
    sliding = stepped_frame(end, 200, recent_days=15, before=100.0, after=10.0)
    sliding[S.AGENT] = "Tiny"

    settings = AnalyticsSettings(rules=(relative_rule(),), agent_min_sales=10_000.0)
    ctx = analytics.build_context(sliding, settings, data_settings, calendar, end)
    assert analytics.evaluate_agents(ctx, sliding, calendar) == []


def test_thresholds_are_never_applied_per_agent(calendar, data_settings):
    """A floor set for the whole book says nothing about one person."""
    end = date(2026, 6, 30)
    frame = flat_frame(end, 200, 10.0, agent="Small")

    settings = AnalyticsSettings(rules=(threshold_rule(warn=1000.0, critical=900.0),))
    ctx = analytics.build_context(frame, settings, data_settings, calendar, end)
    assert analytics.evaluate_agents(ctx, frame, calendar) == []


def test_agent_monitoring_can_be_switched_off(calendar, data_settings):
    end = date(2026, 6, 30)
    frame = stepped_frame(end, 200, 15, 100.0, 40.0)
    settings = AnalyticsSettings(rules=(relative_rule(),), monitor_agents=False)
    ctx = analytics.build_context(frame, settings, data_settings, calendar, end)
    assert analytics.evaluate_agents(ctx, frame, calendar) == []


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #


def test_alerts_sort_worst_first(calendar, data_settings):
    end = date(2026, 6, 30)
    frame = stepped_frame(end, 200, 15, 100.0, 55.0)
    settings = AnalyticsSettings(
        rules=(
            relative_rule(name="Loose", warn_pct=90.0, critical_pct=95.0),
            relative_rule(name="Tight", warn_pct=1.0, critical_pct=2.0),
        )
    )
    ctx = analytics.build_context(frame, settings, data_settings, calendar, end)
    alerts = analytics.evaluate(ctx)

    assert [a.severity for a in alerts] == [Severity.CRITICAL, Severity.OK]
    assert analytics.worst(alerts) is Severity.CRITICAL
    assert analytics.summarise(alerts)[Severity.CRITICAL] == 1


def test_severity_ranking_is_total():
    ranks = [s.rank for s in Severity]
    assert len(set(ranks)) == len(ranks)
    assert Severity.CRITICAL.rank < Severity.WARNING.rank < Severity.OK.rank
    assert Severity.CRITICAL.is_alert and not Severity.NO_DATA.is_alert
