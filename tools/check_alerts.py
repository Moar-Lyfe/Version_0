#!/usr/bin/env python3
"""Evaluate the analytics rules from a terminal and exit non-zero on a breach.

Built to be scheduled or dropped into the Admin pipeline, so a metric slipping
is noticed without anyone opening the dashboard.

    python tools/check_alerts.py                 # everything, book-wide + agents
    python tools/check_alerts.py --no-agents     # book-wide rules only
    python tools/check_alerts.py --exclude-web   # ignore web-originated rows
    python tools/check_alerts.py --json          # machine-readable
    python tools/check_alerts.py --csv alerts.csv

Exit codes -- so a scheduler can act on the result:
    0  nothing above the warning line
    1  at least one warning, no criticals
    2  at least one critical
    3  could not evaluate (no data, or not enough history)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import analytics, kpis  # noqa: E402
from app.core.analytics import OVERALL, Severity  # noqa: E402
from app.core.calendar_rules import WorkingCalendar  # noqa: E402
from app.data.excel_loader import load_dataset  # noqa: E402
from app.settings import load_settings  # noqa: E402

EXIT_OK, EXIT_WARNING, EXIT_CRITICAL, EXIT_NO_DATA = 0, 1, 2, 3

SYMBOLS = {
    Severity.CRITICAL: "!!",
    Severity.WARNING: " !",
    Severity.NO_DATA: " ?",
    Severity.OK: " .",
}


def _as_dict(alert) -> dict:
    return {
        "severity": alert.severity.value,
        "rule": alert.rule,
        "scope": alert.scope,
        "metric": alert.metric,
        "metric_label": alert.metric_label,
        "kind": alert.kind,
        "window_days": alert.window,
        "current": alert.current,
        "reference": alert.reference,
        "reference_label": alert.reference_label,
        "shortfall_pct": alert.shortfall_pct,
        "message": alert.message,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-agents", action="store_true", help="Skip per-agent rules.")
    parser.add_argument(
        "--exclude-web", action="store_true", help="Drop web-originated rows first."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout.")
    parser.add_argument("--csv", metavar="PATH", help="Also write the alerts to CSV.")
    parser.add_argument(
        "--quiet", action="store_true", help="Print only warnings and criticals."
    )
    args = parser.parse_args()

    settings = load_settings()
    if not settings.analytics.enabled:
        print("Analytics is disabled in config.yaml.", file=sys.stderr)
        return EXIT_NO_DATA

    result = load_dataset(settings.data)
    if result.frame.empty:
        print("No rows loaded; nothing to evaluate.", file=sys.stderr)
        return EXIT_NO_DATA

    frame = kpis.apply_web_filter(result.frame, include_web=not args.exclude_web)
    calendar = WorkingCalendar.from_settings(settings.calendar)
    today = settings.app.today()

    context = analytics.build_context(
        frame, settings.analytics, settings.data, calendar, today
    )
    alerts = analytics.evaluate(context)
    if not args.no_agents:
        alerts += analytics.evaluate_agents(context, frame, calendar)

    counts = analytics.summarise(alerts)

    if args.json:
        json.dump(
            {
                "evaluated_at": today.isoformat(),
                "rows": result.row_count,
                "history_days": context.history_days,
                "web_sales_included": not args.exclude_web,
                "counts": {s.value: counts[s] for s in Severity},
                "alerts": [_as_dict(a) for a in alerts],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    else:
        print(
            f"{result.row_count:,} rows · {context.history_days} days of history · "
            f"as at {today:%Y-%m-%d}"
            + ("" if not args.exclude_web else " · web sales excluded")
        )
        print(
            f"{counts[Severity.CRITICAL]} critical, {counts[Severity.WARNING]} warning, "
            f"{counts[Severity.NO_DATA]} without data, {counts[Severity.OK]} on track\n"
        )
        for alert in alerts:
            if args.quiet and not alert.severity.is_alert:
                continue
            scope = "" if alert.scope == OVERALL else f"[{alert.scope}] "
            print(f"{SYMBOLS[alert.severity]} {scope}{alert.rule}")
            print(f"     {alert.message}")

    if args.csv:
        target = Path(args.csv)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(_as_dict(alerts[0])))
            writer.writeheader()
            writer.writerows(_as_dict(a) for a in alerts)
        if not args.json:
            print(f"\nWrote {target}")

    if counts[Severity.CRITICAL]:
        return EXIT_CRITICAL
    if counts[Severity.WARNING]:
        return EXIT_WARNING
    # Every rule unanswerable means the check did not really run.
    if counts[Severity.OK] == 0 and counts[Severity.NO_DATA]:
        return EXIT_NO_DATA
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
