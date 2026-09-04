"""Backing up the reporting database.

Once the dashboard reads from Postgres rather than the workbooks, that database
is the system of record for reporting -- on one LAN machine, with no outbound
access and therefore no off-site copy arriving by accident. A disk failure would
mean rebuilding from whatever exports still happen to exist.

Two decisions worth naming:

* **Custom format** (``pg_dump -Fc``). It compresses, and ``pg_restore`` can
  pull a single table out of it, which is what you actually want at 9am when one
  table is wrong and the rest is fine.
* **Every dump is verified.** ``pg_restore --list`` is run against the file
  immediately. A backup nobody has ever read is a hope, not a backup, and the
  cheapest moment to discover a truncated file is the moment it is written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.settings import PostgresSettings

# Where the server's own binaries live when they are not on PATH.
_SEARCH_GLOBS = ("/usr/lib/postgresql/*/bin", "/usr/pgsql-*/bin", "/opt/homebrew/bin")


@dataclass
class BackupResult:
    ok: bool = False
    path: Path | None = None
    size_bytes: int = 0
    duration_seconds: float = 0.0
    verified: bool = False
    object_count: int = 0
    removed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def size_pretty(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:,.1f} {unit}" if unit != "B" else f"{size:,.0f} B"
            size /= 1024
        return f"{size:,.1f} GB"


def find_tool(name: str) -> str | None:
    """Locate ``pg_dump`` / ``pg_restore``, on PATH or in a server install."""
    found = shutil.which(name)
    if found:
        return found
    for pattern in _SEARCH_GLOBS:
        for directory in sorted(Path("/").glob(pattern.lstrip("/")), reverse=True):
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _environment(pg: PostgresSettings) -> dict[str, str]:
    env = os.environ.copy()
    password = pg.password()
    if password:
        # Handed to the child process only; never written to disk or logged.
        env["PGPASSWORD"] = password
    return env


def _explain(stderr: str, pg: PostgresSettings) -> str:
    text = stderr.strip().splitlines()
    detail = text[-1] if text else "pg_dump failed."
    lowered = detail.lower()
    if "server version" in lowered and "pg_dump version" in lowered:
        return (
            f"{detail}\n\nThe pg_dump on this machine is older than the server. "
            "Install a client at least as new as PostgreSQL itself "
            "(postgresql-client-<version>)."
        )
    if "authentication failed" in lowered:
        source = (
            f"the {pg.password_env} environment variable"
            if pg.password_env
            else "libpq (~/.pgpass or PGPASSWORD)"
        )
        return f"{detail} The password comes from {source}."
    return detail


def rotate(directory: Path, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` dumps. Returns what went."""
    if keep <= 0:
        return []
    dumps = sorted(
        directory.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    removed: list[str] = []
    for stale in dumps[keep:]:
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError:
            pass
    return removed


def verify(path: Path) -> tuple[bool, int, str | None]:
    """Read the dump back. Returns (ok, object count, error)."""
    tool = find_tool("pg_restore")
    if tool is None:
        return False, 0, "pg_restore is not installed, so the dump was not verified."
    try:
        completed = subprocess.run(
            [tool, "--list", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, 0, f"Could not read the dump back: {exc}"
    if completed.returncode != 0:
        return False, 0, (completed.stderr.strip().splitlines() or ["unreadable"])[-1]

    entries = [
        line
        for line in completed.stdout.splitlines()
        if line.strip() and not line.startswith(";")
    ]
    return True, len(entries), None


def run(
    pg: PostgresSettings,
    directory: Path,
    keep: int = 14,
    timeout_seconds: int = 1800,
) -> BackupResult:
    """Dump the database, verify it, and rotate older copies."""
    tool = find_tool("pg_dump")
    if tool is None:
        return BackupResult(
            error=(
                "pg_dump is not installed. Install the PostgreSQL client tools "
                "(postgresql-client) on this machine."
            )
        )

    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = directory / f"{pg.database}_{stamp}.dump"

    command = [
        tool,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        f"--host={pg.host}",
        f"--port={pg.port}",
        f"--username={pg.user}",
        f"--dbname={pg.database}",
        f"--file={target}",
    ]

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=_environment(pg),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        return BackupResult(error=f"pg_dump exceeded {timeout_seconds}s and was stopped.")
    except OSError as exc:
        return BackupResult(error=f"Could not run pg_dump: {exc}")

    duration = time.perf_counter() - started
    if completed.returncode != 0:
        # A partial file is worse than none -- it looks like a backup.
        target.unlink(missing_ok=True)
        return BackupResult(error=_explain(completed.stderr, pg), duration_seconds=duration)

    size = target.stat().st_size if target.exists() else 0
    verified, objects, verify_error = verify(target)
    if not verified:
        return BackupResult(
            ok=False,
            path=target,
            size_bytes=size,
            duration_seconds=duration,
            error=f"The dump was written but could not be read back: {verify_error}",
        )

    return BackupResult(
        ok=True,
        path=target,
        size_bytes=size,
        duration_seconds=duration,
        verified=True,
        object_count=objects,
        removed=rotate(directory, keep),
    )


def existing(directory: Path) -> list[tuple[Path, int, datetime]]:
    """Backups on disk, newest first."""
    if not directory.is_dir():
        return []
    out = []
    for path in directory.glob("*.dump"):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append((path, stat.st_size, datetime.fromtimestamp(stat.st_mtime)))
    return sorted(out, key=lambda item: item[2], reverse=True)
