"""Check every data source is reachable before anything else in the run."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import database  # noqa: E402
from app.data.excel_loader import discover_files  # noqa: E402
from app.settings import POSTGRES, load_settings  # noqa: E402


def check_workbooks(settings) -> int:
    """The ETL's input, and the reporting source when source_type is excel."""
    problems = 0
    for source in settings.data.sources:
        root = source.resolved_path()
        files = discover_files(source)
        status = "OK " if files else "!! "
        print(f"{status}{source.name}: {len(files)} file(s) in {root}")
        for path in files:
            print(f"     - {path.name} ({path.stat().st_size / 1024:,.0f} KB)")
        if not files:
            problems += 1
    return problems


def check_database(settings) -> int:
    pg = settings.data.postgres
    probe = database.check(pg)
    if not probe.ok:
        print(f"!! database: {probe.message}")
        return 1

    print(f"OK database: {probe.message}")
    if probe.server_version:
        print(f"     {probe.server_version}")

    try:
        with database.connect(pg) as connection:
            if not database.table_exists(connection, pg.db_schema, pg.table):
                print(f"!! table {pg.qualified_table()} does not exist.")
                print("     Apply db/schema.sql, or fix data.postgres.table.")
                return 1
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT count(*), min(sale_date), max(sale_date) "
                    f"FROM {pg.qualified_table()}"
                )
                rows, first, last = cursor.fetchone()
    except database.DatabaseError as exc:
        print(f"!! {exc}")
        return 1

    if not rows:
        print(f"!! {pg.qualified_table()} is empty. Has the ETL run?")
        return 1
    print(f"OK {pg.qualified_table()}: {rows:,} row(s), {first} to {last}")
    return 0


def main() -> int:
    settings = load_settings()
    print(f"Configuration: {settings.source_file}")
    print(f"Reporting reads from: {settings.data.source_type}\n")

    problems = check_workbooks(settings)
    if settings.data.source_type == POSTGRES:
        print()
        problems += check_database(settings)

    if problems:
        print(f"\n{problems} problem(s) found. Fix these before loading or reporting.")
        return 1
    print("\nEvery source is reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
