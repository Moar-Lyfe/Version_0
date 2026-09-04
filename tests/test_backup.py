"""Backups: written, verified, rotated -- and actually restorable."""

import os
import subprocess
from pathlib import Path

import pytest

from app.data import backup
from app.settings import PostgresSettings

DSN = os.environ.get("EXEC_DASH_TEST_DSN")
requires_db = pytest.mark.skipif(
    not DSN, reason="set EXEC_DASH_TEST_DSN to exercise pg_dump"
)


@pytest.fixture
def pg() -> PostgresSettings:
    host, port, dbname, user = DSN.split(":")
    return PostgresSettings(
        host=host, port=int(port), database=dbname, user=user, password_env=None
    )


# --------------------------------------------------------------------------- #
# No database needed
# --------------------------------------------------------------------------- #


def test_the_tools_are_found_or_reported():
    """Either pg_dump is locatable, or its absence must be sayable."""
    found = backup.find_tool("pg_dump")
    assert found is None or Path(found).exists()


def test_a_missing_tool_is_a_clear_message(monkeypatch, tmp_path):
    monkeypatch.setattr(backup, "find_tool", lambda name: None)
    result = backup.run(PostgresSettings(), tmp_path)
    assert result.ok is False
    assert "pg_dump is not installed" in result.error


def test_rotation_keeps_the_newest(tmp_path):
    import time

    for index in range(5):
        (tmp_path / f"db_{index}.dump").write_bytes(b"x")
        time.sleep(0.01)

    removed = backup.rotate(tmp_path, keep=2)
    remaining = sorted(p.name for p in tmp_path.glob("*.dump"))
    assert len(remaining) == 2
    assert remaining == ["db_3.dump", "db_4.dump"]
    assert len(removed) == 3


def test_rotation_with_keep_zero_does_nothing(tmp_path):
    (tmp_path / "db.dump").write_bytes(b"x")
    assert backup.rotate(tmp_path, keep=0) == []
    assert list(tmp_path.glob("*.dump"))


def test_listing_an_absent_directory_is_empty(tmp_path):
    assert backup.existing(tmp_path / "nope") == []


def test_a_corrupt_dump_fails_verification(tmp_path):
    if backup.find_tool("pg_restore") is None:
        pytest.skip("pg_restore is not installed")
    bad = tmp_path / "truncated.dump"
    bad.write_bytes(b"PGDMP\x00 truncated nonsense")
    ok, objects, error = backup.verify(bad)
    assert ok is False
    assert error


def test_size_is_reported_readably():
    result = backup.BackupResult(size_bytes=1536)
    assert result.size_pretty == "1.5 KB"


# --------------------------------------------------------------------------- #
# Against a real server
# --------------------------------------------------------------------------- #


@requires_db
def test_a_backup_is_written_and_verified(pg, tmp_path):
    result = backup.run(pg, tmp_path, keep=5)
    assert result.ok is True
    assert result.error is None
    assert result.path.exists()
    assert result.size_bytes > 0
    # Verification is the point: a dump nobody read is not a backup.
    assert result.verified is True
    assert result.object_count > 0


@requires_db
def test_successive_backups_rotate(pg, tmp_path):
    for _ in range(3):
        backup.run(pg, tmp_path, keep=2)
    assert len(list(tmp_path.glob("*.dump"))) == 2


@requires_db
def test_a_backup_actually_restores(pg, tmp_path):
    """The only test that proves the backup is worth anything."""
    import psycopg

    result = backup.run(pg, tmp_path, keep=5)
    assert result.ok

    scratch = "backup_restore_test"
    admin = PostgresSettings(**{**pg.__dict__, "database": "postgres"})
    with psycopg.connect(**admin.connection_kwargs(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {scratch}")
            cursor.execute(f"CREATE DATABASE {scratch}")

    restore = backup.find_tool("pg_restore")
    completed = subprocess.run(
        [
            restore,
            f"--host={pg.host}",
            f"--port={pg.port}",
            f"--username={pg.user}",
            f"--dbname={scratch}",
            "--no-owner",
            str(result.path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr

    restored = PostgresSettings(**{**pg.__dict__, "database": scratch})
    with psycopg.connect(**restored.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*), coalesce(sum(premium), 0) FROM sales")
            restored_rows, restored_total = cursor.fetchone()
    with psycopg.connect(**pg.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*), coalesce(sum(premium), 0) FROM sales")
            original_rows, original_total = cursor.fetchone()

    assert restored_rows == original_rows
    assert restored_total == original_total

    with psycopg.connect(**admin.connection_kwargs(), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {scratch}")


@requires_db
def test_an_unreachable_server_leaves_no_partial_file(tmp_path):
    unreachable = PostgresSettings(port=1, password_env=None, connect_timeout=2)
    result = backup.run(unreachable, tmp_path)
    assert result.ok is False
    # A partial file is worse than none: it looks like a backup.
    assert list(tmp_path.glob("*.dump")) == []
