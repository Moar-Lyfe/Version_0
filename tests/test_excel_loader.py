"""Ingestion: column mapping, messy cells, and multi-workbook stacking."""

import pandas as pd
import pytest

from app.data import schema as S
from app.data.excel_loader import (
    discover_files,
    fingerprint,
    load_dataset,
    resolve_columns,
)
from app.settings import CategorySettings, DataSettings, DedupeSettings, SourceSettings

COLUMNS = {
    "date": ("Date", "Effective Date"),
    "premium": ("Premium", "Written Premium"),
    "agent": ("Agent", "Agent Name"),
    "category": ("Category", "Product"),
    "channel": ("Channel", "Source"),
    "policy_id": ("Policy", "Policy Number"),
    "count": ("Units",),
}


def data_settings(tmp_path, **overrides) -> DataSettings:
    return DataSettings(
        sources=(SourceSettings(name="test", path=str(tmp_path), glob="*.xlsx"),),
        columns=COLUMNS,
        category_1=CategorySettings("category_1", "Life", ("Term Life", "Whole Life")),
        category_2=CategorySettings("category_2", "Health", ("Medicare",)),
        web_channel_values=("Web", "Online"),
        web_agent_values=("Web Sales",),
        **overrides,
    )


@pytest.fixture
def workbook(tmp_path):
    frame = pd.DataFrame(
        [
            ["2026-09-01", "Dana Whitfield", "Term Life", "Referral", "P-1", "$1,200.50", 1],
            ["2026-09-01", "Web Sales", "Medicare", "Web", "P-2", "800", 1],
            ["2026-09-02", "Marcus Vale", "Annuity", "Referral", "P-3", "(250.00)", 2],
            ["not a date", "Marcus Vale", "Term Life", "Referral", "P-4", "999", 1],
            ["2026-09-03", "", "Whole Life", "Online", "P-5", "", 1],
        ],
        columns=[
            "Effective Date",
            "Agent Name",
            "Product",
            "Source",
            "Policy Number",
            "Written Premium",
            "Units",
        ],
    )
    path = tmp_path / "book.xlsx"
    frame.to_excel(path, index=False, sheet_name="Production")
    return path


def test_resolve_columns_prefers_exact_alias_matches():
    headers = ["Effective Date", "Written  Premium", "Agent Name"]
    resolved = resolve_columns(headers, COLUMNS)
    assert resolved["date"] == "Effective Date"
    assert resolved["premium"] == "Written  Premium"   # extra whitespace folded away
    assert resolved["agent"] == "Agent Name"
    assert "channel" not in resolved


def test_resolve_columns_falls_back_to_word_containment():
    resolved = resolve_columns(["Agent Name (Primary)", "Sale Date"], COLUMNS)
    assert resolved["agent"] == "Agent Name (Primary)"


def test_resolve_columns_never_claims_one_header_twice():
    resolved = resolve_columns(["Premium"], {"premium": ("Premium",), "amount": ("Premium",)})
    assert resolved == {"premium": "Premium"}


def test_load_normalises_a_messy_sheet(tmp_path, workbook):
    result = load_dataset(data_settings(tmp_path))
    frame = result.frame

    # The unparseable date is dropped, and reported rather than swallowed.
    assert len(frame) == 4
    assert result.files[0].rows_without_date == 1
    assert any("unreadable date" in warning for warning in result.warnings)

    # Currency text, accounting negatives and blanks all become numbers.
    assert frame[S.PREMIUM].tolist() == [1200.50, 800.0, -250.0, 0.0]
    # The count column drives sales; a blank agent becomes Unassigned.
    assert frame[S.SALES].tolist() == [1.0, 1.0, 2.0, 1.0]
    assert frame.loc[3, S.AGENT] == S.UNASSIGNED_AGENT


def test_categories_and_web_flags(tmp_path, workbook):
    result = load_dataset(data_settings(tmp_path))
    frame = result.frame
    assert frame[S.CATEGORY_KEY].tolist() == [
        S.CATEGORY_1,
        S.CATEGORY_2,
        S.CATEGORY_OTHER,
        S.CATEGORY_1,
    ]
    # Web by channel, by agent, and neither.
    assert frame[S.IS_WEB].tolist() == [False, True, False, True]
    assert result.unmatched_categories == {"Annuity": 1}


def test_rows_carry_their_provenance(tmp_path, workbook):
    result = load_dataset(data_settings(tmp_path))
    assert result.frame[S.SOURCE_SHEET].unique().tolist() == ["Production"]
    # Row 2 of the spreadsheet is the first data row under the header.
    assert result.frame[S.SOURCE_ROW].tolist() == [2, 3, 4, 6]


def test_multiple_workbooks_are_stacked_and_sorted(tmp_path):
    for name, day in (("a.xlsx", "2026-09-05"), ("b.xlsx", "2026-09-02")):
        pd.DataFrame(
            [[day, "Dana", "Term Life", "Referral", "P-9", "100", 1]],
            columns=list(
                ["Effective Date", "Agent Name", "Product", "Source", "Policy Number",
                 "Written Premium", "Units"]
            ),
        ).to_excel(tmp_path / name, index=False)

    result = load_dataset(data_settings(tmp_path))
    assert result.row_count == 2
    assert result.file_count == 2
    assert result.frame[S.DATE].is_monotonic_increasing


def test_missing_date_column_is_reported_not_raised(tmp_path):
    pd.DataFrame([["Dana", 100]], columns=["Agent Name", "Written Premium"]).to_excel(
        tmp_path / "bad.xlsx", index=False
    )
    result = load_dataset(data_settings(tmp_path))
    assert result.frame.empty
    assert result.has_errors
    assert "No date column" in result.files[0].error


def test_missing_directory_warns(tmp_path):
    settings = DataSettings(
        sources=(SourceSettings(name="gone", path=str(tmp_path / "nope")),),
        columns=COLUMNS,
    )
    result = load_dataset(settings)
    assert result.frame.empty
    assert any("no files matched" in w.lower() for w in result.warnings)


def test_dedupe_is_opt_in(tmp_path):
    rows = [["2026-09-01", "Dana", "Term Life", "Referral", "P-1", "100", 1]] * 2
    pd.DataFrame(
        rows,
        columns=["Effective Date", "Agent Name", "Product", "Source", "Policy Number",
                 "Written Premium", "Units"],
    ).to_excel(tmp_path / "dupes.xlsx", index=False)

    assert load_dataset(data_settings(tmp_path)).row_count == 2

    deduped = load_dataset(
        data_settings(tmp_path, dedupe=DedupeSettings(enabled=True, keys=("policy_id", "date")))
    )
    assert deduped.row_count == 1
    assert deduped.duplicates_removed == 1


def test_open_workbook_lock_files_are_ignored(tmp_path, workbook):
    (tmp_path / "~$book.xlsx").write_bytes(b"not a workbook")
    source = SourceSettings(name="test", path=str(tmp_path), glob="*.xlsx")
    assert [p.name for p in discover_files(source)] == ["book.xlsx"]


def test_fingerprint_changes_when_a_file_changes(tmp_path, workbook):
    source = SourceSettings(name="test", path=str(tmp_path), glob="*.xlsx")
    before = fingerprint((source,))
    pd.DataFrame([["2026-09-04", "Dana", "Term Life", "Referral", "P-8", "1", 1]],
                 columns=["Effective Date", "Agent Name", "Product", "Source",
                          "Policy Number", "Written Premium", "Units"]
                 ).to_excel(tmp_path / "extra.xlsx", index=False)
    assert fingerprint((source,)) != before
