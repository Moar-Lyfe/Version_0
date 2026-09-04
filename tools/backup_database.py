#!/usr/bin/env python3
"""Back up the reporting database, verify the dump, and rotate old copies.

Once the dashboard reads from Postgres, that database is the system of record
for reporting -- on one machine, with no outbound access and so no off-site copy
arriving by accident.

Each dump is written in PostgreSQL's custom format (compressed, and
`pg_restore` can pull a single table out of it) and then read straight back with
`pg_restore --list`. A backup nobody has ever read is a hope, not a backup.

    python tools/backup_database.py
    python tools/backup_database.py --keep 30
    python tools/backup_database.py --dir /mnt/nas/dashboard-backups
    python tools/backup_database.py --list

Restore, into a scratch database first:

    createdb reporting_restore
    pg_restore -d reporting_restore --no-owner runtime/backups/<file>.dump

Exit codes: 0 written and verified · 1 could not run · 2 dump unverifiable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402
from app.data import backup  # noqa: E402
from app.settings import POSTGRES, load_settings  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_UNVERIFIED = 0, 1, 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", help="Where to write. Default: runtime/backups.")
    parser.add_argument(
        "--keep", type=int, default=14, help="How many dumps to retain (default 14)."
    )
    parser.add_argument("--list", action="store_true", help="List what is on disk.")
    args = parser.parse_args()

    settings = load_settings()
    directory = Path(args.dir) if args.dir else paths.BACKUP_DIR
    if not directory.is_absolute():
        directory = paths.PROJECT_ROOT / directory

    if args.list:
        found = backup.existing(directory)
        if not found:
            print(f"No backups in {directory}.")
            return EXIT_OK
        print(f"{len(found)} backup(s) in {directory}:")
        for path, size, when in found:
            print(f"  {when:%Y-%m-%d %H:%M}  {size / 1_048_576:>8,.1f} MB  {path.name}")
        return EXIT_OK

    if settings.data.source_type != POSTGRES:
        print(
            "The dashboard is reading from Excel, so there is no reporting "
            "database to back up. Backing up anyway if one is configured.",
            file=sys.stderr,
        )

    pg = settings.data.postgres
    print(f"Backing up {pg.describe()} to {directory} ...")

    result = backup.run(pg, directory, keep=args.keep)
    if result.error and not result.ok:
        print(f"\n{result.error}", file=sys.stderr)
        return EXIT_UNVERIFIED if result.path else EXIT_FAILED

    print(
        f"Wrote {result.path.name} — {result.size_pretty}, {result.object_count} "
        f"objects, verified in {result.duration_seconds:.1f}s"
    )
    if result.removed:
        print(f"Rotated out {len(result.removed)} older dump(s), keeping {args.keep}.")

    remaining = backup.existing(directory)
    if remaining:
        oldest = remaining[-1][2]
        print(f"{len(remaining)} backup(s) on disk, oldest {oldest:%Y-%m-%d %H:%M}.")

    if directory.is_relative_to(paths.PROJECT_ROOT):
        print(
            "\nThese sit on the same machine as the database. Copy them somewhere "
            "else — a share, a NAS, anything — or a single disk failure takes both."
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
