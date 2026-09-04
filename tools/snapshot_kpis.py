#!/usr/bin/env python3
"""Record what the dashboard is reporting today, and detect restatements.

Every figure the dashboard shows is recomputed from the current workbooks, so a
corrected or re-exported row silently changes what last month "was". A daily
snapshot freezes what was reported, which makes "what did we report on
September 1?" answerable and makes a restatement visible.

    python tools/snapshot_kpis.py               # take today's snapshot
    python tools/snapshot_kpis.py --check       # only report restatements
    python tools/snapshot_kpis.py --as-reported 2026-09-01
    python tools/snapshot_kpis.py --list

Run it nightly, after whatever refreshes the workbooks. Exit codes:
    0  snapshot taken (or checked) with nothing unexpected
    1  restatements found -- a closed day's figure has moved
    3  nothing to snapshot
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import kpis, snapshots  # noqa: E402
from app.data.excel_loader import load_dataset  # noqa: E402
from app.settings import load_settings  # noqa: E402

EXIT_OK, EXIT_RESTATED, EXIT_NO_DATA = 0, 1, 3


def _report_restatements(found, symbol: str) -> None:
    print(f"\n{len(found)} restatement(s) since the snapshot of {found[0].as_of}:")
    for item in found:
        prefix = symbol if item.metric == "premium" else ""
        pct = "" if item.delta_pct is None else f" ({item.delta_pct:+.1f}%)"
        print(
            f"  {item.day}  {item.metric:<11} "
            f"{prefix}{item.was:,.2f} -> {prefix}{item.now:,.2f}"
            f"  {item.delta:+,.2f}{pct}"
        )
    print(
        "\nA closed day's figure has changed. That is usually a corrected row or "
        "a re-export -- worth knowing before anyone asks why last month moved."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Report restatements without writing."
    )
    parser.add_argument("--list", action="store_true", help="List snapshots on file.")
    parser.add_argument(
        "--as-reported", metavar="YYYY-MM-DD", help="Print what was reported that day."
    )
    parser.add_argument(
        "--exclude-web", action="store_true", help="Drop web-originated rows first."
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.snapshots.enabled:
        print("Snapshots are disabled in config.yaml.", file=sys.stderr)
        return EXIT_NO_DATA

    if args.list:
        taken = snapshots.snapshot_dates(settings.snapshots)
        if not taken:
            print("No snapshots on file yet.")
            return EXIT_OK
        print(f"{len(taken)} snapshot(s) in {settings.snapshots.resolved_directory()}:")
        for day in taken:
            print(f"  {day}")
        return EXIT_OK

    if args.as_reported:
        try:
            when = date.fromisoformat(args.as_reported)
        except ValueError:
            print(f"'{args.as_reported}' is not a YYYY-MM-DD date.", file=sys.stderr)
            return EXIT_NO_DATA
        rows = snapshots.as_reported(settings.snapshots, when)
        if rows.empty:
            print(f"No snapshot was taken on {when}.", file=sys.stderr)
            return EXIT_NO_DATA
        print(f"Reported on {when}:\n")
        for period, chunk in rows.groupby("period_label", sort=False):
            print(f"  {period}")
            for _, row in chunk.iterrows():
                print(f"    {row['metric_label']:<20} {row['value']:>14,.2f}")
        return EXIT_OK

    result = load_dataset(settings.data)
    if result.frame.empty:
        print("No rows loaded; nothing to snapshot.", file=sys.stderr)
        return EXIT_NO_DATA

    frame = kpis.apply_web_filter(result.frame, include_web=not args.exclude_web)
    today = settings.app.today()
    symbol = settings.app.currency_symbol

    # Look for restatements *before* writing, or today's snapshot becomes the
    # thing we would be comparing against.
    found = snapshots.find_restatements(frame, settings.snapshots, today)

    if not args.check:
        period_path, daily_path = snapshots.take(
            frame, settings.data, settings.snapshots, today
        )
        print(f"Snapshot for {today} written:")
        print(f"  {period_path}")
        print(f"  {daily_path}")
    else:
        print(f"Checking {today} against the archive (nothing written).")

    if found:
        _report_restatements(found, symbol)
        return EXIT_RESTATED

    taken = snapshots.snapshot_dates(settings.snapshots)
    if len(taken) > 1:
        print("\nNo restatements: every previously recorded day still matches.")
    return EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Piping into `head` closes the stream early; that is not an error.
        sys.stderr.close()
        raise SystemExit(EXIT_OK) from None
