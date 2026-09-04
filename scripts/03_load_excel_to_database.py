"""Load the latest Excel export into the reporting database, with confirmation.

The Admin panel's pipeline step for the ETL. It shows what it is about to do,
asks before touching the database, and reports what changed -- so a nightly load
can be driven by whoever is at the dashboard rather than only from a terminal.

`python tools/etl_excel_to_postgres.py` is the same operation without prompts,
for a scheduler.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import database  # noqa: E402
from app.data.etl import (  # noqa: E402
    Outcome,
    load,
    normalise,
    read_sheet,
    record_run,
    write_csv,
)
from app.data.excel_loader import discover_files  # noqa: E402
from app.settings import POSTGRES, load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    pg = settings.data.postgres

    if settings.data.source_type != POSTGRES:
        print(
            "The dashboard is configured to read from Excel, not the database.\n"
            "Loading anyway is harmless, but set data.source_type: postgres in "
            "config.yaml for the dashboard to show what this loads."
        )

    workbook = None
    for source in settings.data.sources:
        files = discover_files(source)
        if files:
            workbook = max(files, key=lambda p: p.stat().st_mtime)
            sheet, header_row = source.sheet, source.header_row
            break
    if workbook is None:
        print("No workbook matched data.sources in config.yaml.", file=sys.stderr)
        return 1

    modified = datetime.fromtimestamp(workbook.stat().st_mtime)
    print(f"Workbook: {workbook}")
    print(f"Modified: {modified:%Y-%m-%d %H:%M}")
    print(f"Target:   {pg.qualified_table()} on {pg.describe()}")

    probe = database.check(pg)
    if not probe.ok:
        print(f"\nCannot reach the database: {probe.message}", file=sys.stderr)
        return 1
    print(f"Database: {probe.message}\n")

    started = datetime.now().astimezone()
    outcome = Outcome()
    try:
        raw = read_sheet(workbook, sheet, header_row)
        sheet_name = str(raw.attrs.get("sheet_name", sheet or "0"))
        rows = normalise(raw, settings.data, workbook, sheet_name, header_row, outcome)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read the workbook: {exc}", file=sys.stderr)
        return 1

    print(f"Read {outcome.rows_read:,} row(s); {len(rows):,} loadable.")
    for problem in outcome.problems:
        print(f"  ! {problem}")
    if not rows:
        print("Nothing to load.")
        return 0

    answer = input(f"\nMerge {len(rows):,} row(s) into {pg.qualified_table()}? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        print("Cancelled by operator. The database was not touched.")
        return 1

    outcome.csv_path = write_csv(
        rows, Path(__file__).resolve().parent.parent / "runtime/etl", workbook, sheet_name
    )
    print(f"Staged {outcome.csv_path.name}")

    try:
        load(outcome.csv_path, pg, outcome, "update")
    except Exception as exc:  # noqa: BLE001
        print(f"Load failed: {exc}", file=sys.stderr)
        record_run(pg, outcome, workbook, sheet_name, started, "failed", str(exc))
        return 1

    print(
        f"\n{outcome.rows_inserted:,} inserted · {outcome.rows_updated:,} updated · "
        f"{outcome.rows_skipped:,} unchanged"
    )
    record_run(pg, outcome, workbook, sheet_name, started, "succeeded", None)
    print("\nPress Refresh on the dashboard to pick this up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
