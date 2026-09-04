"""Read the canonical dataset out of the reporting database.

Produces exactly the same :class:`LoadResult` the Excel reader does -- same
columns, same derived fields, same warnings shape -- so everything downstream
(KPIs, projections, moving averages, alerts) is unaware of where the rows came
from. Switching ``data.source_type`` is the whole migration.

Column names are mapped rather than assumed, so an existing ``sales`` table does
not have to be renamed to be usable. Only ``date`` is required; anything else
falls back to the same defaults the workbook reader uses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from psycopg import sql

from app.data import database
from app.data import schema as S
from app.data.excel_loader import FileReport, LoadResult
from app.settings import DataSettings, PostgresSettings

# Canonical fields we ask the database for, in the order they are selected.
SELECTED = ("date", "premium", "agent", "category", "channel", "policy_id", "count")


def _identifier(name: str) -> sql.Identifier:
    """Quote a possibly dotted identifier, so `public.sales` composes safely."""
    return sql.Identifier(*name.split("."))


def build_query(
    settings: PostgresSettings, extra_columns: list[str] | None = None
) -> tuple[sql.Composed, dict[str, str]]:
    """The SELECT, plus the canonical -> column map it resolved.

    Composed with ``psycopg.sql`` rather than string formatting, so a table or
    column name coming out of ``config.yaml`` is quoted as an identifier and
    cannot be injected. ``extra_columns`` are provenance columns selected under
    their own names when the table happens to have them.
    """
    resolved: dict[str, str] = {}
    projections: list[sql.Composed] = []

    for canonical in SELECTED:
        column = settings.column(canonical)
        if column:
            resolved[canonical] = column
            projections.append(
                sql.SQL("{} AS {}").format(
                    sql.Identifier(column), sql.Identifier(canonical)
                )
            )
        else:
            # Absent from this table: select a NULL so every result set has the
            # same shape and the reader needs no branching.
            projections.append(sql.SQL("NULL AS {}").format(sql.Identifier(canonical)))

    for name in extra_columns or []:
        projections.append(sql.Identifier(name))

    query = sql.SQL("SELECT {fields} FROM {table}").format(
        fields=sql.SQL(", ").join(projections),
        table=_identifier(settings.qualified_table()),
    )
    if settings.where.strip():
        # Deliberately raw: `where` is a config-authored SQL predicate, which is
        # the point of the setting. It is trusted exactly as much as the rest of
        # config.yaml, and never built from user input at runtime.
        query = sql.SQL("{} WHERE {}").format(query, sql.SQL(settings.where))
    return query, resolved


def _optional_columns(available: list[str], settings: PostgresSettings) -> list[str]:
    wanted = ("source_file", "source_sheet", "source_row")
    lowered = {name.lower() for name in available}
    return [name for name in wanted if name in lowered]


def _to_frame(
    rows: list[tuple],
    field_names: list[str],
    settings: DataSettings,
    warnings: list[str],
) -> tuple[pd.DataFrame, int]:
    """Turn result rows into the canonical frame. Returns (frame, dropped)."""
    if not rows:
        return S.empty_frame(), 0

    raw = pd.DataFrame(rows, columns=field_names)

    out = pd.DataFrame(index=raw.index)
    out[S.DATE] = pd.to_datetime(raw["date"], errors="coerce").dt.normalize()
    out[S.PREMIUM] = [S.to_number(v) for v in raw["premium"]]

    counts = [S.to_number(v) for v in raw["count"]]
    # A NULL or zero count still means one sale, matching the workbook reader.
    out[S.SALES] = [c if c else 1.0 for c in counts]

    out[S.AGENT] = [S.clean_text(v, S.UNASSIGNED_AGENT) for v in raw["agent"]]
    out[S.CATEGORY] = [S.clean_text(v, "") for v in raw["category"]]
    out[S.CHANNEL] = [S.clean_text(v, "") for v in raw["channel"]]
    out[S.POLICY_ID] = [S.clean_text(v, "") for v in raw["policy_id"]]

    out[S.SOURCE_FILE] = (
        [S.clean_text(v, "database") for v in raw["source_file"]]
        if "source_file" in raw.columns
        else "database"
    )
    out[S.SOURCE_SHEET] = (
        [S.clean_text(v, "") for v in raw["source_sheet"]]
        if "source_sheet" in raw.columns
        else ""
    )
    out[S.SOURCE_ROW] = (
        [int(S.to_number(v)) for v in raw["source_row"]]
        if "source_row" in raw.columns
        else 0
    )

    dropped = int(out[S.DATE].isna().sum())
    if dropped:
        warnings.append(
            f"{dropped} row(s) skipped -- the date column held no readable date."
        )
    out = out.loc[out[S.DATE].notna()]

    return S.derive(out, settings), dropped


def load_dataset(settings: DataSettings) -> LoadResult:
    """Read every reportable row from the database."""
    loaded_at = datetime.now().astimezone()
    pg = settings.postgres
    warnings: list[str] = []
    report = FileReport(path=pg.describe(), sheet=pg.qualified_table())

    try:
        with database.connect(pg) as connection:
            if not database.table_exists(connection, pg.db_schema, pg.table):
                report.error = (
                    f"Table {pg.qualified_table()} does not exist. Apply "
                    "db/schema.sql, or point data.postgres.table at the right one."
                )
                return LoadResult(
                    frame=S.empty_frame(),
                    loaded_at=loaded_at,
                    files=[report],
                    warnings=[report.error],
                )

            available = database.table_columns(connection, pg.db_schema, pg.table)
            report.headers = tuple(available)

            query, resolved = build_query(pg)
            report.matched_columns = dict(resolved)
            report.missing_columns = tuple(c for c in SELECTED if c not in resolved)

            missing_in_table = {
                canonical: column
                for canonical, column in resolved.items()
                if column.lower() not in {name.lower() for name in available}
            }
            if missing_in_table:
                listed = ", ".join(f"{k} -> {v}" for k, v in missing_in_table.items())
                report.error = (
                    f"Configured column(s) not present in {pg.qualified_table()}: "
                    f"{listed}. Available: {', '.join(available)}"
                )
                return LoadResult(
                    frame=S.empty_frame(),
                    loaded_at=loaded_at,
                    files=[report],
                    warnings=[report.error],
                )

            # Provenance columns come along when the table carries them.
            query, _ = build_query(pg, _optional_columns(available, pg))

            with connection.cursor() as cursor:
                cursor.execute(query)
                field_names = [str(d.name) for d in cursor.description]
                rows = cursor.fetchall()

    except database.DatabaseError as exc:
        report.error = str(exc)
        return LoadResult(
            frame=S.empty_frame(),
            loaded_at=loaded_at,
            files=[report],
            warnings=[str(exc)],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced on Diagnostics
        report.error = f"{type(exc).__name__}: {exc}"
        return LoadResult(
            frame=S.empty_frame(),
            loaded_at=loaded_at,
            files=[report],
            warnings=[report.error],
        )

    frame, dropped = _to_frame(rows, field_names, settings, warnings)
    report.rows_read = len(rows)
    report.rows_without_date = dropped
    report.rows_kept = int(len(frame))
    report.modified_at = loaded_at

    if not frame.empty:
        frame = frame.sort_values(S.DATE, kind="stable").reset_index(drop=True)

    unmatched: dict[str, int] = {}
    if not frame.empty:
        other = frame.loc[frame[S.CATEGORY_KEY] == S.CATEGORY_OTHER, S.CATEGORY]
        counts = other.loc[other.astype(str).str.len() > 0].value_counts()
        unmatched = {str(k): int(v) for k, v in counts.items()}

    return LoadResult(
        frame=frame,
        loaded_at=loaded_at,
        files=[report],
        warnings=warnings,
        unmatched_categories=unmatched,
    )


def latest_etl_runs(settings: PostgresSettings, limit: int = 10) -> list[dict[str, Any]]:
    """Recent rows from ``etl_runs``, empty when the table is absent."""
    try:
        with database.connect(settings) as connection:
            if not database.table_exists(connection, settings.db_schema, "etl_runs"):
                return []
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "SELECT started_at, finished_at, source_file, source_sheet, "
                        "rows_read, rows_inserted, rows_updated, rows_skipped, "
                        "status, message FROM {} ORDER BY started_at DESC LIMIT %s"
                    ).format(_identifier(f"{settings.db_schema}.etl_runs")),
                    (limit,),
                )
                names = [str(d.name) for d in cursor.description]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
    except database.DatabaseError:
        return []
