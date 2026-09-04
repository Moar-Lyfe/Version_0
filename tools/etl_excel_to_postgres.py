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
    prune,
    read_sheet,
    reconcile,
    record_run,
    write_csv,
)
from app.data.excel_loader import discover_files  # noqa: E402
from app.settings import load_settings  # noqa: E402

EXIT_OK, EXIT_READ_FAILED, EXIT_DB_FAILED = 0, 1, 2

# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def matched_workbooks(settings) -> list[tuple[Path, object, int]]:
    """Every workbook the config matches, oldest first.

    Oldest first matters: when two exports carry the same policy, the later one
    should be the version that survives the merge.
    """
    found: list[tuple[Path, object, int]] = []
    for source in settings.data.sources:
        for path in discover_files(source):
            found.append((path, source.sheet, source.header_row))
    return sorted(found, key=lambda item: item[0].stat().st_mtime)


def resolve_input(settings, explicit: str | None, load_all: bool) -> list[tuple[Path, object, int]]:
    """The workbooks to read: the one named, all of them, or the newest match."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{path} does not exist.")
        source = settings.data.sources[0] if settings.data.sources else None
        return [
            (
                path,
                source.sheet if source else None,
                source.header_row if source else 0,
            )
        ]

    found = matched_workbooks(settings)
    if not found:
        raise FileNotFoundError(
            "No workbook matched data.sources in config.yaml, and --file was not "
            "given."
        )
    return found if load_all else [found[-1]]


def _reconcile(settings, args) -> int:
    """Report -- and optionally delete -- rows Excel no longer has."""
    pg = settings.data.postgres
    try:
        files = matched_workbooks(settings)
    except OSError as exc:
        print(f"Could not list workbooks: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED
    if not files:
        print("No workbook matched data.sources; nothing to compare.", file=sys.stderr)
        return EXIT_READ_FAILED

    print(f"Scanning {len(files)} workbook(s) against {pg.qualified_table()} ...")
    try:
        result = reconcile(files, settings.data, pg)
    except database.DatabaseError as exc:
        print(f"Reconcile failed: {exc}", file=sys.stderr)
        return EXIT_DB_FAILED

    for problem in result.problems:
        print(f"  ! {problem}")
    print(
        f"  {result.keys_in_source:,} row(s) in Excel across "
        f"{len(result.scanned_files)} file(s); "
        f"{result.rows_in_scope:,} database row(s) attributed to them."
    )

    if result.is_clean:
        print("\nIn sync: every database row still exists in its workbook.")
        return EXIT_OK

    print(f"\n{len(result.orphans)} row(s) are in the database but not in Excel:")
    for orphan in result.orphans[:40]:
        print(
            f"  {orphan.sale_date}  {orphan.policy_id or '(no policy)':<12} "
            f"{orphan.agent:<18} {orphan.premium:>12,.2f}  "
            f"{orphan.source_file}:{orphan.source_row}"
        )
    if len(result.orphans) > 40:
        print(f"  ...and {len(result.orphans) - 40} more.")

    total = sum(o.premium for o in result.orphans)
    print(f"\n  {total:,.2f} in premium is still being counted for these rows.")

    if not args.prune:
        print(
            "\nReport only. Re-run with --prune to delete them, once you have "
            "checked the list -- a row missing because somebody filtered the "
            "sheet is not a row that should be deleted."
        )
        return EXIT_OK

    if not args.yes:
        answer = input(f"\nDelete {len(result.orphans)} row(s)? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            print("Cancelled. Nothing was deleted.")
            return EXIT_OK

    try:
        deleted = prune(pg, result.orphans)
    except database.DatabaseError as exc:
        print(f"Prune failed: {exc}", file=sys.stderr)
        return EXIT_DB_FAILED
    print(f"Deleted {deleted:,} row(s).")
    return EXIT_OK


def _load_one(
    path: Path, sheet, header_row: int, settings, args, pg
) -> tuple[int, Outcome]:
    """Read, stage and load one workbook. Returns (exit code, outcome)."""
    started = datetime.now().astimezone()
    outcome = Outcome()

    try:
        raw = read_sheet(path, sheet, header_row)
        sheet_name = str(raw.attrs.get("sheet_name", sheet or "0"))
        rows = normalise(raw, settings.data, path, sheet_name, header_row, outcome)
    except Exception as exc:  # noqa: BLE001
        print(f"Read failed for {path.name}: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED, outcome

    print(f"Read   {path}  [{sheet_name}]")
    print(f"       {outcome.rows_read:,} row(s) read, {len(rows):,} loadable")
    for problem in outcome.problems:
        print(f"       ! {problem}")

    if not rows:
        print("       nothing to load.")
        return EXIT_OK, outcome

    csv_dir = Path(args.csv_dir)
    if not csv_dir.is_absolute():
        csv_dir = Path(__file__).resolve().parent.parent / csv_dir
    outcome.csv_path = write_csv(rows, csv_dir, path, sheet_name)
    print(f"Stage  {outcome.csv_path}")

    if args.csv_only:
        return EXIT_OK, outcome
    if args.dry_run:
        print(
            f"Dry run: {len(rows):,} row(s) would be merged into "
            f"{pg.qualified_table()}."
        )
        return EXIT_OK, outcome

    try:
        load(outcome.csv_path, pg, outcome, args.on_conflict)
    except database.DatabaseError as exc:
        print(f"Load failed: {exc}", file=sys.stderr)
        record_run(pg, outcome, path, sheet_name, started, "failed", str(exc))
        return EXIT_DB_FAILED, outcome
    except Exception as exc:  # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        print(f"Load failed: {message}", file=sys.stderr)
        record_run(pg, outcome, path, sheet_name, started, "failed", message)
        return EXIT_DB_FAILED, outcome

    verb = "updated" if args.on_conflict == "update" else "already present"
    print(
        f"Load   {outcome.rows_staged:,} staged · "
        f"{outcome.rows_inserted:,} inserted · "
        f"{outcome.rows_updated:,} {verb} · "
        f"{outcome.rows_skipped:,} unchanged"
    )
    record_run(pg, outcome, path, sheet_name, started, "succeeded", None)
    return EXIT_OK, outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="Workbook to load. Default: newest matched.")
    parser.add_argument(
        "--all",
        dest="load_all",
        action="store_true",
        help="Load every matched workbook, oldest first, instead of just the newest.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Report rows in the database whose source row is gone from Excel.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="With --reconcile, delete those rows. Asks first unless --yes.",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation for --prune."
    )
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

    if args.reconcile or args.prune:
        return _reconcile(settings, args)

    try:
        workbooks = resolve_input(settings, args.file, args.load_all)
    except (FileNotFoundError, OSError) as exc:
        print(f"Read failed: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED

    if args.sheet is not None:
        workbooks = [(path, args.sheet, header) for path, _, header in workbooks]
    if args.header_row is not None:
        workbooks = [(path, sheet, args.header_row) for path, sheet, _ in workbooks]

    print(f"Target {pg.qualified_table()} @ {pg.describe()}")
    if len(workbooks) > 1:
        print(f"       {len(workbooks)} workbook(s), oldest first\n")

    totals = Outcome()
    for path, sheet, header_row in workbooks:
        code, outcome = _load_one(path, sheet, header_row, settings, args, pg)
        totals.rows_read += outcome.rows_read
        totals.rows_staged += outcome.rows_staged
        totals.rows_inserted += outcome.rows_inserted
        totals.rows_updated += outcome.rows_updated
        totals.rows_skipped += outcome.rows_skipped
        if code != EXIT_OK:
            return code
        if len(workbooks) > 1:
            print()

    if len(workbooks) > 1:
        print(
            f"Total  {totals.rows_read:,} read · {totals.rows_inserted:,} inserted · "
            f"{totals.rows_updated:,} updated · {totals.rows_skipped:,} unchanged"
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
