"""Check every configured Excel source is readable before anything else runs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.excel_loader import discover_files  # noqa: E402
from app.settings import load_settings  # noqa: E402


def main() -> int:
    settings = load_settings()
    print(f"Configuration: {settings.source_file}")

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

    if problems:
        print(f"\n{problems} source(s) matched nothing. Check data.sources.")
        return 1
    print("\nAll sources readable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
