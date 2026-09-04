"""Export the current KPI snapshot to CSV, confirming the target first."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.core import kpis  # noqa: E402
from app.core.periods import PERIOD_ORDER, build_periods  # noqa: E402
from app.data.excel_loader import load_dataset  # noqa: E402
from app.settings import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    result = load_dataset(settings.data)
    print(f"Loaded {result.row_count:,} rows from {result.file_count} workbook(s).")

    if result.frame.empty:
        print("Nothing to export.")
        return 1

    # A prompt in the middle of the script: the Admin panel opens an answer box
    # here, and a real terminal simply reads stdin.
    answer = input("Include web sales in the export? [Y/n] ").strip().lower()
    include_web = answer not in {"n", "no"}
    print(f"Web sales {'included' if include_web else 'excluded'}.")

    frame = kpis.apply_web_filter(result.frame, include_web)
    definitions = kpis.kpi_definitions(settings.data)
    periods = build_periods(settings.app.today())

    rows = []
    for key in PERIOD_ORDER:
        period = periods[key]
        values = kpis.compute_window(frame, period.start, period.end)
        rows.append(
            {
                "period": period.label,
                "start": period.start,
                "end": period.end,
                **{d.label: values.get(d.key) for d in definitions},
            }
        )

    out_dir = Path(__file__).resolve().parent.parent / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"kpi_snapshot_{datetime.now():%Y%m%d_%H%M%S}.csv"

    confirm = input(f"Write {target.name} to {out_dir}? [y/N] ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("Cancelled by operator. Nothing written.")
        return 1

    pd.DataFrame(rows).to_csv(target, index=False)
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
