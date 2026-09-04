#!/usr/bin/env python3
"""Load an Excel export into the reporting database, via CSV.

Excel stays the operational front end. This moves each export into Postgres in
three visible stages, so a failure is always attributable to one of them:

    1. READ     the workbook and the named sheet, normalised with exactly the
                same column mapping and cell parsing the dashboard used to do
                directly -- currency text, accounting negatives, blank agents.
    2. STAGE    write a CSV to the archive directory. That file is the record of
                what was handed to the database, and re-loading it later needs
                no Excel at all.
    3. LOAD     COPY the CSV into a temporary table, then merge into `sales`.

The merge is keyed on a natural key built from the source row (the policy
number, by default), which is what makes the load **idempotent**: running the
same workbook twice does not duplicate anything.

    python tools/etl_excel_to_postgres.py
    python tools/etl_excel_to_postgres.py --file exports/september.xlsx --sheet Production
    python tools/etl_excel_to_postgres.py --dry-run        # read and stage, do not load
    python tools/etl_excel_to_postgres.py --csv-only       # stop after the CSV
    python tools/etl_excel_to_postgres.py --on-conflict ignore

Exit codes:
    0  loaded (or nothing to do)
    1  read/parse failure -- nothing was loaded
    2  database failure -- nothing was committed
"""

from __future__ import annotations

import argparse
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
from app.settings import load_settings  # noqa: E402

EXIT_OK, EXIT_READ_FAILED, EXIT_DB_FAILED = 0, 1, 2

# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def resolve_input(settings, explicit: str | None) -> tuple[Path, object, int]:
    """The workbook to read: the one named, else the newest the config matches."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{path} does not exist.")
        source = settings.data.sources[0] if settings.data.sources else None
        return path, (source.sheet if source else None), (source.header_row if source else 0)

    for source in settings.data.sources:
        files = discover_files(source)
        if files:
            newest = max(files, key=lambda p: p.stat().st_mtime)
            return newest, source.sheet, source.header_row
    raise FileNotFoundError(
        "No workbook matched data.sources in config.yaml, and --file was not given."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="Workbook to load. Default: newest matched.")
    parser.add_argument("--sheet", help="Sheet name or index. Default: from config.")
    parser.add_argument("--header-row", type=int, help="Zero-based header row.")
    parser.add_argument(
        "--csv-dir", default="runtime/etl", help="Where staging CSVs are archived."
    )
    parser.add_argument(
        "--on-conflict",
        choices=("update", "ignore"),
        default="update",
        help="update (default) applies corrections; ignore only adds new rows.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Read and stage, but do not load."
    )
    parser.add_argument(
        "--csv-only", action="store_true", help="Stop after writing the CSV."
    )
    args = parser.parse_args()

    settings = load_settings()
    pg = settings.data.postgres
    started = datetime.now().astimezone()
    outcome = Outcome()

    # --- 1. read ---------------------------------------------------------- #
    try:
        path, sheet, header_row = resolve_input(settings, args.file)
        if args.sheet is not None:
            sheet = args.sheet
        if args.header_row is not None:
            header_row = args.header_row

        raw = read_sheet(path, sheet, header_row)
        sheet_name = str(raw.attrs.get("sheet_name", sheet or "0"))
        rows = normalise(raw, settings.data, path, sheet_name, header_row, outcome)
    except Exception as exc:  # noqa: BLE001 - reported, not raised at a terminal
        print(f"Read failed: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED

    print(f"Read   {path}  [{sheet_name}]")
    print(f"       {outcome.rows_read:,} row(s) read, {len(rows):,} loadable")
    for problem in outcome.problems:
        print(f"       ! {problem}")

    if not rows:
        print("Nothing to load.")
        return EXIT_OK

    # --- 2. stage --------------------------------------------------------- #
    csv_dir = Path(args.csv_dir)
    if not csv_dir.is_absolute():
        csv_dir = Path(__file__).resolve().parent.parent / csv_dir
    outcome.csv_path = write_csv(rows, csv_dir, path, sheet_name)
    print(f"Stage  {outcome.csv_path}")

    if args.csv_only:
        print("Stopped after the CSV (--csv-only).")
        return EXIT_OK
    if args.dry_run:
        print(f"Dry run: {len(rows):,} row(s) would be merged into {pg.qualified_table()}.")
        return EXIT_OK

    # --- 3. load ---------------------------------------------------------- #
    try:
        load(outcome.csv_path, pg, outcome, args.on_conflict)
    except database.DatabaseError as exc:
        print(f"Load failed: {exc}", file=sys.stderr)
        record_run(pg, outcome, path, sheet_name, started, "failed", str(exc))
        return EXIT_DB_FAILED
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        print(f"Load failed: {message}", file=sys.stderr)
        record_run(pg, outcome, path, sheet_name, started, "failed", message)
        return EXIT_DB_FAILED

    verb = "updated" if args.on_conflict == "update" else "already present"
    print(f"Load   {pg.qualified_table()} @ {pg.describe()}")
    print(
        f"       {outcome.rows_staged:,} staged · "
        f"{outcome.rows_inserted:,} inserted · "
        f"{outcome.rows_updated:,} {verb} · "
        f"{outcome.rows_skipped:,} unchanged"
    )
    record_run(pg, outcome, path, sheet_name, started, "succeeded", None)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
