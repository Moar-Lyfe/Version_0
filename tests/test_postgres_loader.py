"""Reading the canonical dataset out of Postgres.

Split in two: the mapping and parsing logic is tested with no database at all,
and the round-trip is tested against a real server when one is offered through
``EXEC_DASH_TEST_DSN``. That way the suite is meaningful on a laptop with no
Postgres and thorough on a machine that has one.
"""

import os
from datetime import date

import pytest

from app.data import postgres_loader
from app.data import schema as S
from app.settings import CategorySettings, DataSettings, PostgresSettings

pytest_plugins: list[str] = []


@pytest.fixture
def data_settings() -> DataSettings:
    return DataSettings(
        source_type="postgres",
        category_1=CategorySettings("category_1", "Life", ("Term Life",)),
        category_2=CategorySettings("category_2", "Health", ("Medicare",)),
        web_channel_values=("Web",),
        web_agent_values=("Web Sales",),
    )


@pytest.fixture
def pg() -> PostgresSettings:
    return PostgresSettings(database="reporting", user="dashboard")


# --------------------------------------------------------------------------- #
# Query composition -- no database needed
# --------------------------------------------------------------------------- #


def rendered(query) -> str:
    """The SQL as text, for assertions."""
    return query.as_string(None) if hasattr(query, "as_string") else str(query)


def test_the_default_mapping_targets_the_shipped_schema(pg):
    query, resolved = postgres_loader.build_query(pg)
    assert resolved["date"] == "sale_date"
    assert resolved["count"] == "units"
    text = rendered(query)
    assert '"sale_date" AS "date"' in text
    assert '"public"."sales"' in text


def test_columns_can_be_remapped_without_renaming_the_table(pg):
    remapped = PostgresSettings(
        table="policy_sales",
        columns={"date": "effective_date", "premium": "written_premium"},
    )
    query, resolved = postgres_loader.build_query(remapped)
    assert resolved["date"] == "effective_date"
    text = rendered(query)
    assert '"effective_date" AS "date"' in text
    assert '"policy_sales"' in text


def test_an_unmapped_field_selects_null_so_the_shape_never_changes(pg):
    sparse = PostgresSettings(columns={"date": "sale_date", "channel": ""})
    query, resolved = postgres_loader.build_query(sparse)
    assert "channel" not in resolved
    assert 'NULL AS "channel"' in rendered(query)


def test_identifiers_are_quoted_not_interpolated():
    """A table name out of config.yaml must not be able to inject SQL."""
    nasty = PostgresSettings(table='sales"; DROP TABLE sales; --')
    text = rendered(postgres_loader.build_query(nasty)[0])
    assert "DROP TABLE sales; --" in text          # present, but...
    assert 'sales""; DROP TABLE sales; --"' in text  # ...quoted as one identifier


def test_a_where_clause_is_appended(pg):
    filtered = PostgresSettings(where="status <> 'VOID'")
    assert "WHERE status <> 'VOID'" in rendered(postgres_loader.build_query(filtered)[0])


def test_provenance_columns_are_selected_when_present(pg):
    text = rendered(postgres_loader.build_query(pg, ["source_file", "source_row"])[0])
    assert '"source_file"' in text and '"source_row"' in text


# --------------------------------------------------------------------------- #
# Row -> frame -- no database needed
# --------------------------------------------------------------------------- #

FIELDS = [
    "date", "premium", "agent", "category", "channel", "policy_id", "count",
    "source_file", "source_sheet", "source_row",
]


def rows_to_frame(rows, data_settings, warnings=None):
    # `warnings or []` would substitute a fresh list for a caller's empty one,
    # quietly discarding everything appended to it.
    sink = [] if warnings is None else warnings
    return postgres_loader._to_frame(rows, FIELDS, data_settings, sink)


def test_rows_become_the_canonical_frame(data_settings):
    rows = [
        (date(2026, 9, 1), 1200.50, "Dana", "Term Life", "Referral", "P-1", 1,
         "book.xlsx", "Production", 2),
        (date(2026, 9, 1), 800, "Web Sales", "Medicare", "Web", "P-2", 1,
         "book.xlsx", "Production", 3),
    ]
    frame, dropped = rows_to_frame(rows, data_settings)

    assert dropped == 0
    assert list(frame.columns) == list(S.CANONICAL_COLUMNS)
    assert frame[S.PREMIUM].tolist() == [1200.50, 800.0]
    assert frame[S.CATEGORY_KEY].tolist() == [S.CATEGORY_1, S.CATEGORY_2]
    # Web by channel and by agent, exactly as the workbook reader decides it.
    assert frame[S.IS_WEB].tolist() == [False, True]
    assert frame[S.SOURCE_ROW].tolist() == [2, 3]


def test_nulls_fall_back_to_the_same_defaults_as_the_workbook_reader(data_settings):
    rows = [(date(2026, 9, 1), None, None, None, None, None, None, None, None, None)]
    frame, _ = rows_to_frame(rows, data_settings)

    assert frame[S.PREMIUM].iloc[0] == 0.0
    assert frame[S.SALES].iloc[0] == 1.0          # a null count is still one sale
    assert frame[S.AGENT].iloc[0] == S.UNASSIGNED_AGENT
    assert frame[S.CATEGORY_KEY].iloc[0] == S.CATEGORY_OTHER


def test_a_count_column_drives_sales(data_settings):
    rows = [(date(2026, 9, 1), 100, "Dana", "Term Life", "", "P-1", 3, None, None, None)]
    frame, _ = rows_to_frame(rows, data_settings)
    assert frame[S.SALES].iloc[0] == 3.0


def test_undated_rows_are_dropped_and_reported(data_settings):
    warnings: list[str] = []
    rows = [
        (date(2026, 9, 1), 100, "Dana", "Term Life", "", "P-1", 1, None, None, None),
        (None, 200, "Dana", "Term Life", "", "P-2", 1, None, None, None),
    ]
    frame, dropped = rows_to_frame(rows, data_settings, warnings)

    assert dropped == 1
    assert len(frame) == 1
    assert any("no readable date" in w for w in warnings)


def test_no_rows_yields_a_typed_empty_frame(data_settings):
    frame, dropped = rows_to_frame([], data_settings)
    assert frame.empty
    assert dropped == 0
    assert list(frame.columns) == list(S.CANONICAL_COLUMNS)


def test_decimal_premiums_survive_the_trip(data_settings):
    """psycopg returns NUMERIC as Decimal; the frame must be float."""
    from decimal import Decimal

    rows = [(date(2026, 9, 1), Decimal("1234.56"), "Dana", "Term Life", "", "P-1",
             Decimal("2.000"), None, None, None)]
    frame, _ = rows_to_frame(rows, data_settings)
    assert frame[S.PREMIUM].iloc[0] == pytest.approx(1234.56)
    assert frame[S.SALES].iloc[0] == pytest.approx(2.0)
    assert frame[S.PREMIUM].dtype.kind == "f"


# --------------------------------------------------------------------------- #
# Round trip -- only when a database is offered
# --------------------------------------------------------------------------- #

DSN = os.environ.get("EXEC_DASH_TEST_DSN")
requires_db = pytest.mark.skipif(
    not DSN, reason="set EXEC_DASH_TEST_DSN to run the database round trip"
)


@pytest.fixture
def live_settings() -> PostgresSettings:
    """Parse the test DSN into settings. Format: host:port:db:user."""
    host, port, dbname, user = DSN.split(":")
    return PostgresSettings(
        host=host, port=int(port), database=dbname, user=user,
        password_env=None, table="sales_roundtrip",
    )


@requires_db
def test_a_real_round_trip(live_settings, data_settings):
    import psycopg

    settings = DataSettings(
        source_type="postgres",
        postgres=live_settings,
        category_1=data_settings.category_1,
        category_2=data_settings.category_2,
        web_channel_values=data_settings.web_channel_values,
        web_agent_values=data_settings.web_agent_values,
    )

    with psycopg.connect(**live_settings.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS sales_roundtrip")
            cursor.execute(
                """
                CREATE TABLE sales_roundtrip (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    source_key TEXT NOT NULL,
                    sale_date DATE NOT NULL,
                    agent TEXT, category TEXT, channel TEXT, policy_id TEXT,
                    premium NUMERIC(14,2), units NUMERIC(12,3),
                    source_file TEXT, source_sheet TEXT, source_row INTEGER
                )
                """
            )
            cursor.executemany(
                "INSERT INTO sales_roundtrip (source_key, sale_date, agent, category,"
                " channel, policy_id, premium, units, source_file, source_sheet,"
                " source_row) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    ("k1", date(2026, 9, 1), "Dana", "Term Life", "Referral", "P-1",
                     "1200.50", "1", "book.xlsx", "Production", 2),
                    ("k2", date(2026, 9, 2), "Web Sales", "Medicare", "Web", "P-2",
                     "800.00", "1", "book.xlsx", "Production", 3),
                ],
            )
        connection.commit()

    result = postgres_loader.load_dataset(settings)

    assert result.row_count == 2
    assert not result.has_errors
    assert result.frame[S.PREMIUM].sum() == pytest.approx(2000.50)
    assert result.frame[S.IS_WEB].tolist() == [False, True]
    assert result.frame[S.CATEGORY_KEY].tolist() == [S.CATEGORY_1, S.CATEGORY_2]
    # Sorted by date, like the workbook reader.
    assert result.frame[S.DATE].is_monotonic_increasing


@requires_db
def test_a_missing_table_is_reported_not_raised(live_settings, data_settings):
    settings = DataSettings(
        source_type="postgres",
        postgres=PostgresSettings(
            **{**live_settings.__dict__, "table": "definitely_not_here"}
        ),
    )
    result = postgres_loader.load_dataset(settings)
    assert result.frame.empty
    assert result.has_errors
    assert "does not exist" in result.files[0].error


@requires_db
def test_a_mismapped_column_is_reported_with_what_is_available(
    live_settings, data_settings
):
    settings = DataSettings(
        source_type="postgres",
        postgres=PostgresSettings(
            **{**live_settings.__dict__, "columns": {"date": "no_such_column"}}
        ),
    )
    result = postgres_loader.load_dataset(settings)
    assert result.has_errors
    assert "not present" in result.files[0].error
    assert "Available:" in result.files[0].error


def test_an_unreachable_server_is_reported_not_raised(data_settings):
    """No database needed: nothing is listening on port 1."""
    settings = DataSettings(
        source_type="postgres",
        postgres=PostgresSettings(port=1, connect_timeout=2, password_env=None),
    )
    result = postgres_loader.load_dataset(settings)
    assert result.frame.empty
    assert result.has_errors
    assert "Could not reach PostgreSQL" in result.files[0].error
