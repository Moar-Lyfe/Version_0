"""Evaluate the analytics rules and report anything slipping."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import analytics, data_health, kpis  # noqa: E402
from app.core.analytics import OVERALL, Severity  # noqa: E402
from app.core.calendar_rules import WorkingCalendar  # noqa: E402
from app.data.loader import load_for_source  # noqa: E402
from app.settings import load_settings  # noqa: E402

SYMBOLS = {
    Severity.CRITICAL: "!!",
    Severity.WARNING: " !",
    Severity.NO_DATA: " ?",
    Severity.OK: " .",
}


def main() -> int:
    settings = load_settings()
    if not settings.analytics.enabled:
        print("Analytics is disabled in config.yaml; nothing to check.")
        return 0

    result = load_for_source(settings.data)
    if result.frame.empty:
        print("No rows loaded; nothing to check.", file=sys.stderr)
        return 1

    frame = kpis.apply_web_filter(result.frame, include_web=True)
    calendar = WorkingCalendar.from_settings(settings.calendar)
    today = settings.app.today()

    health = data_health.evaluate(
        result, frame, today, calendar, settings.analytics.data_health, settings.data
    )
    context = analytics.build_context(
        frame, settings.analytics, settings.data, calendar, today
    )
    alerts = health + analytics.evaluate(context)
    alerts += analytics.evaluate_agents(context, frame, calendar)

    counts = analytics.summarise(alerts)
    print(
        f"{result.row_count:,} rows · {counts[Severity.CRITICAL]} critical, "
        f"{counts[Severity.WARNING]} warning, {counts[Severity.OK]} on track"
    )

    if data_health.is_compromised(health):
        print(
            "\nDATA PROBLEM: the performance rules below sit on a feed that is "
            "not current. Fix the data before acting on them."
        )

    problems = [a for a in alerts if a.severity.is_alert]
    if not problems:
        print("\nNothing above the warning line.")
        return 0

    print()
    for alert in problems:
        scope = "" if alert.scope == OVERALL else f"[{alert.scope}] "
        print(f"{SYMBOLS[alert.severity]} {scope}{alert.rule}")
        print(f"     {alert.message}")

    # Deliberately exits 0. "The business is slipping" and "the script failed"
    # are different things, and a routine that ends red for the first would
    # teach people to ignore the second. `tools/check_alerts.py` keeps the
    # meaningful exit codes, for the scheduler where they actually signal.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
