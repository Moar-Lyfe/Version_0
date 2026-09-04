"""Excel -> CSV -> Postgres.

The read and stage stages are tested with no database. The load stage -- where
idempotency actually lives -- runs against a real server when one is offered
through ``EXEC_DASH_TEST_DSN``.
"""

import csv
import os
from pathlib import Path

import pandas as pd
import pytest

from app.data import etl
from app.settings import CategorySettings, DataSettings, PostgresSettings

ROOT = Path(__file__).resolve().parent.parent


COLUMNS = {
    "date": ("Date", "Effective Date"),
    "premium": ("Premium", "Written Premium"),
    "agent": ("Agent", "Agent Name"),
    "category": ("Category", "Product"),
    "channel": ("Channel", "Source"),
    "policy_id": ("Policy", "Policy Number"),
    "count": ("Units",),
}

HEADERS = [
    "Effective Date", "Agent Name", "Product", "Source", "Policy Number",
    "Written Premium", "Units",
]


@pytest.fixture
def data_settings() -> DataSettings:
    return DataSettings(
        columns=COLUMNS,
        category_1=CategorySettings("category_1", "Life", ("Term Life",)),
        category_2=CategorySettings("category_2", "Health", ("Medicare",)),
    )


def write_workbook(path: Path, rows: list[list], sheet: str = "Production") -> Path:
    pd.DataFrame(rows, columns=HEADERS).to_excel(path, index=False, sheet_name=sheet)
    return path


@pytest.fixture
def workbook(tmp_path) -> Path:
    return write_workbook(
        tmp_path / "export.xlsx",
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$1,200.50", 1],
            ["2026-09-01", "Web Sales", "Medicare", "Web", "P-2", "800", 1],
            ["2026-09-02", "Marcus Vale", "Annuity", "Referral", "P-3", "(250.00)", 2],
            ["not a date", "Marcus Vale", "Term Life", "Referral", "P-4", "999", 1],
        ],
    )


def normalise(workbook: Path, data_settings, sheet="Production", header_row=0):
    outcome = etl.Outcome()
    raw = etl.read_sheet(workbook, sheet, header_row)
    rows = etl.normalise(raw, data_settings, workbook, sheet, header_row, outcome)
    return rows, outcome


# --------------------------------------------------------------------------- #
# 1. Read
# --------------------------------------------------------------------------- #


def test_the_named_sheet_is_read(workbook, data_settings):
    raw = etl.read_sheet(workbook, "Production", 0)
    assert raw.attrs["sheet_name"] == "Production"
    assert len(raw) == 4


def test_a_missing_sheet_names_what_is_available(workbook):
    with pytest.raises(ValueError, match="not found"):
        etl.read_sheet(workbook, "Nope", 0)


def test_a_sheet_index_resolves_to_its_real_name(workbook):
    assert etl.read_sheet(workbook, 0, 0).attrs["sheet_name"] == "Production"


def test_an_out_of_range_index_is_refused(workbook):
    with pytest.raises(ValueError, match="out of range"):
        etl.read_sheet(workbook, 7, 0)


def test_cells_are_parsed_the_same_way_the_dashboard_parsed_them(
    workbook, data_settings
):
    rows, outcome = normalise(workbook, data_settings)

    assert outcome.rows_read == 4
    assert len(rows) == 3                      # the undated row is dropped
    assert any("no readable date" in p for p in outcome.problems)

    by_policy = {r["policy_id"]: r for r in rows}
    assert by_policy["P-1"]["premium"] == "1200.50"     # currency text
    assert by_policy["P-3"]["premium"] == "-250.00"     # accounting negative
    assert by_policy["P-3"]["units"] == "2.000"
    # Spreadsheet row numbers, so a bad figure can be found in the workbook.
    assert by_policy["P-1"]["source_row"] == 2


def test_a_missing_date_column_is_a_clear_failure(tmp_path, data_settings):
    path = tmp_path / "bad.xlsx"
    pd.DataFrame([["Dana", 100]], columns=["Agent Name", "Written Premium"]).to_excel(
        path, index=False
    )
    with pytest.raises(ValueError, match="No date column"):
        normalise(path, data_settings, sheet=0)


# --------------------------------------------------------------------------- #
# The natural key -- what makes the load idempotent
# --------------------------------------------------------------------------- #


def test_the_policy_number_is_the_key_when_there_is_one(workbook, data_settings):
    first, _ = normalise(workbook, data_settings)

    # Same policies, different premiums: a correction, not new business.
    corrected = write_workbook(
        workbook.parent / "corrected.xlsx",
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$9,999.00", 1],
            ["2026-09-01", "Web Sales", "Medicare", "Web", "P-2", "800", 1],
            ["2026-09-02", "Marcus Vale", "Annuity", "Referral", "P-3", "(250.00)", 2],
        ],
    )
    second, _ = normalise(corrected, data_settings)

    assert {r["source_key"] for r in first} == {r["source_key"] for r in second}


def test_without_a_policy_number_the_key_is_the_row_content(tmp_path, data_settings):
    headers = [h for h in HEADERS if h != "Policy Number"]
    path = tmp_path / "nokey.xlsx"
    pd.DataFrame(
        [["2026-09-01", "Dana", "Term Life", "Referral", "$100.00", 1]], columns=headers
    ).to_excel(path, index=False, sheet_name="Production")

    rows, outcome = normalise(path, data_settings)
    assert rows[0]["source_key"]
    assert any("content hash" in p for p in outcome.problems)


def test_a_repeated_key_within_one_sheet_is_collapsed(tmp_path, data_settings):
    """Two rows with one policy number would break the merge outright."""
    path = write_workbook(
        tmp_path / "dupes.xlsx",
        [
            ["2026-09-01", "Dana", "Term Life", "Referral", "P-1", "100", 1],
            ["2026-09-01", "Dana", "Term Life", "Referral", "P-1", "100", 1],
        ],
    )
    rows, outcome = normalise(path, data_settings)
    assert len(rows) == 1
    assert any("repeated a key" in p for p in outcome.problems)


def test_the_key_is_stable_across_runs(workbook, data_settings):
    first, _ = normalise(workbook, data_settings)
    second, _ = normalise(workbook, data_settings)
    assert [r["source_key"] for r in first] == [r["source_key"] for r in second]


# --------------------------------------------------------------------------- #
# 2. Stage
# --------------------------------------------------------------------------- #


def test_the_csv_holds_every_staging_column(workbook, data_settings, tmp_path):
    rows, _ = normalise(workbook, data_settings)
    path = etl.write_csv(rows, tmp_path / "etl", workbook, "Production")

    assert path.exists()
    with path.open(newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    assert list(written[0]) == list(etl.STAGE_COLUMNS)
    assert len(written) == len(rows)


def test_each_run_archives_its_own_csv(workbook, data_settings, tmp_path):
    rows, _ = normalise(workbook, data_settings)
    first = etl.write_csv(rows, tmp_path / "etl", workbook, "Production")
    second = etl.write_csv(rows, tmp_path / "etl", workbook, "Production")
    # Same second or not, neither run may silently overwrite the other's record.
    assert first.exists() and second.exists()


# --------------------------------------------------------------------------- #
# 3. Load -- only when a database is offered
# --------------------------------------------------------------------------- #

DSN = os.environ.get("EXEC_DASH_TEST_DSN")
requires_db = pytest.mark.skipif(
    not DSN, reason="set EXEC_DASH_TEST_DSN to run the load stage"
)


@pytest.fixture
def live_pg(tmp_path) -> PostgresSettings:
    host, port, dbname, user = DSN.split(":")
    settings = PostgresSettings(
        host=host, port=int(port), database=dbname, user=user,
        password_env=None, table="sales_etl_test",
    )
    import psycopg

    with psycopg.connect(**settings.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS sales_etl_test CASCADE")
            cursor.execute("DROP TABLE IF EXISTS etl_runs_etl_test CASCADE")
            cursor.execute(
                """
                CREATE TABLE sales_etl_test (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    source_key TEXT NOT NULL,
                    sale_date DATE NOT NULL,
                    agent TEXT NOT NULL DEFAULT 'Unassigned',
                    category TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    policy_id TEXT NOT NULL DEFAULT '',
                    premium NUMERIC(14,2) NOT NULL DEFAULT 0,
                    units NUMERIC(12,3) NOT NULL DEFAULT 1,
                    source_file TEXT, source_sheet TEXT, source_row INTEGER
                )
                """
            )
            cursor.execute(
                "CREATE UNIQUE INDEX ON sales_etl_test (source_key)"
            )
        connection.commit()
    return settings


def count_rows(pg: PostgresSettings) -> tuple[int, float]:
    import psycopg

    with psycopg.connect(**pg.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*), coalesce(sum(premium), 0) FROM {pg.table}")
            rows, total = cursor.fetchone()
    return int(rows), float(total)


@requires_db
def test_a_fresh_load_inserts_everything(workbook, data_settings, live_pg, tmp_path):
    rows, _ = normalise(workbook, data_settings)
    csv_path = etl.write_csv(rows, tmp_path / "etl", workbook, "Production")

    outcome = etl.Outcome()
    etl.load(csv_path, live_pg, outcome, "update")

    assert outcome.rows_staged == 3
    assert outcome.rows_inserted == 3
    assert outcome.rows_updated == 0
    assert count_rows(live_pg)[0] == 3


@requires_db
def test_reloading_the_same_workbook_changes_nothing(
    workbook, data_settings, live_pg, tmp_path
):
    """The property the whole design exists for."""
    rows, _ = normalise(workbook, data_settings)
    csv_path = etl.write_csv(rows, tmp_path / "etl", workbook, "Production")
    etl.load(csv_path, live_pg, etl.Outcome(), "update")

    before = count_rows(live_pg)
    second = etl.Outcome()
    etl.load(csv_path, live_pg, second, "update")

    assert second.rows_inserted == 0
    assert second.rows_updated == 0
    assert second.rows_skipped == 3      # present and identical
    assert count_rows(live_pg) == before


@requires_db
def test_a_correction_updates_in_place(workbook, data_settings, live_pg, tmp_path):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )
    rows_before, total_before = count_rows(live_pg)

    corrected = write_workbook(
        tmp_path / "corrected.xlsx",
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$2,200.50", 1],
            ["2026-09-01", "Web Sales", "Medicare", "Web", "P-2", "800", 1],
            ["2026-09-02", "Marcus Vale", "Annuity", "Referral", "P-3", "(250.00)", 2],
        ],
    )
    fixed_rows, _ = normalise(corrected, data_settings)
    outcome = etl.Outcome()
    etl.load(
        etl.write_csv(fixed_rows, tmp_path / "etl", corrected, "Production"),
        live_pg, outcome, "update",
    )

    rows_after, total_after = count_rows(live_pg)
    assert outcome.rows_inserted == 0
    assert outcome.rows_updated == 1
    assert rows_after == rows_before                     # no duplicate
    assert total_after - total_before == pytest.approx(1000.0)


@requires_db
def test_ignore_mode_leaves_corrections_alone(
    workbook, data_settings, live_pg, tmp_path
):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "ignore",
    )
    _, total_before = count_rows(live_pg)

    corrected = write_workbook(
        tmp_path / "corrected.xlsx",
        [["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$9,999.00", 1]],
    )
    fixed_rows, _ = normalise(corrected, data_settings)
    outcome = etl.Outcome()
    etl.load(
        etl.write_csv(fixed_rows, tmp_path / "etl", corrected, "Production"),
        live_pg, outcome, "ignore",
    )

    assert outcome.rows_inserted == 0
    assert count_rows(live_pg)[1] == pytest.approx(total_before)


@requires_db
def test_new_rows_load_incrementally(workbook, data_settings, live_pg, tmp_path):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )

    grown = write_workbook(
        tmp_path / "grown.xlsx",
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$1,200.50", 1],
            ["2026-09-03", "Priya Raghavan", "Term Life", "Referral", "P-9", "$500.00", 1],
        ],
    )
    new_rows, _ = normalise(grown, data_settings)
    outcome = etl.Outcome()
    etl.load(
        etl.write_csv(new_rows, tmp_path / "etl", grown, "Production"),
        live_pg, outcome, "update",
    )

    assert outcome.rows_inserted == 1
    assert outcome.rows_skipped == 1
    assert count_rows(live_pg)[0] == 4


# --------------------------------------------------------------------------- #
# Reconciliation -- rows Excel no longer has
# --------------------------------------------------------------------------- #


@requires_db
def test_a_deleted_excel_row_shows_as_an_orphan(
    workbook, data_settings, live_pg, tmp_path
):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )

    # The same workbook, with one policy removed.
    trimmed = write_workbook(
        tmp_path / "export.xlsx",   # same filename: same source_file in the table
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$1,200.50", 1],
            ["2026-09-02", "Marcus Vale", "Annuity", "Referral", "P-3", "(250.00)", 2],
        ],
    )
    result = etl.reconcile([(trimmed, "Production", 0)], data_settings, live_pg)

    assert not result.is_clean
    assert [o.policy_id for o in result.orphans] == ["P-2"]
    assert result.rows_in_scope == 3


@requires_db
def test_an_unchanged_workbook_reconciles_clean(
    workbook, data_settings, live_pg, tmp_path
):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )
    result = etl.reconcile([(workbook, "Production", 0)], data_settings, live_pg)
    assert result.is_clean
    assert result.orphans == []


@requires_db
def test_rows_from_an_unscanned_workbook_are_never_condemned(
    workbook, data_settings, live_pg, tmp_path
):
    """The mistake that would empty the table.

    Reconciling against one workbook must not condemn rows loaded from a
    different one -- otherwise archiving last year's export would delete last
    year's data.
    """
    first, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(first, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )

    other = write_workbook(
        tmp_path / "other.xlsx",
        [["2026-08-01", "Priya", "Term Life", "Referral", "P-99", "$400.00", 1]],
    )
    second, _ = normalise(other, data_settings)
    etl.load(
        etl.write_csv(second, tmp_path / "etl", other, "Production"),
        live_pg, etl.Outcome(), "update",
    )
    assert count_rows(live_pg)[0] == 4

    # Scan only the second workbook.
    result = etl.reconcile([(other, "Production", 0)], data_settings, live_pg)
    assert result.is_clean
    assert result.rows_in_scope == 1        # only other.xlsx's row was in scope


@requires_db
def test_an_unreadable_workbook_takes_its_rows_out_of_scope(
    workbook, data_settings, live_pg, tmp_path
):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )

    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a workbook")
    result = etl.reconcile(
        [(workbook, "Production", 0), (broken, "Production", 0)],
        data_settings,
        live_pg,
    )
    assert any("could not be read" in p for p in result.problems)
    assert result.is_clean          # the readable one still matched


@requires_db
def test_prune_deletes_exactly_the_orphans(
    workbook, data_settings, live_pg, tmp_path
):
    rows, _ = normalise(workbook, data_settings)
    etl.load(
        etl.write_csv(rows, tmp_path / "etl", workbook, "Production"),
        live_pg, etl.Outcome(), "update",
    )
    before_rows, before_total = count_rows(live_pg)

    trimmed = write_workbook(
        tmp_path / "export.xlsx",
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$1,200.50", 1],
            ["2026-09-02", "Marcus Vale", "Annuity", "Referral", "P-3", "(250.00)", 2],
        ],
    )
    result = etl.reconcile([(trimmed, "Production", 0)], data_settings, live_pg)
    deleted = etl.prune(live_pg, result.orphans)

    after_rows, after_total = count_rows(live_pg)
    assert deleted == 1
    assert after_rows == before_rows - 1
    # P-2 carried 800.00.
    assert before_total - after_total == pytest.approx(800.0)


@requires_db
def test_pruning_nothing_is_a_no_op(live_pg):
    assert etl.prune(live_pg, []) == 0


def test_scanning_no_files_reports_rather_than_guessing(data_settings, tmp_path):
    """No database needed: an empty scan must never be read as "delete all"."""
    keys, scanned, problems = etl.scan_source_keys([], data_settings)
    assert keys == set()
    assert scanned == []
    assert problems == []
