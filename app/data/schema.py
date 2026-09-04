"""The canonical table every workbook is normalised into.

Downstream code (KPIs, pivots, projections) only ever sees these columns, which
is what lets the dashboard accept wildly different Excel exports.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

import pandas as pd

DATE = "date"
AGENT = "agent"
CATEGORY = "category"
CATEGORY_KEY = "category_key"
PREMIUM = "premium"
SALES = "sales"
CHANNEL = "channel"
POLICY_ID = "policy_id"
IS_WEB = "is_web"
SOURCE_FILE = "source_file"
SOURCE_SHEET = "source_sheet"
SOURCE_ROW = "source_row"

CANONICAL_COLUMNS = (
    DATE,
    AGENT,
    CATEGORY,
    CATEGORY_KEY,
    PREMIUM,
    SALES,
    CHANNEL,
    POLICY_ID,
    IS_WEB,
    SOURCE_FILE,
    SOURCE_SHEET,
    SOURCE_ROW,
)

CATEGORY_1 = "category_1"
CATEGORY_2 = "category_2"
CATEGORY_OTHER = "other"

UNASSIGNED_AGENT = "Unassigned"

_WHITESPACE = re.compile(r"\s+")
_NUMERIC_NOISE = re.compile(r"[^0-9.\-]")


def normalise_key(value: object) -> str:
    """Fold a header or lookup value into a comparable key.

    Case, accents, non-breaking spaces and repeated whitespace are all removed so
    that ``"Written  Premium"``, ``"written premium"`` and ``"WRITTEN PREMIUM"``
    collapse to the same key.
    """
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace(" ", " ").strip().lower()
    return _WHITESPACE.sub(" ", text)


def to_number(value: object) -> float:
    """Parse a spreadsheet cell into a float.

    Handles currency symbols, thousands separators, trailing percent signs and
    accounting negatives such as ``(1,234.00)``. Anything unparseable is 0.0.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return 0.0 if pd.isna(value) else float(value)

    text = str(value).strip()
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    cleaned = _NUMERIC_NOISE.sub("", text)
    if cleaned in ("", "-", ".", "-."):
        return 0.0
    try:
        number = float(cleaned)
    except ValueError:
        return 0.0
    return -number if negative else number


def clean_text(value: object, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    text = _WHITESPACE.sub(" ", str(value).replace(" ", " ").strip())
    return text or default


def empty_frame() -> pd.DataFrame:
    """A correctly typed, zero-row canonical frame."""
    return pd.DataFrame(
        {
            DATE: pd.Series(dtype="datetime64[ns]"),
            AGENT: pd.Series(dtype="object"),
            CATEGORY: pd.Series(dtype="object"),
            CATEGORY_KEY: pd.Series(dtype="object"),
            PREMIUM: pd.Series(dtype="float64"),
            SALES: pd.Series(dtype="float64"),
            CHANNEL: pd.Series(dtype="object"),
            POLICY_ID: pd.Series(dtype="object"),
            IS_WEB: pd.Series(dtype="bool"),
            SOURCE_FILE: pd.Series(dtype="object"),
            SOURCE_SHEET: pd.Series(dtype="object"),
            SOURCE_ROW: pd.Series(dtype="int64"),
        }
    )


def slice_dates(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Rows whose date falls inside the inclusive ``[start, end]`` window."""
    if frame.empty:
        return frame
    days = frame[DATE].dt.normalize()
    mask = (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))
    return frame.loc[mask]
