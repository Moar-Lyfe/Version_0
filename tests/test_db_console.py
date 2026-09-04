"""The database console's guard rails.

The point of these is the negative cases: the console must refuse to write when
it is not supposed to, however the statement is phrased. That is enforced by
PostgreSQL, so most of this needs a real server -- offered via
``EXEC_DASH_TEST_DSN`` and skipped when absent.
"""

import dataclasses
import os

import pytest

from app.data import db_console
from app.settings import ConsoleSettings, PostgresSettings

DSN = os.environ.get("EXEC_DASH_TEST_DSN")
requires_db = pytest.mark.skipif(
    not DSN, reason="set EXEC_DASH_TEST_DSN to exercise the console"
)


@pytest.fixture
def console() -> ConsoleSettings:
    return ConsoleSettings(row_limit=10, statement_timeout_seconds=5)


@pytest.fixture
def pg() -> PostgresSettings:
    host, port, dbname, user = DSN.split(":")
    return PostgresSettings(
        host=host, port=int(port), database=dbname, user=user, password_env=None
    )


# --------------------------------------------------------------------------- #
# No database needed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "statement, expected",
    [
        ("SELECT 1", True),
        ("  select * from sales", True),
        ("WITH x AS (SELECT 1) SELECT * FROM x", True),
        ("EXPLAIN SELECT 1", True),
        ("SHOW timezone", True),
        ("DELETE FROM sales", False),
        ("UPDATE sales SET premium = 0", False),
        ("DROP TABLE sales", False),
    ],
)
def test_read_detection_is_advisory(statement, expected):
    """Used only for wording. The engine is what actually enforces read-only."""
    assert db_console.looks_like_a_read(statement) is expected


def test_an_empty_statement_is_refused(pg_settings=None):
    result = db_console.run_query(
        PostgresSettings(), "   ", ConsoleSettings()
    )
    assert result.ok is False
    assert "Nothing to run" in result.error


def test_an_unreachable_server_is_reported_not_raised():
    result = db_console.run_query(
        PostgresSettings(port=1, connect_timeout=2, password_env=None),
        "SELECT 1",
        ConsoleSettings(),
    )
    assert result.ok is False
    assert "Could not reach PostgreSQL" in result.error


def test_unknown_maintenance_is_refused():
    result = db_console.maintain(PostgresSettings(), "rm -rf")
    assert result.ok is False
    assert "Unknown maintenance action" in result.error


# --------------------------------------------------------------------------- #
# Against a real server
# --------------------------------------------------------------------------- #


@requires_db
def test_a_read_returns_rows(pg, console):
    result = db_console.run_query(pg, "SELECT 1 AS x, 'a' AS y", console)
    assert result.ok
    assert result.read_only is True
    assert list(result.frame.columns) == ["x", "y"]
    assert result.row_count == 1


@requires_db
@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM sales",
        "UPDATE sales SET premium = 0",
        "DROP TABLE sales",
        "CREATE TABLE console_should_not_exist (id int)",
        "TRUNCATE sales",
        # The case a text check gets wrong: a write hidden inside a CTE that
        # starts with the word SELECT.
        "WITH gone AS (DELETE FROM sales RETURNING id) SELECT count(*) FROM gone",
    ],
)
def test_writes_are_refused_by_the_engine(pg, console, statement):
    result = db_console.run_query(pg, statement, console)
    assert result.ok is False
    assert "read-only transaction" in result.error


@requires_db
def test_write_mode_does_nothing_unless_config_allows_it(pg, console):
    """Ticking the box is not enough; config has to permit writes at all."""
    result = db_console.run_query(
        pg, "CREATE TEMP TABLE nope (id int)", console, write_mode=True
    )
    assert result.ok is False
    assert "read-only transaction" in result.error


@requires_db
def test_writes_work_when_explicitly_enabled(pg):
    permissive = ConsoleSettings(allow_writes=True, row_limit=10)
    result = db_console.run_query(
        pg, "CREATE TEMP TABLE console_ok (id int)", permissive, write_mode=True
    )
    assert result.ok is True
    assert result.read_only is False


@requires_db
def test_results_are_capped(pg, console):
    result = db_console.run_query(
        pg, "SELECT generate_series(1, 100) AS n", console
    )
    assert result.ok
    assert result.row_count == console.row_limit
    assert result.truncated is True


@requires_db
def test_a_runaway_statement_is_cancelled(pg):
    tight = ConsoleSettings(statement_timeout_seconds=1)
    result = db_console.run_query(pg, "SELECT pg_sleep(5)", tight)
    assert result.ok is False
    assert "Cancelled after 1s" in result.error


@requires_db
def test_a_syntax_error_comes_back_as_a_message(pg, console):
    result = db_console.run_query(pg, "SELCT 1", console)
    assert result.ok is False
    assert result.error


@requires_db
def test_table_stats_list_the_schema(pg):
    stats = db_console.table_stats(pg)
    assert any(s.name == "sales" for s in stats)
    assert all(s.total_bytes >= 0 for s in stats)


@requires_db
def test_the_sales_summary_is_exact(pg):
    summary = db_console.sales_summary(pg)
    assert summary is not None
    assert summary.rows > 0
    assert summary.first_date is not None and summary.last_date is not None

    counted = db_console.run_query(
        pg, f"SELECT count(*) AS n FROM {pg.qualified_table()}", ConsoleSettings()
    )
    assert int(counted.frame["n"].iloc[0]) == summary.rows


@requires_db
def test_a_missing_table_summarises_as_none(pg):
    absent = dataclasses.replace(pg, table="definitely_not_here")
    assert db_console.sales_summary(absent) is None
