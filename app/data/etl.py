"""Excel -> CSV -> Postgres, as a library.

Excel stays the operational front end. This module moves an export into the
reporting database in three separable stages, so a failure is always
attributable to one of them:

1. **Read** the workbook and the named sheet, normalised with exactly the same
   column mapping and cell parsing the dashboard applies -- currency text,
   accounting negatives, blank agents. One mapping, one set of rules, whichever
   end of the pipeline you are standing at.
2. **Stage** the rows as a CSV in an archive directory. That file is the record
   of what was handed to the database, and re-loading it later needs no Excel.
3. **Load** by COPYing the CSV into a temporary table and merging into `sales`.

The merge is keyed on a natural key built from the source row -- the policy
number when the export has one -- which is what makes the load **idempotent**:
running the same workbook twice adds nothing, and a corrected row updates rather
than arriving as a second sale.

``tools/etl_excel_to_postgres.py`` is the command-line front end to this.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from psycopg import sql

from app.data import database
from app.data import schema as S
from app.data.excel_loader import resolve_columns
from app.settings import DataSettings, PostgresSettings

# Columns written to the CSV and staged, in order.
STAGE_COLUMNS = (
    "source_key",
    "sale_date",
    "agent",
    "category",
    "channel",
    "policy_id",
    "premium",
    "units",
    "source_file",
    "source_sheet",
    "source_row",
)

# Fields composing the natural key when a policy number is unavailable.
FALLBACK_KEY_FIELDS = ("sale_date", "agent", "category", "premium", "source_row")


@dataclass
class Outcome:
    rows_read: int = 0
    rows_staged: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    csv_path: Path | None = None
    problems: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1. Read
# --------------------------------------------------------------------------- #


def read_sheet(path: Path, sheet: str | int | None, header_row: int) -> pd.DataFrame:
    """Read one named sheet, resolving an index or 'first sheet' to a real name."""
    with pd.ExcelFile(path) as book:
        names = list(book.sheet_names)
        if not names:
            raise ValueError(f"{path.name} has no sheets.")

        if sheet is None or (isinstance(sheet, str) and not str(sheet).strip()):
            target = names[0]
        elif isinstance(sheet, int) or (
            isinstance(sheet, str) and str(sheet).strip().lstrip("-").isdigit()
        ):
            index = int(sheet)
            if not -len(names) <= index < len(names):
                raise ValueError(
                    f"Sheet index {index} is out of range; {path.name} has "
                    f"{len(names)}: {', '.join(names)}"
                )
            target = names[index]
        else:
            if sheet not in names:
                raise ValueError(
                    f"Sheet {sheet!r} not found in {path.name}. "
                    f"Available: {', '.join(names)}"
                )
            target = str(sheet)

        frame = book.parse(target, header=header_row, dtype=object)
    frame.attrs["sheet_name"] = target
    return frame


def natural_key(row: dict, key_fields: tuple[str, ...]) -> str:
    """A stable identity for a source row.

    A policy number is the right key when the export has one: a corrected row
    keeps its number, so the correction updates rather than arriving as a second
    sale. Without one, a hash of the row's content is the best available -- with
    the caveat that editing such a row makes it look new, which is why the
    policy number is strongly preferred.
    """
    parts = []
    for name in key_fields:
        value = row.get(name, "")
        if isinstance(value, float):
            value = f"{value:.4f}"
        parts.append(S.normalise_key(value))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


def normalise(
    raw: pd.DataFrame,
    settings: DataSettings,
    source_file: Path,
    sheet_name: str,
    header_row: int,
    outcome: Outcome,
) -> list[dict]:
    """Workbook rows -> staging rows, using the dashboard's own column mapping."""
    raw = raw.dropna(how="all")
    columns = resolve_columns(list(raw.columns), settings.columns)

    if "date" not in columns:
        raise ValueError(
            "No date column found. Add the workbook's date header to "
            "data.columns.date in config.yaml. Headers seen: "
            + ", ".join(str(c) for c in raw.columns)
        )

    dates = pd.to_datetime(raw[columns["date"]], errors="coerce")

    def column_values(canonical: str, default=None):
        if canonical in columns:
            return list(raw[columns[canonical]])
        return [default] * len(raw)

    premiums = column_values("premium", 0)
    agents = column_values("agent", "")
    categories = column_values("category", "")
    channels = column_values("channel", "")
    policies = column_values("policy_id", "")
    counts = column_values("count", None)

    has_policy = "policy_id" in columns
    rows: list[dict] = []
    undated = 0

    for position, index in enumerate(raw.index):
        when = dates.iloc[position]
        outcome.rows_read += 1
        if pd.isna(when):
            undated += 1
            continue

        units = S.to_number(counts[position]) if counts[position] is not None else 0.0
        policy = S.clean_text(policies[position], "")

        row = {
            "sale_date": when.date().isoformat(),
            "agent": S.clean_text(agents[position], S.UNASSIGNED_AGENT),
            "category": S.clean_text(categories[position], ""),
            "channel": S.clean_text(channels[position], ""),
            "policy_id": policy,
            "premium": f"{S.to_number(premiums[position]):.2f}",
            "units": f"{(units or 1.0):.3f}",
            "source_file": source_file.name,
            "source_sheet": sheet_name,
            # +header_row+2 gives the row number a human sees in Excel.
            "source_row": int(index) + header_row + 2,
        }
        key_fields = ("policy_id",) if (has_policy and policy) else FALLBACK_KEY_FIELDS
        row["source_key"] = natural_key(row, key_fields)
        rows.append(row)

    if undated:
        outcome.problems.append(
            f"{undated} row(s) skipped -- the date cell held no readable date."
        )
    if not has_policy:
        outcome.problems.append(
            "No policy-number column mapped, so rows are keyed by content hash. "
            "An edited row will load as a new one; map data.columns.policy_id to "
            "avoid that."
        )

    # A workbook that repeats a policy number would break the merge with
    # "ON CONFLICT DO UPDATE command cannot affect row a second time".
    seen: dict[str, int] = {}
    deduped: list[dict] = []
    for row in rows:
        key = row["source_key"]
        if key in seen:
            seen[key] += 1
            continue
        seen[key] = 1
        deduped.append(row)
    repeats = sum(count - 1 for count in seen.values() if count > 1)
    if repeats:
        outcome.problems.append(
            f"{repeats} row(s) repeated a key already seen in this sheet; the "
            "first of each was kept."
        )
    return deduped


# --------------------------------------------------------------------------- #
# 2. Stage
# --------------------------------------------------------------------------- #


def write_csv(rows: list[dict], directory: Path, source: Path, sheet: str) -> Path:
    """Write the staging CSV -- the record of what was handed to the database."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_sheet = "".join(c if c.isalnum() else "_" for c in sheet)[:40]
    path = directory / f"{source.stem}_{safe_sheet}_{stamp}.csv"

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(STAGE_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------- #
# 3. Load
# --------------------------------------------------------------------------- #

MERGE_UPDATE = """
INSERT INTO {table} AS target ({columns})
SELECT {columns} FROM {staging}
ON CONFLICT (source_key) DO UPDATE SET
    sale_date = EXCLUDED.sale_date,
    agent = EXCLUDED.agent,
    category = EXCLUDED.category,
    channel = EXCLUDED.channel,
    policy_id = EXCLUDED.policy_id,
    premium = EXCLUDED.premium,
    units = EXCLUDED.units,
    source_file = EXCLUDED.source_file,
    source_sheet = EXCLUDED.source_sheet,
    source_row = EXCLUDED.source_row
WHERE target.sale_date IS DISTINCT FROM EXCLUDED.sale_date
   OR target.agent     IS DISTINCT FROM EXCLUDED.agent
   OR target.category  IS DISTINCT FROM EXCLUDED.category
   OR target.channel   IS DISTINCT FROM EXCLUDED.channel
   OR target.policy_id IS DISTINCT FROM EXCLUDED.policy_id
   OR target.premium   IS DISTINCT FROM EXCLUDED.premium
   OR target.units     IS DISTINCT FROM EXCLUDED.units
RETURNING (xmax = 0) AS inserted
"""

MERGE_IGNORE = """
INSERT INTO {table} ({columns})
SELECT {columns} FROM {staging}
ON CONFLICT (source_key) DO NOTHING
RETURNING true AS inserted
"""


def load(
    csv_path: Path,
    pg: PostgresSettings,
    outcome: Outcome,
    on_conflict: str,
) -> None:
    """COPY the CSV into a temp table, then merge into the sales table."""
    columns = sql.SQL(", ").join(sql.Identifier(c) for c in STAGE_COLUMNS)
    table = sql.Identifier(pg.db_schema, pg.table)
    staging = sql.Identifier("etl_staging")

    with database.connect(pg) as connection:
        with connection.cursor() as cursor:
            # Shaped from the real table by selecting the staging columns out
            # of it: the temp table then has exactly those columns with exactly
            # those types, so a bad value fails at COPY rather than halfway
            # through the merge -- and the two can never drift apart.
            #
            # (LIKE would be the obvious choice, but it copies the identity
            # column without its sequence, leaving `id` NOT NULL with no
            # default and every COPY failing.)
            cursor.execute(
                sql.SQL(
                    "CREATE TEMP TABLE {staging} ON COMMIT DROP AS "
                    "SELECT {columns} FROM {table} WITH NO DATA"
                ).format(staging=staging, columns=columns, table=table)
            )

            copy_sql = sql.SQL("COPY {staging} ({columns}) FROM STDIN WITH CSV HEADER")
            with cursor.copy(copy_sql.format(staging=staging, columns=columns)) as copy:
                with csv_path.open("rb") as handle:
                    while chunk := handle.read(65536):
                        copy.write(chunk)

            cursor.execute(sql.SQL("SELECT count(*) FROM {}").format(staging))
            outcome.rows_staged = int(cursor.fetchone()[0])

            template = MERGE_UPDATE if on_conflict == "update" else MERGE_IGNORE
            cursor.execute(
                sql.SQL(template).format(
                    table=table, columns=columns, staging=staging
                )
            )
            results = cursor.fetchall()

        outcome.rows_inserted = sum(1 for row in results if row[0])
        outcome.rows_updated = sum(1 for row in results if not row[0])
        outcome.rows_skipped = outcome.rows_staged - len(results)
        connection.commit()


def record_run(
    pg: PostgresSettings,
    outcome: Outcome,
    source: Path,
    sheet: str,
    started: datetime,
    status: str,
    message: str | None,
) -> None:
    """Append to etl_runs. Never allowed to fail the load it is describing."""
    try:
        with database.connect(pg) as connection:
            if not database.table_exists(connection, pg.db_schema, "etl_runs"):
                return
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "INSERT INTO {} (started_at, finished_at, source_file, "
                        "source_sheet, csv_path, rows_read, rows_staged, "
                        "rows_inserted, rows_updated, rows_skipped, status, message) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    ).format(sql.Identifier(pg.db_schema, "etl_runs")),
                    (
                        started,
                        datetime.now().astimezone(),
                        source.name,
                        sheet,
                        str(outcome.csv_path) if outcome.csv_path else None,
                        outcome.rows_read,
                        outcome.rows_staged,
                        outcome.rows_inserted,
                        outcome.rows_updated,
                        outcome.rows_skipped,
                        status,
                        message,
                    ),
                )
            connection.commit()
    except Exception:  # noqa: BLE001 - auditing must not break the pipeline
        pass
