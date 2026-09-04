"""Back up the reporting database and verify the dump can be read back."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402
from app.data import backup  # noqa: E402
from app.settings import POSTGRES, load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    if settings.data.source_type != POSTGRES:
        print("Reading from Excel; no reporting database to back up.")
        return 0

    pg = settings.data.postgres
    print(f"Backing up {pg.describe()} ...")

    result = backup.run(pg, paths.BACKUP_DIR, keep=14)
    if not result.ok:
        print(f"\n{result.error}", file=sys.stderr)
        return 1

    print(
        f"{result.path.name} — {result.size_pretty}, {result.object_count} objects, "
        f"verified in {result.duration_seconds:.1f}s"
    )
    if result.removed:
        print(f"Rotated out {len(result.removed)} older dump(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
