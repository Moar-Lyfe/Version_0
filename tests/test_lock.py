"""The pipeline run lock: one run at a time, and no lock nobody can clear."""

import json
import os
import socket
from datetime import datetime, timedelta

import pytest

from app import paths
from app.admin import lock


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(paths, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(paths, "RUN_LOCK_FILE", runtime / "pipeline.lock")
    monkeypatch.setattr(paths, "RUN_LOG_DIR", runtime / "logs")


def write_lock(**overrides) -> dict:
    payload = {
        "token": "abc123",
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "owner": "Admin panel",
        "started_at": datetime.now().astimezone().isoformat(),
    }
    payload.update(overrides)
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    paths.RUN_LOCK_FILE.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_first_caller_wins_and_the_second_is_told_who_holds_it():
    first, holder = lock.acquire("Admin panel")
    assert first is not None and holder is None

    second, holder = lock.acquire("terminal")
    assert second is None
    assert holder is not None
    assert holder.owner == "Admin panel"
    assert "Admin panel" in holder.describe()


def test_releasing_frees_it_for_the_next_run():
    first, _ = lock.acquire("Admin panel")
    assert lock.release(first.token) is True
    assert not paths.RUN_LOCK_FILE.exists()

    second, holder = lock.acquire("terminal")
    assert second is not None and holder is None


def test_a_stale_token_cannot_release_someone_elses_lock():
    """The guard against a finished run deleting a later run's lock."""
    first, _ = lock.acquire("run one")
    lock.release(first.token)
    second, _ = lock.acquire("run two")

    assert lock.release(first.token) is False
    assert lock.read().token == second.token


def test_releasing_an_absent_lock_is_not_an_error():
    assert lock.release("anything") is True


def test_a_dead_holder_on_this_machine_is_stale():
    # PID 1 exists; a very high pid almost certainly does not.
    write_lock(pid=999_999)
    holder = lock.read()
    if os.name == "posix":
        assert lock.is_stale(holder, 3600) is True
        # ...and the next caller simply takes it.
        acquired, blocked = lock.acquire("terminal")
        assert acquired is not None and blocked is None


def test_a_live_holder_is_not_stale():
    write_lock(pid=os.getpid())
    assert lock.is_stale(lock.read(), 3600) is False


def test_an_old_lock_expires_even_when_the_pid_is_unknown():
    """Covers the machine that lost power, and Windows where pids cannot be probed."""
    old = datetime.now().astimezone() - timedelta(seconds=3600 + lock.STALE_GRACE_SECONDS + 60)
    write_lock(host="some-other-machine", pid=4242, started_at=old.isoformat())
    assert lock.is_stale(lock.read(), 3600) is True


def test_a_recent_lock_from_another_machine_is_respected():
    """A pid means nothing off-host, so age is the only safe test there."""
    write_lock(host="some-other-machine", pid=4242)
    holder = lock.read()
    assert lock.is_stale(holder, 3600) is False
    acquired, blocked = lock.acquire("terminal")
    assert acquired is None
    assert blocked.host == "some-other-machine"


def test_corrupt_lock_files_are_treated_as_debris():
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    paths.RUN_LOCK_FILE.write_text("{ not json", encoding="utf-8")
    assert lock.read() is None

    acquired, blocked = lock.acquire("terminal")
    assert acquired is not None and blocked is None


def test_a_lock_missing_fields_is_treated_as_debris():
    paths.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    paths.RUN_LOCK_FILE.write_text(json.dumps({"owner": "half a lock"}), encoding="utf-8")
    assert lock.read() is None


def test_force_release_clears_any_lock():
    write_lock(host="some-other-machine", pid=4242)
    lock.force_release()
    assert lock.read() is None


def test_ownership_is_host_and_pid():
    acquired, _ = lock.acquire("Admin panel")
    assert acquired.is_ours is True
    lock.release(acquired.token)

    write_lock(host="elsewhere")
    assert lock.read().is_ours is False
