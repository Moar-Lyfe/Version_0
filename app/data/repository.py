"""Cached access to the dataset.

Streamlit reruns the whole script on every interaction, so reading the workbooks
inline would re-parse Excel on every click. The load is cached against a
fingerprint of the files on disk, which gives two things at once:

* clicking a widget is instant, because the cache hits;
* replacing a workbook invalidates the cache by itself, because the fingerprint
  changed -- the Refresh button just forces the check to happen now.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from app.data.excel_loader import LoadResult, fingerprint
from app.data.loader import load_for_source
from app.settings import POSTGRES, DataSettings, Settings

_REFRESH_TOKEN = "data_refresh_token"


@st.cache_data(show_spinner=False, ttl=None, max_entries=4)
def _cached_load(_settings: DataSettings, signature: str, token: int) -> LoadResult:
    """Cached by ``signature`` (source state) and ``token`` (manual refresh).

    ``_settings`` is prefixed with an underscore so Streamlit does not try to
    hash the dataclass; ``signature`` already changes whenever the source does.
    """
    return load_for_source(_settings)


def source_signature(settings: DataSettings) -> str:
    """A cheap signature of the data behind the dashboard.

    For workbooks this is their modification state, so a replaced file
    invalidates the cache by itself. A database has no equivalent an app can
    read cheaply -- a row could change at any moment without anything on disk
    moving -- so the connection identity is the key and Refresh is what forces a
    re-read. That is the honest trade: the button means something here.
    """
    if settings.source_type == POSTGRES:
        pg = settings.postgres
        return f"postgres:{pg.describe()}/{pg.qualified_table()}?{pg.where}"
    return fingerprint(settings.sources)


def clear_cache() -> None:
    """Drop every cached dataset, across all sessions."""
    _cached_load.clear()


def current_token() -> int:
    return int(st.session_state.get(_REFRESH_TOKEN, 0))


def request_refresh() -> None:
    """Force the next load to re-read every workbook from disk.

    Dropping the cached entries frees the previous frames instead of leaving
    them parked behind a stale key, and bumping the token guarantees a miss even
    if another session repopulated the cache in between.
    """
    clear_cache()
    st.session_state[_REFRESH_TOKEN] = current_token() + 1


def get_dataset(settings: Settings) -> LoadResult:
    """The dataset in force right now, from whichever source is configured."""
    return _cached_load(
        settings.data, source_signature(settings.data), current_token()
    )


def last_refresh_display(result: LoadResult, tz) -> str:
    """Human-readable timestamp of the load that produced ``result``."""
    stamp: datetime = result.loaded_at.astimezone(tz)
    return stamp.strftime("%b %d, %Y at %I:%M:%S %p %Z").replace(" 0", " ")


def source_description(settings: Settings, result: LoadResult) -> str:
    """Where the rows on screen came from, phrased for the source in use."""
    if settings.data.source_type == POSTGRES:
        return settings.data.postgres.qualified_table()
    return f"{result.file_count} workbook(s)"


def refresh_label(settings: Settings) -> str:
    if settings.data.source_type == POSTGRES:
        return "Re-read the database"
    return "Re-read all workbooks"


def refresh_help(settings: Settings) -> str:
    if settings.data.source_type == POSTGRES:
        return (
            "Re-query the reporting database. Rows can change at any moment "
            "without anything on disk moving, so this button is how the "
            "dashboard picks up a load that has since run."
        )
    return "Re-read every configured Excel workbook from disk."
