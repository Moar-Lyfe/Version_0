"""Small wrappers that keep the pages readable and version-tolerant."""

from __future__ import annotations

from typing import Callable, Sequence

import streamlit as st


def choice(
    label: str,
    options: Sequence[str],
    *,
    key: str,
    default: str | None = None,
    format_func: Callable[[str], str] | None = None,
    help: str | None = None,
) -> str:
    """A horizontal single-select that always has exactly one option selected.

    Prefers ``st.segmented_control`` -- the widest, most tappable control
    Streamlit offers on a phone -- and falls back to a horizontal radio on older
    builds.

    ``st.segmented_control`` lets a second tap on the active option *deselect*
    it and return ``None``, which would leave the dashboard with no reporting
    period at all. Restoring the previous value has to happen before the widget
    is instantiated (Streamlit refuses writes to a live widget's key), so the
    restore is stashed and applied on the next run.
    """
    options = list(options)
    fallback = default if default in options else options[0]
    pending_key = f"__{key}_pending"
    last_key = f"__{key}_last"

    if pending_key in st.session_state:
        st.session_state[key] = st.session_state.pop(pending_key)
    st.session_state.setdefault(key, fallback)
    st.session_state.setdefault(last_key, st.session_state[key])

    kwargs: dict = {"label": label, "options": options, "key": key, "help": help}
    if format_func is not None:
        kwargs["format_func"] = format_func

    if hasattr(st, "segmented_control"):
        selected = st.segmented_control(**kwargs)
        if selected is None:
            # Treat a deselect tap as a no-op and put the previous choice back.
            st.session_state[pending_key] = st.session_state[last_key]
            st.rerun()
    else:
        kwargs["horizontal"] = True
        selected = st.radio(**kwargs)

    st.session_state[last_key] = selected
    return selected


def dataframe(frame, **kwargs) -> None:
    """``st.dataframe`` that stretches to the container on any recent build."""
    try:
        st.dataframe(frame, width="stretch", **kwargs)
    except TypeError:
        st.dataframe(frame, use_container_width=True, **kwargs)
