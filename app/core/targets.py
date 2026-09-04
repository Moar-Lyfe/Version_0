"""Goals, and what they imply about the run rate.

The projection already answers "where will this land?". A target turns that into
the question an executive actually asks: "and is that good enough?"

A target is always a *period total* -- a month or a year. Two derived figures
matter more than the number itself: attainment (how much of it is already
booked) and the daily pace needed to close the gap over the working days that
remain. Both are computed against the same working calendar as everything else,
so the required pace excludes Sundays and holidays rather than quietly assuming
the team works through them.

An absent target means "no target", never zero. Nothing here invents one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.core.periods import MONTH, YEAR, Period
from app.core.projections import ProjectionBasis
from app.settings import TargetSettings


@dataclass(frozen=True)
class Target:
    metric: str
    period_key: str
    value: float
    label: str
    is_override: bool

    @property
    def source(self) -> str:
        return "month override" if self.is_override else "default"


@dataclass(frozen=True)
class Attainment:
    """A target measured against what has actually happened."""

    target: Target
    actual: float
    projected: float | None
    basis: ProjectionBasis | None

    @property
    def achieved_pct(self) -> float:
        return (self.actual / self.target.value * 100.0) if self.target.value else 0.0

    @property
    def projected_pct(self) -> float | None:
        if self.projected is None or not self.target.value:
            return None
        return self.projected / self.target.value * 100.0

    @property
    def gap(self) -> float:
        """What is still needed. Zero once the target is met."""
        return max(self.target.value - self.actual, 0.0)

    @property
    def is_met(self) -> bool:
        return self.actual >= self.target.value

    @property
    def on_track(self) -> bool | None:
        """Whether the current pace lands on the target. ``None`` if unknowable."""
        if self.projected is None:
            return None
        return self.projected >= self.target.value

    @property
    def required_pace(self) -> float | None:
        """Per working day needed over what remains.

        ``None`` when there is nothing left to run, and 0.0 when the target is
        already met -- two different answers that must not be conflated.
        """
        if self.basis is None:
            return None
        if self.is_met:
            return 0.0
        if self.basis.remaining <= 0:
            return None
        return self.gap / self.basis.remaining

    @property
    def current_pace(self) -> float | None:
        if self.basis is None or self.basis.elapsed <= 0:
            return None
        return self.actual / self.basis.elapsed


def resolve(settings: TargetSettings, metric: str, period: Period) -> Target | None:
    """The target in force for ``metric`` over ``period``, if there is one."""
    if not settings.enabled or period.key not in (MONTH, YEAR):
        return None

    anchor: date = period.full_start or period.start

    if period.key == MONTH:
        month_key = f"{anchor.year:04d}-{anchor.month:02d}"
        override = (settings.overrides.get(metric) or {}).get(month_key)
        if override is not None:
            return Target(
                metric=metric,
                period_key=MONTH,
                value=override,
                label=f"{anchor:%B %Y} target",
                is_override=True,
            )

    value = (settings.values.get(metric) or {}).get(period.key)
    if value is None:
        return None

    label = f"{anchor:%B}" if period.key == MONTH else f"{anchor:%Y}"
    return Target(
        metric=metric,
        period_key=period.key,
        value=value,
        label=f"{label} target",
        is_override=False,
    )


def measure(
    settings: TargetSettings,
    metric: str,
    period: Period,
    actual: float,
    projected: float | None = None,
    basis: ProjectionBasis | None = None,
) -> Attainment | None:
    target = resolve(settings, metric, period)
    if target is None:
        return None
    return Attainment(target=target, actual=actual, projected=projected, basis=basis)


def daily_pace_target(
    settings: TargetSettings,
    metric: str,
    periods: dict[str, Period],
    working_days: dict[str, int],
) -> tuple[float, str] | None:
    """The per-working-day rate a period target implies.

    Used to draw a reference line on the moving-average chart, where everything
    is a daily rate rather than a period total. The month target is preferred:
    its horizon is closer to the 15-90 day windows than a year's is.
    """
    for key in (MONTH, YEAR):
        period = periods.get(key)
        if period is None:
            continue
        target = resolve(settings, metric, period)
        days = working_days.get(key, 0)
        if target is not None and days > 0:
            noun = "month" if key == MONTH else "year"
            return target.value / days, f"{noun} target pace"
    return None
