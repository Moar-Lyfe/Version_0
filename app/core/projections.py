"""Straight-line projections off the working calendar.

    projected = actual_to_date / working_days_elapsed * working_days_in_period

Sundays and observed holidays are excluded from both counts, so a month with
two holidays projects against the days the business is actually open rather than
against raw calendar days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.core.calendar_rules import WorkingCalendar
from app.core.kpis import KPI_KEYS, KpiValues
from app.core.periods import Period
from app.settings import ProjectionSettings


@dataclass(frozen=True)
class ProjectionBasis:
    """The working-day arithmetic behind a projection, shown in the UI."""

    elapsed: int
    total: int
    remaining: int
    counts_today: bool
    period_end: date

    @property
    def is_valid(self) -> bool:
        return self.elapsed > 0 and self.total > 0

    @property
    def completion(self) -> float:
        """Fraction of the period's working days already elapsed (0-1)."""
        return (self.elapsed / self.total) if self.total else 0.0


@dataclass(frozen=True)
class Projection:
    """Full-period estimate for one KPI."""

    key: str
    actual: float
    projected: float
    daily_pace: float
    basis: ProjectionBasis

    @property
    def remaining(self) -> float:
        return max(self.projected - self.actual, 0.0)


def build_basis(
    period: Period,
    today: date,
    calendar: WorkingCalendar,
    settings: ProjectionSettings,
) -> ProjectionBasis | None:
    """Working-day counts for ``period``, or ``None`` if it is not projectable."""
    if not period.is_projectable or period.full_start is None or period.full_end is None:
        return None

    # Once the period has closed there is nothing left to project.
    elapsed_end = min(today, period.full_end)
    if (
        not settings.count_today_as_elapsed
        and elapsed_end == today
        and today <= period.full_end
    ):
        # Today is still in progress, so it must not count as an elapsed day.
        elapsed_end = today - timedelta(days=1)

    elapsed = calendar.working_days_between(period.full_start, elapsed_end)
    total = calendar.working_days_between(period.full_start, period.full_end)
    return ProjectionBasis(
        elapsed=elapsed,
        total=total,
        remaining=max(total - elapsed, 0),
        counts_today=settings.count_today_as_elapsed,
        period_end=period.full_end,
    )


def project(
    values: KpiValues,
    basis: ProjectionBasis | None,
) -> dict[str, Projection]:
    """Project every KPI to the end of the period."""
    if basis is None or not basis.is_valid:
        return {}
    out: dict[str, Projection] = {}
    for key in KPI_KEYS:
        actual = values.get(key)
        pace = actual / basis.elapsed
        out[key] = Projection(
            key=key,
            actual=actual,
            projected=pace * basis.total,
            daily_pace=pace,
            basis=basis,
        )
    return out
