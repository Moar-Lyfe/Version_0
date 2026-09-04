"""The operator gate.

One password protects every page that can *act* rather than merely report: the
Admin panel, which runs arbitrary Python, and the Database page, which carries a
SQL console. Both were built separately and only Admin was gated, which meant a
configured password bought less than it appeared to -- the console, the full
table and its CSV export sat open behind it.

The credential is the value of the environment variable named by
``admin.password_env``. Leave that null and there is no gate at all, which is a
reasonable choice on a trusted LAN but should be a deliberate one.
"""

from __future__ import annotations

import hmac

import streamlit as st

from app.settings import AdminSettings

SESSION_KEY = "operator_authorised"


def is_configured(settings: AdminSettings) -> bool:
    """Whether a password is actually in force on this machine."""
    return settings.required_password() is not None


def is_unlocked(settings: AdminSettings) -> bool:
    """Whether this session may act, without drawing anything."""
    if not is_configured(settings):
        return True
    return bool(st.session_state.get(SESSION_KEY))


def gate(settings: AdminSettings, what: str, form_key: str) -> bool:
    """Ask for the password. ``True`` means carry on and draw the page."""
    expected = settings.required_password()
    if expected is None:
        return True
    if st.session_state.get(SESSION_KEY):
        return True

    st.info(
        f"{what} is protected. Enter the value of `{settings.password_env}` as "
        "configured on this machine."
    )
    with st.form(form_key):
        supplied = st.text_input("Password", type="password")
        if st.form_submit_button("Unlock", type="primary"):
            # Constant-time: a timing difference is a small leak, but there is
            # no reason to hand one over for free.
            if hmac.compare_digest(supplied, expected):
                st.session_state[SESSION_KEY] = True
                st.rerun()
            st.error("Incorrect password.")
    return False


def lock_notice(settings: AdminSettings) -> str | None:
    """Why a dangerous option is unavailable, or ``None`` when it is not."""
    if is_configured(settings):
        return None
    return (
        "No operator password is configured, so this is disabled. Set "
        "`admin.password_env` in config.yaml to the name of an environment "
        "variable holding the password, and set that variable for the account "
        "running the dashboard."
    )
