"""Checks on the data itself, rather than on what it says.

A broken export and a bad sales week look identical in a moving average. If the
nightly job dies, or the share unmounts, or somebody moves the folder, the
recent days become zeros, the 15-day average collapses, and the Analytics page
reports a confident 40% premium slide -- sending an executive after a sales
problem that is really a dead scheduled job, while the real production numbers
sit unread in a file.

These checks separate the two. They produce the same :class:`Alert` objects the
performance rules do, so they render, sort and export identically, and they sort
*above* a performance alert of the same severity: if the data is wrong, what it
says about performance is noise.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.core.analytics import DATA, Alert, Severity
from app.core.calendar_rules import WorkingCalendar
from app.data import schema as S
from app.data.excel_loader import LoadResult, discover_files
from app.settings import DataHealthSettings, DataSettings

FRESHNESS = "freshness"
VOLUME = "volume"
SOURCES = "sources"


def _alert(
    rule: str,
    kind: str,
    severity: Severity,
    message: str,
    current: float | None = None,
    reference: float | None = None,
    reference_label: str = "—",
    unit: str = "count",
    window: int = 0,
    shortfall_pct: float | None = None,
) -> Alert:
    return Alert(
        rule=rule,
        metric="data",
        metric_label="Data",
        scope=DATA,
        kind=kind,
        window=window,
        severity=severity,
        current=current,
        reference=reference,
        reference_label=reference_label,
        shortfall_pct=shortfall_pct,
        message=message,
        is_currency=False,
        unit=unit,
    )


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def check_freshness(
    frame: pd.DataFrame,
    today: date,
    calendar: WorkingCalendar,
    settings: DataHealthSettings,
) -> Alert:
    """Working days between the newest row and today.

    Counted in working days so a Sunday, or a long holiday weekend, never reads
    as a broken pipeline.
    """
    if frame.empty:
        return _alert(
            "Data freshness",
            FRESHNESS,
            Severity.CRITICAL,
            "No rows loaded at all. The dashboard is showing nothing, not zero.",
        )

    newest = frame[S.DATE].max().date()
    if newest >= today:
        return _alert(
            "Data freshness",
            FRESHNESS,
            Severity.OK,
            f"The newest row is dated {newest:%b %d}, which is today.",
            current=0.0,
            reference=float(settings.stale_warn_days),
            reference_label="working days behind",
            unit="days",
        )

    # The day after the newest row through yesterday: days that should have
    # produced something and did not.
    gap = calendar.working_days_between(newest + timedelta(days=1), today)
    # Today is still in progress, so it does not count against freshness.
    if calendar.is_working_day(today):
        gap = max(gap - 1, 0)

    if gap >= settings.stale_critical_days:
        severity = Severity.CRITICAL
    elif gap >= settings.stale_warn_days:
        severity = Severity.WARNING
    else:
        severity = Severity.OK

    if severity is Severity.OK:
        message = (
            f"The newest row is dated {newest:%b %d} -- "
            f"{_plural(gap, 'working day')} behind, which is within tolerance."
        )
    else:
        message = (
            f"The newest row is dated {newest:%b %d}, {_plural(gap, 'working day')} "
            f"ago. Check the export before trusting anything below: a stalled "
            f"feed looks exactly like a collapse in production."
        )

    return _alert(
        "Data freshness",
        FRESHNESS,
        severity,
        message,
        current=float(gap),
        reference=float(settings.stale_warn_days),
        reference_label="working days behind",
        unit="days",
    )


def check_volume(
    frame: pd.DataFrame,
    today: date,
    calendar: WorkingCalendar,
    settings: DataHealthSettings,
) -> Alert:
    """Recent rows per working day against the same figure over a longer window.

    Catches the partial export -- a file that updates on time but carries a
    fraction of the rows, which freshness alone cannot see.
    """
    if frame.empty:
        return _alert(
            "Row volume",
            VOLUME,
            Severity.NO_DATA,
            "No rows loaded, so there is no volume to compare.",
        )

    first = frame[S.DATE].min().date()
    if (today - first).days + 1 < settings.volume_baseline:
        return _alert(
            "Row volume",
            VOLUME,
            Severity.NO_DATA,
            f"Needs {settings.volume_baseline} days of history to compare against; "
            f"only {(today - first).days + 1} are loaded.",
            window=settings.volume_window,
        )

    def rate(days: int) -> float | None:
        start = today - timedelta(days=days - 1)
        rows = int(((frame[S.DATE].dt.normalize() >= pd.Timestamp(start))
                    & (frame[S.DATE].dt.normalize() <= pd.Timestamp(today))).sum())
        working = calendar.working_days_between(start, today)
        return (rows / working) if working else None

    recent = rate(settings.volume_window)
    baseline = rate(settings.volume_baseline)

    if recent is None or baseline is None or baseline == 0:
        return _alert(
            "Row volume",
            VOLUME,
            Severity.NO_DATA,
            "The baseline volume is zero, so a percentage comparison says nothing.",
            window=settings.volume_window,
        )

    shortfall = (baseline - recent) / baseline * 100.0
    if shortfall >= settings.volume_critical_pct:
        severity = Severity.CRITICAL
    elif shortfall >= settings.volume_warn_pct:
        severity = Severity.WARNING
    else:
        severity = Severity.OK

    movement = (
        f"{shortfall:.0f}% below" if shortfall >= 0 else f"{abs(shortfall):.0f}% above"
    )
    detail = (
        " That is a large enough drop to suggest a partial export rather than a "
        "quiet fortnight."
        if severity is not Severity.OK
        else ""
    )
    message = (
        f"{recent:,.1f} rows per working day over {settings.volume_window} days, "
        f"{movement} the {settings.volume_baseline}-day rate of "
        f"{baseline:,.1f}.{detail}"
    )

    return _alert(
        "Row volume",
        VOLUME,
        severity,
        message,
        current=recent,
        reference=baseline,
        reference_label=f"{settings.volume_baseline}-day rate",
        unit="count",
        window=settings.volume_window,
        shortfall_pct=shortfall,
    )


def check_sources(result: LoadResult, data_settings: DataSettings) -> Alert:
    """Unreadable workbooks and sources that matched nothing."""
    failed = [report for report in result.files if report.error]
    empty_sources = [
        source.name for source in data_settings.sources if not discover_files(source)
    ]

    if not failed and not empty_sources:
        return _alert(
            "Sources",
            SOURCES,
            Severity.OK,
            f"All {result.file_count} workbook(s) read cleanly across "
            f"{len(data_settings.sources)} source(s).",
            current=0.0,
            reference_label="problems",
        )

    problems = []
    if empty_sources:
        problems.append(
            f"{_plural(len(empty_sources), 'source')} matched no files "
            f"({', '.join(empty_sources)})"
        )
    if failed:
        names = ", ".join(sorted({Path(r.path).name for r in failed}))
        problems.append(f"{_plural(len(failed), 'sheet')} could not be read ({names})")

    # A source matching nothing is a broken path; that removes data wholesale.
    severity = Severity.CRITICAL if empty_sources else Severity.WARNING
    return _alert(
        "Sources",
        SOURCES,
        severity,
        "; ".join(problems).capitalize()
        + ". Open Diagnostics for the resolved paths and the reader errors.",
        current=float(len(failed) + len(empty_sources)),
        reference_label="problems",
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def evaluate(
    result: LoadResult,
    frame: pd.DataFrame,
    today: date,
    calendar: WorkingCalendar,
    settings: DataHealthSettings,
    data_settings: DataSettings,
) -> list[Alert]:
    """Every data-health check, worst first.

    ``frame`` is the filtered view the KPIs are computed from; ``result`` is the
    unfiltered load, because a workbook that failed to read is a problem whether
    or not web sales are switched on.
    """
    if not settings.enabled:
        return []

    alerts = [
        check_freshness(frame, today, calendar, settings),
        check_volume(frame, today, calendar, settings),
    ]
    if settings.check_sources:
        alerts.append(check_sources(result, data_settings))
    return sorted(alerts, key=lambda alert: alert.sort_key)


def is_compromised(alerts: list[Alert]) -> bool:
    """True when the data is untrustworthy enough to discount everything else."""
    return any(alert.severity is Severity.CRITICAL for alert in alerts)
