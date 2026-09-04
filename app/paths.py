"""Filesystem anchors.

Every relative path in ``config.yaml`` resolves against :data:`PROJECT_ROOT`, so
the same configuration file behaves identically no matter which directory the
app was launched from.
"""

from __future__ import annotations

from pathlib import Path

# app/paths.py -> app/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
CONFIG_EXAMPLE_FILE = CONFIG_DIR / "config.example.yaml"

# Machine-local state that must never be committed (saved pipelines, run logs).
RUNTIME_DIR = PROJECT_ROOT / "runtime"
# Historic single-pipeline file, still read once so an existing run order
# survives the upgrade into named routines.
PIPELINE_FILE = RUNTIME_DIR / "pipeline.json"
PIPELINES_FILE = RUNTIME_DIR / "pipelines.json"
RUN_LOCK_FILE = RUNTIME_DIR / "pipeline.lock"
OVERRIDE_FILE = RUNTIME_DIR / "admin_override.json"
RUN_LOG_DIR = RUNTIME_DIR / "logs"


def resolve(path: str | Path) -> Path:
    """Resolve *path* against the project root unless it is already absolute."""
    candidate = Path(str(path)).expanduser()
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def ensure_runtime_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
