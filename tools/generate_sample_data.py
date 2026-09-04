#!/usr/bin/env python3
"""Generate realistic sample workbooks so a fresh clone runs immediately.

The output deliberately mirrors what a real export looks like -- friendly header
names, currency written as text, a channel column, the occasional blank agent --
so the column-mapping layer is exercised rather than bypassed.

    python tools/generate_sample_data.py --months 18

Delete ``data/sample`` and point ``config.yaml`` at the real folder once live
workbooks are available.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from app.core.calendar_rules import WorkingCalendar  # noqa: E402
from app.settings import load_settings  # noqa: E402

AGENTS = [
    ("Dana Whitfield", 1.35),
    ("Marcus Vale", 1.20),
    ("Priya Raghavan", 1.10),
    ("Tom Beckett", 0.95),
    ("Aisha Nordstrom", 0.90),
    ("Leo Carrasco", 0.75),
    ("Jenna Okafor", 0.65),
    ("Sam Dietrich", 0.45),
]

PRODUCTS = [
    ("Term Life", "category_1", 0.30, (900, 4200)),
    ("Whole Life", "category_1", 0.14, (2200, 9500)),
    ("Medicare", "category_2", 0.26, (600, 2400)),
    ("Supplement", "category_2", 0.16, (350, 1500)),
    ("Annuity", "other", 0.09, (3000, 18000)),
    ("Final Expense", "other", 0.05, (400, 1800)),
]

CHANNELS = [("Referral", 0.34), ("Inbound Call", 0.28), ("Web", 0.24), ("Walk-In", 0.14)]


def _weighted(options: list[tuple], rng: random.Random):
    weights = [item[-1] if len(item) == 2 else item[2] for item in options]
    return rng.choices(options, weights=weights, k=1)[0]


def build_rows(start: date, end: date, calendar: WorkingCalendar, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    policy_seq = 41200

    day = start
    while day <= end:
        if not calendar.is_working_day(day):
            day += timedelta(days=1)
            continue

        # Seasonality: Q4 open enrolment lifts volume, mid-summer dips.
        season = 1.0 + 0.32 * (day.month in (10, 11, 12)) - 0.14 * (day.month in (6, 7))
        weekday_factor = 0.55 if day.weekday() == 5 else 1.0  # quiet Saturdays
        expected = 11 * season * weekday_factor
        count = max(0, int(rng.gauss(expected, expected * 0.35)))

        for _ in range(count):
            agent, _weight = _weighted(AGENTS, rng)
            product, category, _p, (low, high) = _weighted(PRODUCTS, rng)
            channel, _c = _weighted(CHANNELS, rng)

            # Web business is booked to a house account, not to a producer.
            if channel == "Web":
                agent = "Web Sales" if rng.random() < 0.75 else agent

            premium = round(rng.uniform(low, high), 2)
            policy_seq += 1
            rows.append(
                {
                    "Date": day,
                    "Agent Name": agent,
                    "Product": product,
                    "Channel": channel,
                    "Policy Number": f"P-{policy_seq}",
                    # Written as display text on purpose -- the loader must cope.
                    "Written Premium": f"${premium:,.2f}",
                    "Units": 1,
                }
            )
        day += timedelta(days=1)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=20, help="History to generate.")
    parser.add_argument("--out", default="data/sample", help="Output directory.")
    parser.add_argument("--seed", type=int, default=20260904)
    args = parser.parse_args()

    settings = load_settings()
    calendar = WorkingCalendar.from_settings(settings.calendar)

    today = settings.app.today()
    start = today - timedelta(days=int(args.months * 30.44))
    rows = build_rows(start, today, calendar, args.seed)

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = Path(__file__).resolve().parent.parent / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    frame = pd.DataFrame(rows)
    # Split by year so the multi-workbook path gets exercised too.
    written: list[Path] = []
    for year, chunk in frame.groupby(frame["Date"].map(lambda d: d.year)):
        target = out_dir / f"production_{year}.xlsx"
        chunk.to_excel(target, index=False, sheet_name="Production")
        written.append(target)

    print(f"Generated {len(frame):,} rows across {len(written)} workbook(s):")
    for path in written:
        print(f"  {path}")
    print(f"Date range: {frame['Date'].min()} -> {frame['Date'].max()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
