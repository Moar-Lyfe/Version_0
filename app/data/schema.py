"""The canonical table every workbook is normalised into.

Downstream code (KPIs, pivots, projections) only ever sees these columns, which
is what lets the dashboard accept wildly different Excel exports.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

import pandas as pd

from app.settings import DataSettings

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


# --------------------------------------------------------------------------- #
# Derived fields
# --------------------------------------------------------------------------- #
#
# Category bucketing and web-sale detection are *interpretation*, not fact, so
# they are applied when the data is read rather than baked into storage. Change
# which products count as Category 1 in config.yaml and every figure moves --
# no reload, no migration. Both the Excel and the database readers call these,
# so the two can never disagree about what a row means.


def category_lookup(settings: DataSettings) -> dict[str, str]:
    """Normalised category value -> ``category_1`` / ``category_2``."""
    lookup: dict[str, str] = {}
    for spec in (settings.category_1, settings.category_2):
        for value in spec.values:
            lookup[normalise_key(value)] = spec.key
    return lookup


def web_lookups(settings: DataSettings) -> tuple[set[str], set[str]]:
    return (
        {normalise_key(v) for v in settings.web_channel_values},
        {normalise_key(v) for v in settings.web_agent_values},
    )


def derive(frame: pd.DataFrame, settings: DataSettings) -> pd.DataFrame:
    """Add :data:`CATEGORY_KEY` and :data:`IS_WEB` from the raw columns.

    ``frame`` must already carry CATEGORY, CHANNEL and AGENT. Returned with the
    canonical columns in canonical order.
    """
    if frame.empty:
        return empty_frame()

    lookup = category_lookup(settings)
    web_channels, web_agents = web_lookups(settings)

    out = frame.copy()
    out[CATEGORY_KEY] = [
        lookup.get(normalise_key(value), CATEGORY_OTHER) for value in out[CATEGORY]
    ]
    out[IS_WEB] = [
        normalise_key(channel) in web_channels or normalise_key(agent) in web_agents
        for channel, agent in zip(out[CHANNEL], out[AGENT])
    ]
    return out[list(CANONICAL_COLUMNS)]
