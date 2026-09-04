"""Connecting to the reporting database.

One place owns how a connection is made, so the dashboard, the ETL and the
diagnostics all fail the same way and report the same thing. Nothing here ever
puts the password in a message: a wrong password and an unreachable host must
produce different, useful text without either leaking the credential.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from app.settings import PostgresSettings

try:
    import psycopg

    PSYCOPG_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the install check
    psycopg = None  # type: ignore[assignment]
    PSYCOPG_AVAILABLE = False

DRIVER_HINT = (
    "The PostgreSQL driver is not installed. Run `pip install -r requirements.txt` "
    "(it provides psycopg[binary])."
)


class DatabaseError(RuntimeError):
    """A connection or query failure, already phrased for a human."""


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    message: str
    server_version: str | None = None


def _describe_failure(settings: PostgresSettings, exc: Exception) -> str:
    """Turn a driver exception into something an operator can act on."""
    text = str(exc).strip().splitlines()
    detail = text[0] if text else exc.__class__.__name__
    lowered = detail.lower()

    if "password authentication failed" in lowered or "no password supplied" in lowered:
        source = (
            f"the {settings.password_env} environment variable"
            if settings.password_env
            else "libpq (~/.pgpass or PGPASSWORD)"
        )
        return (
            f"Authentication failed for user '{settings.user}'. The password comes "
            f"from {source} — check it is set for the account running the app."
        )
    if "could not connect" in lowered or "connection refused" in lowered:
        return (
            f"Could not reach PostgreSQL at {settings.host}:{settings.port}. "
            "Check the server is running and the port is open."
        )
    if "does not exist" in lowered and "database" in lowered:
        return (
            f"Database '{settings.database}' does not exist on "
            f"{settings.host}:{settings.port}."
        )
    if "timeout" in lowered:
        return (
            f"Timed out after {settings.connect_timeout}s connecting to "
            f"{settings.host}:{settings.port}."
        )
    return detail


@contextmanager
def connect(settings: PostgresSettings) -> Iterator[Any]:
    """A connection to the reporting database, closed on the way out."""
    if not PSYCOPG_AVAILABLE:
        raise DatabaseError(DRIVER_HINT)

    try:
        connection = psycopg.connect(**settings.connection_kwargs())
    except Exception as exc:  # noqa: BLE001 - re-raised with a usable message
        raise DatabaseError(_describe_failure(settings, exc)) from exc

    try:
        yield connection
    finally:
        connection.close()


def check(settings: PostgresSettings) -> ConnectionCheck:
    """Probe the connection without raising. Used by Diagnostics and health."""
    if not PSYCOPG_AVAILABLE:
        return ConnectionCheck(False, DRIVER_HINT)
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version()")
                row = cursor.fetchone()
        version = str(row[0]).split(",")[0] if row else None
        return ConnectionCheck(True, f"Connected to {settings.describe()}", version)
    except DatabaseError as exc:
        return ConnectionCheck(False, str(exc))


def table_exists(connection: Any, schema: str, table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"{schema}.{table}",),
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def table_columns(connection: Any, schema: str, table: str) -> list[str]:
    """Column names of ``schema.table``, in ordinal order."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [str(row[0]) for row in cursor.fetchall()]
