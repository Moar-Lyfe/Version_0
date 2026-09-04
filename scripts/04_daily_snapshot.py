"""Record what is being reported today, and flag anything that has changed."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import kpis, snapshots  # noqa: E402
from app.data.loader import load_for_source  # noqa: E402
from app.settings import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    if not settings.snapshots.enabled:
        print("Snapshots are disabled in config.yaml; nothing to do.")
        return 0

    result = load_for_source(settings.data)
    if result.frame.empty:
        print("No rows loaded; nothing to snapshot.", file=sys.stderr)
        return 1

    frame = kpis.apply_web_filter(result.frame, include_web=True)
    today = settings.app.today()

    # Look for restatements before writing, or today's own snapshot becomes the
    # thing today would be compared against.
    restated = snapshots.find_restatements(frame, settings.snapshots, today)

    period_path, daily_path = snapshots.take(
        frame, settings.data, settings.snapshots, today
    )
    print(f"Snapshot for {today}: {result.row_count:,} row(s)")
    print(f"  {period_path.name}")
    print(f"  {daily_path.name}")

    if not restated:
        taken = snapshots.snapshot_dates(settings.snapshots)
        if len(taken) > 1:
            print("\nNo restatements: every recorded day still matches.")
        return 0

    print(f"\n{len(restated)} restatement(s) since {restated[0].as_of}:")
    for item in restated[:20]:
        pct = "" if item.delta_pct is None else f" ({item.delta_pct:+.1f}%)"
        print(f"  {item.day}  {item.metric:<11} {item.was:,.2f} -> {item.now:,.2f}{pct}")
    if len(restated) > 20:
        print(f"  ...and {len(restated) - 20} more.")
    print(
        "\nA closed day's figure has moved -- usually a corrected row or a "
        "re-export. Worth knowing before anyone asks why last month changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
