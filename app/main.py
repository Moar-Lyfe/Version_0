"""Streamlit entrypoint.

    streamlit run app/main.py

Everything the app needs comes from ``config/config.yaml`` (or the committed
example when that file is absent), so moving the dashboard to another machine is
a clone, an install, and one edited path.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `streamlit run app/main.py` from any working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st  # noqa: E402

from app.settings import load_settings  # noqa: E402
from app.ui import theme  # noqa: E402
from app.views import admin, dashboard, diagnostics  # noqa: E402


def main() -> None:
    settings = load_settings()

    st.set_page_config(
        page_title=settings.app.title,
        page_icon="📊",
        layout="centered",
        # Collapsed by default so a phone opens straight onto the KPIs.
        initial_sidebar_state="collapsed",
    )
    theme.inject()

    pages = {
        "Dashboard": lambda: dashboard.render(settings),
        "Admin": lambda: admin.render(settings),
        "Diagnostics": lambda: diagnostics.render(settings),
    }

    with st.sidebar:
        st.markdown(f"### {settings.app.title}")
        st.caption(settings.app.organization or "Internal reporting")
        choice = st.radio("Page", list(pages), label_visibility="collapsed")
        st.divider()
        st.caption(f"Config: `{settings.source_file.name}`")

    pages[choice]()


if __name__ == "__main__":
    main()
