"""A single-holder lock for pipeline runs.

The runner lives in Streamlit's per-session state, so without this two people
with the dashboard open -- or one person in a browser and a scheduled job in a
terminal -- can start the same scripts at the same moment. For read-only checks
that is harmless; for anything writing to a shared drive it is corruption.

The lock is a file created with ``O_EXCL``, which is atomic on every platform
this runs on, so two processes racing cannot both win.

**Stale locks are the hard part.** A machine that loses power mid-run leaves the
file behind, and a lock nobody can clear is worse than no lock at all. Three
things are recorded to make staleness decidable: the host, the process id, and
the start time. A lock is stale when the owning process is gone (checked only on
the same host, and only where the check is safe), or when it is older than a run
could legitimately last.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import paths

# A run cannot legitimately outlive the script timeout by more than the time it
# takes to tear one down.
STALE_GRACE_SECONDS = 300


@dataclass(frozen=True)
class RunLock:
    token: str
    pid: int
    host: str
    owner: str
    started_at: datetime

    @property
    def age(self) -> timedelta:
        return datetime.now().astimezone() - self.started_at

    @property
    def is_ours(self) -> bool:
        """Held by this very process, rather than another one on this machine."""
        return self.host == socket.gethostname() and self.pid == os.getpid()

    def describe(self) -> str:
        where = "this machine" if self.host == socket.gethostname() else self.host
        minutes = self.age.total_seconds() / 60
        when = (
            f"{minutes:.0f} minutes ago" if minutes >= 1 else "less than a minute ago"
        )
        return f"{self.owner} on {where} (pid {self.pid}), started {when}"

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "pid": self.pid,
            "host": self.host,
            "owner": self.owner,
            "started_at": self.started_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RunLock | None":
        try:
            return cls(
                token=str(raw["token"]),
                pid=int(raw["pid"]),
                host=str(raw["host"]),
                owner=str(raw.get("owner") or "unknown"),
                started_at=datetime.fromisoformat(str(raw["started_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _process_alive(pid: int) -> bool:
    """Whether ``pid`` is still running, where that can be asked safely.

    POSIX only. On Windows ``os.kill(pid, 0)`` does not mean "probe" -- CPython
    maps it onto ``TerminateProcess``, which would kill the very process we are
    asking about. There, staleness is decided by age alone.
    """
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Owned by another user, but definitely alive.
        return True
    except OSError:
        return True
    return True


def read() -> RunLock | None:
    """The lock currently on disk, if any and if it parses."""
    if not paths.RUN_LOCK_FILE.exists():
        return None
    try:
        raw = json.loads(paths.RUN_LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return RunLock.from_dict(raw)


def is_stale(lock: RunLock, max_age_seconds: int) -> bool:
    """Whether ``lock`` can be discarded without stepping on a live run."""
    if lock.age.total_seconds() > max_age_seconds + STALE_GRACE_SECONDS:
        return True
    # A pid only means something on the machine that issued it.
    if lock.host == socket.gethostname() and not _process_alive(lock.pid):
        return True
    return False


def acquire(owner: str, max_age_seconds: int = 3600) -> tuple[RunLock | None, RunLock | None]:
    """Take the lock.

    Returns ``(lock, holder)``: exactly one is set. ``lock`` is yours to release;
    ``holder`` is whoever has it, when you could not.
    """
    paths.ensure_runtime_dirs()
    candidate = RunLock(
        token=uuid.uuid4().hex,
        pid=os.getpid(),
        host=socket.gethostname(),
        owner=owner,
        started_at=datetime.now().astimezone(),
    )
    payload = json.dumps(candidate.to_dict(), indent=2) + "\n"

    for _ in range(2):
        try:
            handle = os.open(
                paths.RUN_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
        except FileExistsError:
            existing = read()
            if existing is None:
                # Unreadable: treat as debris and try once more.
                _remove()
                continue
            if is_stale(existing, max_age_seconds):
                _remove()
                continue
            return None, existing
        except OSError:
            return None, read()
        else:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(payload)
            return candidate, None

    return None, read()


def release(token: str) -> bool:
    """Drop the lock, but only if ``token`` still holds it.

    The token check is what stops a run that already finished from deleting the
    lock a *later* run has since taken.
    """
    current = read()
    if current is None:
        return True
    if current.token != token:
        return False
    _remove()
    return True


def force_release() -> None:
    """Clear the lock regardless of who holds it.

    Only for a lock the operator has established is dead: it removes the file,
    it does not stop whatever process may still be running.
    """
    _remove()


def _remove() -> None:
    try:
        paths.RUN_LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass
