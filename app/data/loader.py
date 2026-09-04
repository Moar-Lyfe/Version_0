"""Source dispatch, with no Streamlit in sight.

Which reader runs is a data concern, not a UI one. Keeping it here means
command-line tools and pipeline scripts can load the dataset without importing
Streamlit -- which would otherwise print a "no runtime found" warning into the
middle of their output.

:mod:`app.data.repository` wraps this with the dashboard's cache.
"""

from __future__ import annotations

from app.data import postgres_loader
from app.data.excel_loader import LoadResult
from app.data.excel_loader import load_dataset as load_excel
from app.settings import POSTGRES, DataSettings


def load_for_source(settings: DataSettings) -> LoadResult:
    """Read the dataset from whichever source is configured."""
    if settings.source_type == POSTGRES:
        return postgres_loader.load_dataset(settings)
    return load_excel(settings)
