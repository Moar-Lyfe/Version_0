#!/usr/bin/env python3
"""Run the saved pipeline from a real terminal.

Same steps, same order, same prompts as the Admin panel -- but stdin is your
terminal, so this is the one to use from a scheduler, over SSH, or when nobody
wants a browser in the loop.

    python tools/run_pipeline.py                # the saved run order
    python tools/run_pipeline.py --list         # show it without running
    python tools/run_pipeline.py --all          # every script, filename order
    python tools/run_pipeline.py --yes          # skip the pause-before prompts
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.admin import registry  # noqa: E402
from app.settings import load_settings  # noqa: E402

RULE = "=" * 62


def _resolve_pipeline(admin, use_all: bool) -> registry.Pipeline:
    scripts = registry.discover(admin)
    if use_all:
        return registry.default_pipeline(scripts)
    pipeline = registry.load_pipeline()
    if pipeline.steps:
        return pipeline
    print("No saved run order; falling back to filename order.\n")
    return registry.default_pipeline(scripts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Show the order and exit.")
    parser.add_argument("--all", action="store_true", help="Ignore the saved order.")
    parser.add_argument(
        "--yes", action="store_true", help="Auto-approve every pause-before step."
    )
    args = parser.parse_args()

    settings = load_settings()
    admin = registry.effective_admin_settings(settings.admin)
    root = admin.resolved_scripts_dir()

    if not root.is_dir():
        print(f"Scripts directory not found: {root}", file=sys.stderr)
        return 2

    pipeline = _resolve_pipeline(admin, args.all)
    steps = pipeline.enabled_steps()

    print(f"Scripts directory: {root}")
    print(f"{len(steps)} enabled step(s):")
    for index, step in enumerate(steps, start=1):
        flags = " [pause]" if step.pause_before else ""
        extra = f" {step.args}" if step.args else ""
        print(f"  {index}. {step.script}{extra}{flags}")
    print()

    if args.list:
        return 0
    if not steps:
        print("Nothing to run.")
        return 1

    for index, step in enumerate(steps, start=1):
        script_path = registry.resolve_script(admin, step.script)
        if script_path is None:
            print(f"[skipped] {step.script} not found inside {root}", file=sys.stderr)
            return 1

        if step.pause_before and not args.yes:
            reply = input(f"Run {step.script}? [Y/n] ").strip().lower()
            if reply in {"n", "no"}:
                print("Stopped by operator.")
                return 1

        working_dir = (
            Path(admin.working_dir) if admin.working_dir else script_path.parent
        )
        command = [sys.executable, "-u", str(script_path)]
        if step.args.strip():
            command.extend(shlex.split(step.args))

        print(f"\n{RULE}")
        print(f"[{index}/{len(steps)}] {step.script}")
        print(f"started {datetime.now():%H:%M:%S} · cwd {working_dir}")
        print(RULE)

        # stdin/stdout are inherited, so input() talks straight to this terminal.
        result = subprocess.run(command, cwd=str(working_dir))
        if result.returncode != 0:
            print(
                f"\n--- {step.script} FAILED (exit {result.returncode}). "
                f"Pipeline stopped. ---",
                file=sys.stderr,
            )
            return result.returncode
        print(f"\n--- {step.script} finished (exit 0) ---")

    print("\nPipeline finished. All steps exited 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
