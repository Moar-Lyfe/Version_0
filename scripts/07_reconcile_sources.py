"""Report database rows whose source row is no longer in the Excel workbooks.

The merge inserts and updates; it has no concept of a row that went away. A
voided policy or a deleted line keeps being counted, silently, until someone
looks. This looks.

It reports only. Deleting is `python tools/etl_excel_to_postgres.py --reconcile
--prune`, after a person has read the list — a row missing because somebody
filtered the sheet is not a row that should be deleted.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.etl import reconcile  # noqa: E402
from app.data.excel_loader import discover_files  # noqa: E402
from app.settings import POSTGRES, load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    if settings.data.source_type != POSTGRES:
        print("Reading from Excel; there is nothing to reconcile against.")
        return 0

    files = [
        (path, source.sheet, source.header_row)
        for source in settings.data.sources
        for path in discover_files(source)
    ]
    if not files:
        print("No workbook matched data.sources; nothing to compare.", file=sys.stderr)
        return 1

    pg = settings.data.postgres
    print(f"Scanning {len(files)} workbook(s) against {pg.qualified_table()} ...")
    result = reconcile(files, settings.data, pg)

    for problem in result.problems:
        print(f"  ! {problem}")
    print(
        f"  {result.keys_in_source:,} row(s) in Excel; "
        f"{result.rows_in_scope:,} database row(s) attributed to those files."
    )

    if result.is_clean:
        print("\nIn sync: every database row still exists in its workbook.")
        return 0

    total = sum(o.premium for o in result.orphans)
    print(f"\n{len(result.orphans)} row(s) in the database are no longer in Excel:")
    for orphan in result.orphans[:25]:
        print(
            f"  {orphan.sale_date}  {orphan.policy_id or '(no policy)':<12} "
            f"{orphan.agent:<18} {orphan.premium:>12,.2f}  "
            f"{orphan.source_file}:{orphan.source_row}"
        )
    if len(result.orphans) > 25:
        print(f"  ...and {len(result.orphans) - 25} more.")
    print(f"\n  {total:,.2f} in premium is still being counted for these rows.")
    print(
        "\nReport only. Check the list, then delete with:\n"
        "  python tools/etl_excel_to_postgres.py --reconcile --prune"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
