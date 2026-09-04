"""The Database page: what is in there, and a console for when you need one.

Reading order: is it reachable, what does it hold, what has the ETL been doing,
and then a place to ask it something directly.

The console defaults to read-only, enforced by PostgreSQL rather than by
inspecting the statement, and every statement carries a timeout. Writes and
maintenance are both opt-in in config, because anyone who can reach this page on
the LAN can reach this console.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app import paths
from app.data import database, db_console, postgres_loader
from app.settings import POSTGRES, Settings
from app.ui import components
from app.ui.theme import format_currency, md_escape
from app.views import widgets

RESULT_KEY = "db_console_result"
STATEMENT_KEY = "db_console_statement"

EXAMPLES = {
    "Rows per month": (
        "SELECT date_trunc('month', sale_date)::date AS month,\n"
        "       count(*) AS sales,\n"
        "       round(sum(premium)) AS premium\n"
        "FROM {table}\n"
        "GROUP BY 1 ORDER BY 1 DESC\n"
        "LIMIT 24"
    ),
    "Today's rows": (
        "SELECT sale_date, agent, category, channel, policy_id, premium\n"
        "FROM {table}\n"
        "WHERE sale_date = CURRENT_DATE\n"
        "ORDER BY agent"
    ),
    "Duplicate policy numbers": (
        "SELECT policy_id, count(*) AS rows\n"
        "FROM {table}\n"
        "WHERE policy_id <> ''\n"
        "GROUP BY 1 HAVING count(*) > 1\n"
        "ORDER BY 2 DESC"
    ),
    "Categories not mapped for reporting": (
        "SELECT category, count(*) AS rows, round(sum(premium)) AS premium\n"
        "FROM {table}\n"
        "GROUP BY 1 ORDER BY 2 DESC"
    ),
    "Most recent load": (
        "SELECT started_at, source_file, source_sheet, rows_inserted,\n"
        "       rows_updated, rows_skipped, status\n"
        "FROM etl_runs ORDER BY started_at DESC LIMIT 20"
    ),
}


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


def _connection(settings: Settings) -> bool:
    pg = settings.data.postgres
    components.section("Connection", pg.qualified_table())

    probe = database.check(pg)
    if probe.ok:
        st.success(probe.message)
    else:
        st.error(probe.message)

    pills = [pg.describe()]
    if probe.server_version:
        pills.append(probe.server_version)
    pills.append(
        f"Password from {pg.password_env}"
        if pg.password_env
        else "Password from libpq (~/.pgpass or PGPASSWORD)"
    )
    if pg.where:
        pills.append(f"Reporting filter: {pg.where}")
    components.meta_strip(pills)
    return probe.ok


def _contents(settings: Settings) -> None:
    pg = settings.data.postgres
    components.section("Contents", "what the database holds")

    summary = db_console.sales_summary(pg)
    if summary is None:
        st.warning(
            f"`{pg.qualified_table()}` does not exist, or could not be read. "
            "Apply `db/schema.sql`, or point `data.postgres.table` at the right "
            "table."
        )
    else:
        cards = [
            components.Card(label="Rows", value=f"{summary.rows:,}"),
            components.Card(
                label="Premium",
                value=format_currency(summary.premium, settings.app.currency_symbol),
            ),
            components.Card(
                label="Date range",
                value=(
                    f"{summary.first_date} → {summary.last_date}"
                    if summary.first_date
                    else "—"
                ),
            ),
            components.Card(
                label="Agents / categories",
                value=f"{summary.agents} / {summary.categories}",
            ),
        ]
        components.kpi_grid(cards)

    stats = db_console.table_stats(pg)
    if stats:
        widgets.dataframe(
            pd.DataFrame(
                [
                    {
                        "Table": s.name,
                        "Rows (estimate)": s.estimated_rows,
                        "Size": s.size_pretty,
                    }
                    for s in stats
                ]
            ),
            hide_index=True,
        )
        st.caption(
            "Row counts here are the planner's estimates, refreshed by ANALYZE. "
            "The figure above the table is an exact count."
        )


def _load_history(settings: Settings) -> None:
    pg = settings.data.postgres
    runs = postgres_loader.latest_etl_runs(pg, limit=20)
    components.section("Load history", f"{len(runs)} recent run(s)")

    if not runs:
        st.caption(
            "No load history yet. `tools/etl_excel_to_postgres.py` records every "
            "run in `etl_runs`; apply `db/schema.sql` if the table is missing."
        )
        return

    widgets.dataframe(
        pd.DataFrame(
            [
                {
                    "Started": r["started_at"].strftime("%Y-%m-%d %H:%M"),
                    "Workbook": r["source_file"] or "—",
                    "Sheet": r["source_sheet"] or "—",
                    "Read": r["rows_read"],
                    "Inserted": r["rows_inserted"],
                    "Updated": r["rows_updated"],
                    "Unchanged": r["rows_skipped"],
                    "Status": r["status"],
                    "Message": (r["message"] or "")[:120],
                }
                for r in runs
            ]
        ),
        hide_index=True,
    )


def _render_result(result: db_console.QueryResult, settings: Settings) -> None:
    if not result.ok:
        st.error(result.error or "The statement failed.")
        return

    mode = "read-only" if result.read_only else "WRITE"
    if result.affected is not None:
        st.success(
            f"{result.affected:,} row(s) affected in {result.duration_ms:,.0f} ms "
            f"({mode})."
        )
    else:
        note = " · capped" if result.truncated else ""
        st.success(
            f"{result.row_count:,} row(s) in {result.duration_ms:,.0f} ms "
            f"({mode}){note}."
        )

    for notice in result.notices[:10]:
        st.caption(f"notice: {notice}")

    if result.frame.empty:
        return

    widgets.dataframe(result.frame, hide_index=True)
    if result.truncated:
        st.caption(
            f"Showing the first {settings.data.postgres.console.row_limit:,} rows. "
            "Add a LIMIT, or raise `data.postgres.console.row_limit`."
        )
    st.download_button(
        "Download these rows (CSV)",
        data=result.frame.to_csv(index=False).encode("utf-8"),
        file_name="query_result.csv",
        mime="text/csv",
    )


def _console(settings: Settings) -> None:
    pg = settings.data.postgres
    console = pg.console
    components.section(
        "Console",
        "read-only" if not console.allow_writes else "WRITES ENABLED",
    )

    if not console.enabled:
        st.caption(
            "The console is off. Set `data.postgres.console.enabled: true` in "
            "config.yaml to switch it on."
        )
        return

    if console.allow_writes:
        st.warning(
            "**Writes are enabled for this console.** Anyone who can reach this "
            "page can change or delete data. Set "
            "`data.postgres.console.allow_writes: false` unless you are actively "
            "using it."
        )
    else:
        st.caption(
            "Statements run inside a PostgreSQL **read-only transaction**, so "
            "the engine itself refuses to write — however the statement is "
            f"phrased. Each is cancelled after "
            f"{console.statement_timeout_seconds}s, and results are capped at "
            f"{console.row_limit:,} rows."
        )

    example = st.selectbox(
        "Start from an example",
        ["— write my own —", *EXAMPLES],
        key="db_console_example",
    )
    if example in EXAMPLES and st.button("Use this example", key="db_console_use"):
        st.session_state[STATEMENT_KEY] = EXAMPLES[example].format(
            table=pg.qualified_table()
        )
        st.rerun()

    with st.form("db_console_form"):
        statement = st.text_area(
            "SQL",
            value=st.session_state.get(STATEMENT_KEY, ""),
            height=160,
            placeholder=f"SELECT * FROM {pg.qualified_table()} LIMIT 20",
            label_visibility="collapsed",
        )
        run_col, write_col = st.columns([2, 1])
        with run_col:
            submitted = st.form_submit_button("Run", type="primary")
        with write_col:
            write_mode = st.checkbox(
                "Write mode",
                value=False,
                disabled=not console.allow_writes,
                help=(
                    "Allow this statement to change data."
                    if console.allow_writes
                    else "Disabled: set data.postgres.console.allow_writes to enable."
                ),
            )

    if submitted:
        st.session_state[STATEMENT_KEY] = statement
        st.session_state[RESULT_KEY] = db_console.run_query(
            pg, statement, console, write_mode=write_mode
        )

    result = st.session_state.get(RESULT_KEY)
    if result is not None:
        _render_result(result, settings)


def _maintenance(settings: Settings) -> None:
    pg = settings.data.postgres
    console = pg.console
    components.section("Maintenance", "schema and housekeeping")

    if not console.allow_maintenance:
        st.caption(
            "Maintenance actions are off. Set "
            "`data.postgres.console.allow_maintenance: true` in config.yaml to "
            "allow applying the schema and running ANALYZE / VACUUM from here."
        )
        return

    st.caption(
        "These change the database. Applying the schema is idempotent — every "
        "statement in `db/schema.sql` is CREATE ... IF NOT EXISTS — so it is safe "
        "to run against an existing database."
    )

    schema_col, analyze_col, vacuum_col = st.columns(3)
    with schema_col:
        if st.button("Apply db/schema.sql", key="db_apply_schema"):
            st.session_state[RESULT_KEY] = db_console.apply_schema(
                pg, paths.PROJECT_ROOT / "db" / "schema.sql"
            )
            st.rerun()
    with analyze_col:
        if st.button("ANALYZE", key="db_analyze"):
            st.session_state[RESULT_KEY] = db_console.maintain(pg, "analyze")
            st.rerun()
    with vacuum_col:
        if st.button("VACUUM", key="db_vacuum"):
            st.session_state[RESULT_KEY] = db_console.maintain(pg, "vacuum")
            st.rerun()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def render(settings: Settings) -> None:
    components.masthead("Database", settings.app.organization)
    components.subhead("The reporting database: what is in it, and a way to ask it.")

    if settings.data.source_type != POSTGRES:
        components.empty_state(
            "Reading from Excel",
            "The dashboard is configured with `data.source_type: excel`, so there "
            "is no reporting database in use. This page becomes active once "
            "`source_type` is `postgres` — see `db/README.md` for the setup.",
        )
        st.caption(
            md_escape(
                "The ETL can still load into a database before you switch over: "
                "`python tools/etl_excel_to_postgres.py`."
            )
        )
        return

    if not database.PSYCOPG_AVAILABLE:
        st.error(database.DRIVER_HINT)
        return

    reachable = _connection(settings)
    if not reachable:
        st.info(
            "Everything below needs a working connection. Check the host, port "
            "and credentials in `config/config.yaml`, and that PostgreSQL is "
            "running."
        )
        return

    _contents(settings)
    _load_history(settings)
    _console(settings)
    _maintenance(settings)
