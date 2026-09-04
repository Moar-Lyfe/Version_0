"""Operations against the reporting database: inspection, queries, maintenance.

The console runs statements a person typed, which deserves care. Two things
carry the safety, and neither is a regular expression over the SQL:

* **PostgreSQL enforces read-only.** Unless writes are explicitly enabled in
  config, every statement runs inside a ``READ ONLY`` transaction. The engine
  refuses to write, whatever the statement does -- through a CTE, a function, a
  trigger. Inspecting statement text is guesswork by comparison, and gets it
  wrong the first time somebody writes ``WITH x AS (DELETE ...)``.
* **Every statement has a timeout.** A missing WHERE clause on a large table
  should waste fifteen seconds, not lock a reporting database for an afternoon.

Results are capped as well, so a careless ``SELECT *`` returns a page rather
than everything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from psycopg import sql

from app.data import database
from app.settings import ConsoleSettings, PostgresSettings

# Statements that read but are not SELECT, so the UI describes them correctly.
_READ_PREFIXES = ("select", "with", "show", "explain", "table", "values")


@dataclass
class QueryResult:
    """Everything the page needs to render one execution."""

    ok: bool
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    row_count: int = 0
    truncated: bool = False
    affected: int | None = None
    duration_ms: float = 0.0
    notices: list[str] = field(default_factory=list)
    error: str | None = None
    read_only: bool = True

    @property
    def returned_rows(self) -> bool:
        return not self.frame.empty or self.row_count > 0


@dataclass(frozen=True)
class TableStat:
    name: str
    estimated_rows: int
    total_bytes: int
    size_pretty: str


def looks_like_a_read(statement: str) -> bool:
    """Whether the statement reads. Advisory only -- the engine is the guard."""
    stripped = statement.strip().lstrip("(").lower()
    return stripped.startswith(_READ_PREFIXES)


def run_query(
    pg: PostgresSettings,
    statement: str,
    console: ConsoleSettings,
    *,
    write_mode: bool = False,
) -> QueryResult:
    """Execute one statement and bring back a page of results."""
    statement = statement.strip().rstrip(";").strip()
    if not statement:
        return QueryResult(ok=False, error="Nothing to run.")

    read_only = not (write_mode and console.allow_writes)
    notices: list[str] = []
    started = time.perf_counter()

    try:
        with database.connect(pg) as connection:
            connection.autocommit = False
            # Set on the connection, not with SET TRANSACTION: the latter must
            # be the first statement in its transaction, which the timeout
            # below would already have consumed.
            connection.read_only = read_only
            connection.add_notice_handler(
                lambda diag: notices.append(str(diag.message_primary))
            )
            with connection.cursor() as cursor:
                # A statement that runs away should cost seconds, not an
                # afternoon of someone else's reporting.
                cursor.execute(
                    sql.SQL("SET LOCAL statement_timeout = {}").format(
                        sql.Literal(f"{console.statement_timeout_seconds}s")
                    )
                )
                cursor.execute(statement)
                duration = (time.perf_counter() - started) * 1000

                if cursor.description is None:
                    affected = cursor.rowcount
                    if read_only:
                        connection.rollback()
                    else:
                        connection.commit()
                    return QueryResult(
                        ok=True,
                        affected=affected,
                        duration_ms=duration,
                        notices=list(notices),
                        read_only=read_only,
                    )

                columns = [str(d.name) for d in cursor.description]
                rows = cursor.fetchmany(console.row_limit + 1)
                truncated = len(rows) > console.row_limit
                rows = rows[: console.row_limit]

            # A read has nothing to keep; a write the operator enabled does.
            if read_only:
                connection.rollback()
            else:
                connection.commit()

    except database.DatabaseError as exc:
        return QueryResult(ok=False, error=str(exc), read_only=read_only)
    except Exception as exc:  # noqa: BLE001 - the whole point is to show it
        message = str(exc).strip() or exc.__class__.__name__
        if "statement timeout" in message.lower():
            message = (
                f"Cancelled after {console.statement_timeout_seconds}s. Add a "
                "WHERE clause or a LIMIT, or raise "
                "data.postgres.console.statement_timeout_seconds."
            )
        return QueryResult(ok=False, error=message, read_only=read_only)

    frame = pd.DataFrame(rows, columns=columns)
    return QueryResult(
        ok=True,
        frame=frame,
        row_count=len(frame),
        truncated=truncated,
        duration_ms=duration,
        notices=notices,
        read_only=read_only,
    )


def table_stats(pg: PostgresSettings) -> list[TableStat]:
    """Every table in the configured schema, with size and a row estimate."""
    query = """
        SELECT relname,
               n_live_tup,
               pg_total_relation_size(relid) AS total_bytes,
               pg_size_pretty(pg_total_relation_size(relid)) AS pretty
        FROM pg_stat_user_tables
        WHERE schemaname = %s
        ORDER BY pg_total_relation_size(relid) DESC
    """
    try:
        with database.connect(pg) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (pg.db_schema,))
                return [
                    TableStat(str(r[0]), int(r[1] or 0), int(r[2] or 0), str(r[3]))
                    for r in cursor.fetchall()
                ]
    except database.DatabaseError:
        return []


@dataclass(frozen=True)
class SalesSummary:
    rows: int
    first_date: object | None
    last_date: object | None
    agents: int
    categories: int
    premium: float


def sales_summary(pg: PostgresSettings) -> SalesSummary | None:
    """Exact figures for the reporting table, not the planner's estimate."""
    date_column = pg.column("date") or "sale_date"
    agent_column = pg.column("agent")
    category_column = pg.column("category")
    premium_column = pg.column("premium")

    projections = [
        sql.SQL("count(*)"),
        sql.SQL("min({})").format(sql.Identifier(date_column)),
        sql.SQL("max({})").format(sql.Identifier(date_column)),
        sql.SQL("count(DISTINCT {})").format(sql.Identifier(agent_column))
        if agent_column
        else sql.SQL("0"),
        sql.SQL("count(DISTINCT {})").format(sql.Identifier(category_column))
        if category_column
        else sql.SQL("0"),
        sql.SQL("coalesce(sum({}), 0)").format(sql.Identifier(premium_column))
        if premium_column
        else sql.SQL("0"),
    ]
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(projections),
        sql.Identifier(pg.db_schema, pg.table),
    )
    try:
        with database.connect(pg) as connection:
            if not database.table_exists(connection, pg.db_schema, pg.table):
                return None
            with connection.cursor() as cursor:
                cursor.execute(query)
                row = cursor.fetchone()
    except database.DatabaseError:
        return None
    if row is None:
        return None
    return SalesSummary(
        rows=int(row[0]),
        first_date=row[1],
        last_date=row[2],
        agents=int(row[3]),
        categories=int(row[4]),
        premium=float(row[5]),
    )


MAINTENANCE = {
    "analyze": ("ANALYZE", "Refresh the planner's statistics."),
    "vacuum": ("VACUUM (ANALYZE)", "Reclaim dead rows and refresh statistics."),
}


def maintain(pg: PostgresSettings, action: str) -> QueryResult:
    """Run a maintenance command against the reporting table."""
    if action not in MAINTENANCE:
        return QueryResult(ok=False, error=f"Unknown maintenance action {action!r}.")

    command, _ = MAINTENANCE[action]
    started = time.perf_counter()
    try:
        notices: list[str] = []
        with database.connect(pg) as connection:
            # VACUUM cannot run inside a transaction block.
            connection.autocommit = True
            connection.add_notice_handler(
                lambda diag: notices.append(str(diag.message_primary))
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("{} {}").format(
                        sql.SQL(command), sql.Identifier(pg.db_schema, pg.table)
                    )
                )
    except database.DatabaseError as exc:
        return QueryResult(ok=False, error=str(exc), read_only=False)
    except Exception as exc:  # noqa: BLE001
        return QueryResult(ok=False, error=str(exc), read_only=False)

    return QueryResult(
        ok=True,
        duration_ms=(time.perf_counter() - started) * 1000,
        notices=notices,
        read_only=False,
    )


def apply_schema(pg: PostgresSettings, path: Path) -> QueryResult:
    """Apply db/schema.sql. Every statement in it is idempotent."""
    try:
        script = path.read_text(encoding="utf-8")
    except OSError as exc:
        return QueryResult(ok=False, error=f"Could not read {path}: {exc}")

    started = time.perf_counter()
    notices: list[str] = []
    try:
        with database.connect(pg) as connection:
            connection.add_notice_handler(
                lambda diag: notices.append(str(diag.message_primary))
            )
            with connection.cursor() as cursor:
                cursor.execute(script)
            connection.commit()
    except database.DatabaseError as exc:
        return QueryResult(ok=False, error=str(exc), read_only=False)
    except Exception as exc:  # noqa: BLE001
        return QueryResult(ok=False, error=str(exc), read_only=False)

    return QueryResult(
        ok=True,
        duration_ms=(time.perf_counter() - started) * 1000,
        notices=notices,
        read_only=False,
    )
