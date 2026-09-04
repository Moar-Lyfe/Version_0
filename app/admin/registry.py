"""Discover runnable scripts and persist the run order.

Scripts live in a directory outside the app (``admin.scripts_dir``). Nothing is
imported or executed to inspect them -- the description is pulled out of the
module docstring with :mod:`ast`, which parses without running a line.

The saved run order lives in ``runtime/pipeline.json`` so it survives restarts
without ever being committed.
"""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from app import paths
from app.settings import AdminSettings


@dataclass(frozen=True)
class ScriptInfo:
    """One discovered script."""

    path: Path
    relative: str
    description: str
    size_bytes: int
    modified_at: datetime | None

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class Step:
    """One entry in the pipeline."""

    script: str
    enabled: bool = True
    # Hold the pipeline and wait for an explicit click before this step runs.
    pause_before: bool = False
    # Extra command-line arguments, parsed with shlex at run time.
    args: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Step":
        return cls(
            script=str(raw.get("script", "")),
            enabled=bool(raw.get("enabled", True)),
            pause_before=bool(raw.get("pause_before", False)),
            args=str(raw.get("args", "") or ""),
        )


@dataclass
class Pipeline:
    """A named, ordered routine.

    The panel runs several -- the ETL load, the morning routine, the general
    script list -- and they share one run lock, because they share one database
    and one set of output files.
    """

    key: str = "scripts"
    label: str = "Scripts"
    description: str = ""
    steps: list[Step] = field(default_factory=list)

    def enabled_steps(self) -> list[Step]:
        return [step for step in self.steps if step.enabled and step.script]


ETL = "etl"
SCRIPTS = "scripts"
MORNING = "morning"

PIPELINE_ORDER = (ETL, MORNING, SCRIPTS)

PIPELINE_META: dict[str, tuple[str, str]] = {
    ETL: (
        "ETL",
        "Move the latest Excel export into the reporting database. "
        "Asks before it writes.",
    ),
    MORNING: (
        "Morning Maintenance",
        "The start-of-day routine: check the sources are reachable, load "
        "overnight's export, record what is being reported, then judge it.",
    ),
    SCRIPTS: (
        "Scripts",
        "Everything else in the scripts folder, in whatever order you set.",
    ),
}

# Which scripts each routine wants. A routine omits any it cannot find, so a
# half-populated scripts folder still gives a usable panel rather than an error.
PIPELINE_DEFAULTS: dict[str, tuple[str, ...]] = {
    ETL: ("03_load_excel_to_database.py",),
    MORNING: (
        "01_validate_sources.py",
        "03_load_excel_to_database.py",
        "04_daily_snapshot.py",
        "05_alert_check.py",
    ),
}


_IGNORED_PREFIXES = ("_", ".")


def _description(path: Path) -> str:
    """First line of the module docstring, without executing the file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return ""
    doc = ast.get_docstring(tree) or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def discover(settings: AdminSettings) -> list[ScriptInfo]:
    """Every script the panel is allowed to run, sorted by filename.

    Filenames sort naturally, so prefixing scripts ``01_``, ``02_`` gives a
    sensible default order before anyone touches the pipeline editor.
    """
    root = settings.resolved_scripts_dir()
    if not root.is_dir():
        return []

    pattern = f"**/{settings.scripts_glob}" if settings.recursive else settings.scripts_glob
    found: list[ScriptInfo] = []
    for path in sorted(root.glob(pattern)):
        if not path.is_file() or path.name.startswith(_IGNORED_PREFIXES):
            continue
        try:
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime).astimezone()
            size = stat.st_size
        except OSError:
            modified, size = None, 0
        found.append(
            ScriptInfo(
                path=path,
                relative=str(path.relative_to(root)),
                description=_description(path),
                size_bytes=size,
                modified_at=modified,
            )
        )
    return found


def resolve_script(settings: AdminSettings, relative: str) -> Path | None:
    """Turn a saved relative path back into a file, refusing directory escapes.

    A pipeline file is editable on disk, so ``../../etc/something.py`` must not
    resolve to anything the panel will run.
    """
    root = settings.resolved_scripts_dir().resolve()
    try:
        candidate = (root / relative).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _meta(key: str) -> tuple[str, str]:
    return PIPELINE_META.get(key, (key.replace("_", " ").title(), ""))


def default_steps(key: str, scripts: list[ScriptInfo]) -> list[Step]:
    """A routine's starting order, from the scripts actually present."""
    available = {info.relative for info in scripts}
    wanted = PIPELINE_DEFAULTS.get(key)
    if wanted is None:  # the general list: everything, filename order
        return [Step(script=info.relative) for info in scripts]
    return [Step(script=name) for name in wanted if name in available]


def _read_all() -> dict[str, list[Step]]:
    """Saved run orders, keyed by routine."""
    if paths.PIPELINES_FILE.exists():
        try:
            raw = json.loads(paths.PIPELINES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            str(key): [Step.from_dict(item) for item in (value or {}).get("steps", [])]
            for key, value in (raw.get("pipelines") or {}).items()
        }

    # Upgrade path: an order saved before routines existed becomes the general
    # script list rather than being thrown away.
    if paths.PIPELINE_FILE.exists():
        try:
            raw = json.loads(paths.PIPELINE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {SCRIPTS: [Step.from_dict(item) for item in raw.get("steps", [])]}

    return {}


def _write_all(saved: dict[str, list[Step]]) -> None:
    paths.ensure_runtime_dirs()
    payload = {
        "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pipelines": {
            key: {"steps": [step.to_dict() for step in steps]}
            for key, steps in saved.items()
        },
    }
    paths.PIPELINES_FILE.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def load_pipeline(
    key: str = SCRIPTS, scripts: list[ScriptInfo] | None = None
) -> Pipeline:
    """One routine's run order: the saved one, else its default."""
    label, description = _meta(key)
    steps = _read_all().get(key)
    if steps is None:
        steps = default_steps(key, scripts or [])
    return Pipeline(key=key, label=label, description=description, steps=steps)


def save_pipeline(pipeline: Pipeline) -> None:
    saved = _read_all()
    saved[pipeline.key] = pipeline.steps
    _write_all(saved)


def reset_pipeline(key: str) -> None:
    """Forget a routine's saved order so it falls back to its default."""
    saved = _read_all()
    saved.pop(key, None)
    _write_all(saved)


# --------------------------------------------------------------------------- #
# Scripts-directory override
# --------------------------------------------------------------------------- #
#
# config.yaml is the durable home for admin.scripts_dir, but pointing the panel
# at a different folder should not require editing YAML and restarting. The
# override below is written to runtime/ (git-ignored) and wins when present.


def load_dir_override() -> str | None:
    if not paths.OVERRIDE_FILE.exists():
        return None
    try:
        raw = json.loads(paths.OVERRIDE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = str(raw.get("scripts_dir") or "").strip()
    return value or None


def save_dir_override(scripts_dir: str | None) -> None:
    paths.ensure_runtime_dirs()
    if not scripts_dir:
        paths.OVERRIDE_FILE.unlink(missing_ok=True)
        return
    paths.OVERRIDE_FILE.write_text(
        json.dumps({"scripts_dir": scripts_dir}, indent=2) + "\n", encoding="utf-8"
    )


def effective_admin_settings(settings: AdminSettings) -> AdminSettings:
    """Admin settings with any saved directory override applied."""
    override = load_dir_override()
    if not override:
        return settings
    return replace(settings, scripts_dir=override)


def default_pipeline(scripts: list[ScriptInfo], key: str = SCRIPTS) -> Pipeline:
    """A routine's starting point, ignoring anything saved."""
    label, description = _meta(key)
    return Pipeline(
        key=key,
        label=label,
        description=description,
        steps=default_steps(key, scripts),
    )
