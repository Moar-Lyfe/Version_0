"""A point-in-time record of what was reported.

Every figure the dashboard shows is recomputed from the workbooks as they stand
right now. That is usually what you want -- and it quietly means history moves:
a corrected row, a restated policy, a re-exported month, and last month's
reported premium is no longer the number anyone saw at the time. "What did we
report on September 1?" is a normal executive question, occasionally an audit
one, and without an archive it is unanswerable.

Two files are kept, both small and both append-only:

``period_kpis.csv``
    One row per (as_of, period, metric): the headline figures exactly as the
    dashboard showed them that day. This is the record of what was said.

``daily_metrics.csv``
    One row per (as_of, date) covering a trailing window: the daily totals as
    they looked on that day. Comparing two snapshots of the same date is what
    makes a restatement visible instead of invisible.

Re-running on the same day replaces that day's rows rather than appending
duplicates, so a snapshot step is safe to run more than once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from app.core import kpis
from app.core.periods import PERIOD_ORDER, build_periods
from app.data import schema as S
from app.settings import DataSettings, SnapshotSettings

PERIOD_FILE = "period_kpis.csv"
DAILY_FILE = "daily_metrics.csv"

AS_OF = "as_of"


@dataclass(frozen=True)
class Restatement:
    """A day whose figure changed between a snapshot and now."""

    day: date
    metric: str
    was: float
    now: float
    as_of: date

    @property
    def delta(self) -> float:
        return self.now - self.was

    @property
    def delta_pct(self) -> float | None:
        return (self.delta / abs(self.was) * 100.0) if self.was else None


def _read(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError):
        return pd.DataFrame()


def _write_replacing(path: Path, existing: pd.DataFrame, fresh: pd.DataFrame,
                     as_of: date) -> None:
    """Append ``fresh``, dropping any rows already recorded for ``as_of``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not existing.empty and AS_OF in existing.columns:
        existing = existing.loc[existing[AS_OF].astype(str) != as_of.isoformat()]
    combined = pd.concat([existing, fresh], ignore_index=True) if not existing.empty else fresh
    combined.to_csv(path, index=False)


def period_rows(
    frame: pd.DataFrame, data_settings: DataSettings, as_of: date
) -> pd.DataFrame:
    """The headline figures for every reporting window, as of ``as_of``."""
    periods = build_periods(as_of)
    definitions = kpis.kpi_definitions(data_settings)
    rows = []
    for key in PERIOD_ORDER:
        period = periods[key]
        values = kpis.compute_window(frame, period.start, period.end)
        for definition in definitions:
            rows.append(
                {
                    AS_OF: as_of.isoformat(),
                    "period": key,
                    "period_label": period.label,
                    "start": period.start.isoformat(),
                    "end": period.end.isoformat(),
                    "metric": definition.key,
                    "metric_label": definition.label,
                    "value": values.get(definition.key),
                }
            )
    return pd.DataFrame(rows)


def daily_rows(frame: pd.DataFrame, as_of: date, days: int) -> pd.DataFrame:
    """Daily totals over the trailing window, as they look right now."""
    start = as_of - timedelta(days=days - 1)
    daily = kpis.daily_series(frame, start, as_of)

    counts = pd.Series(0, index=daily.index, dtype="int64")
    if not frame.empty:
        window = S.slice_dates(frame, start, as_of)
        if not window.empty:
            sized = window.groupby(window[S.DATE].dt.normalize()).size()
            counts = sized.reindex(daily.index, fill_value=0).astype("int64")

    out = daily.copy()
    out["rows"] = counts.to_numpy()
    out = out.reset_index().rename(columns={S.DATE: "date"})
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out.insert(0, AS_OF, as_of.isoformat())
    return out


def take(
    frame: pd.DataFrame,
    data_settings: DataSettings,
    settings: SnapshotSettings,
    as_of: date,
) -> tuple[Path, Path]:
    """Record the current state. Returns the two files written."""
    directory = settings.resolved_directory()
    period_path = directory / PERIOD_FILE
    daily_path = directory / DAILY_FILE

    _write_replacing(
        period_path, _read(period_path), period_rows(frame, data_settings, as_of), as_of
    )
    _write_replacing(
        daily_path,
        _read(daily_path),
        daily_rows(frame, as_of, settings.daily_days),
        as_of,
    )
    return period_path, daily_path


# --------------------------------------------------------------------------- #
# Reading back
# --------------------------------------------------------------------------- #


def history(settings: SnapshotSettings) -> pd.DataFrame:
    """Every period snapshot on file."""
    return _read(settings.resolved_directory() / PERIOD_FILE)


def snapshot_dates(settings: SnapshotSettings) -> list[date]:
    frame = history(settings)
    if frame.empty or AS_OF not in frame.columns:
        return []
    return sorted({date.fromisoformat(str(v)) for v in frame[AS_OF].unique()})


def as_reported(
    settings: SnapshotSettings, as_of: date, period: str | None = None
) -> pd.DataFrame:
    """What the dashboard showed on ``as_of``."""
    frame = history(settings)
    if frame.empty:
        return frame
    out = frame.loc[frame[AS_OF].astype(str) == as_of.isoformat()]
    if period:
        out = out.loc[out["period"] == period]
    return out


def find_restatements(
    frame: pd.DataFrame,
    settings: SnapshotSettings,
    as_of: date,
) -> list[Restatement]:
    """Days whose totals have changed since the most recent earlier snapshot.

    Compares the newest snapshot taken *before* today against the data as it
    stands now. A hit means a closed day's number moved -- a corrected row, a
    re-export, a late entry -- which is exactly the thing that silently rewrites
    a month nobody expected to change.
    """
    daily_path = settings.resolved_directory() / DAILY_FILE
    archive = _read(daily_path)
    if archive.empty or AS_OF not in archive.columns:
        return []

    earlier = archive.loc[archive[AS_OF].astype(str) < as_of.isoformat()]
    if earlier.empty:
        return []

    latest_as_of = str(earlier[AS_OF].max())
    previous = earlier.loc[earlier[AS_OF].astype(str) == latest_as_of]
    reference_date = date.fromisoformat(latest_as_of)

    current = daily_rows(frame, as_of, settings.daily_days).set_index("date")
    tolerance = settings.restatement_tolerance
    metrics = [m for m in kpis.KPI_KEYS if m in previous.columns]

    out: list[Restatement] = []
    for _, row in previous.iterrows():
        day_key = str(row["date"])
        if day_key not in current.index:
            continue
        for metric in metrics:
            was = float(row[metric])
            now = float(current.loc[day_key, metric])
            if abs(now - was) > tolerance:
                out.append(
                    Restatement(
                        day=date.fromisoformat(day_key),
                        metric=metric,
                        was=was,
                        now=now,
                        as_of=reference_date,
                    )
                )
    return sorted(out, key=lambda r: (r.day, r.metric))
