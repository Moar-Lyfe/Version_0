"""Visual language for the dashboard.

Two rules drive everything here:

* **Minimal.** No colour is used decoratively -- colour only ever means
  "up", "down" or "needs attention".
* **Mobile first.** Streamlit's ``st.columns`` squeezes rather than wraps on a
  phone, so KPI cards are laid out with a CSS grid that genuinely reflows: four
  across on a desktop, two on a phone, one on a very narrow screen.

Colours are expressed as translucent greys over the current background, so the
same stylesheet reads correctly in Streamlit's light and dark themes.
"""

from __future__ import annotations

import streamlit as st

# Categorical slot 1 of the data-viz palette; the chart uses the same hue.
ACCENT = "#2a78d6"
ACCENT_DARK = "#2266bd"
POSITIVE = "#12805c"
NEGATIVE = "#c0392b"
NEUTRAL = "rgba(128, 128, 128, 0.85)"

_CSS = (
    """
<style>
:root {
  --ed-line: rgba(128, 128, 128, 0.22);
  --ed-surface: rgba(128, 128, 128, 0.07);
  --ed-muted: rgba(128, 128, 128, 0.95);
  --ed-radius: 12px;
}

/* ---------- Layout: tighter, calmer, phone-friendly ---------- */
.block-container,
[data-testid="stMainBlockContainer"] {
  padding-top: 2.2rem;
  padding-bottom: 3rem;
  max-width: 1180px;
}

@media (max-width: 640px) {
  .block-container,
  [data-testid="stMainBlockContainer"] {
    /* Streamlit's own padding wins on specificity here, and its fixed 60px
       header is opaque -- without !important the masthead hides behind it. */
    padding-top: 4.6rem !important;
    padding-left: 0.85rem;
    padding-right: 0.85rem;
  }
}

/* Keep the toolbar out of the way without hiding the sidebar control. */
[data-testid="stDecoration"] { display: none; }
[data-testid="stAppDeployButton"] { display: none; }
footer { visibility: hidden; height: 0; }

/* Streamlit only reads .streamlit/config.toml relative to the working
   directory, so an app launched from elsewhere would fall back to the default
   red accent. Pinning the primary action here keeps buttons, the selected
   segment and the chart on one colour however the app was started. */
[data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-primaryFormSubmit"],
button[kind="primary"],
button[kind="primaryFormSubmit"] {
  background-color: __ACCENT__;
  border-color: __ACCENT__;
  color: #ffffff;
}
[data-testid="stBaseButton-primary"]:hover,
[data-testid="stBaseButton-primaryFormSubmit"]:hover,
button[kind="primary"]:hover,
button[kind="primaryFormSubmit"]:hover {
  background-color: __ACCENT_DARK__;
  border-color: __ACCENT_DARK__;
  color: #ffffff;
}

/* ---------- Page masthead ---------- */
.ed-masthead {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.35rem 0.75rem;
  margin-bottom: 0.15rem;
}
.ed-masthead h1 {
  font-size: clamp(1.4rem, 4.6vw, 2rem);
  font-weight: 650;
  letter-spacing: -0.02em;
  margin: 0;
  padding: 0;
  line-height: 1.15;
}
.ed-masthead .ed-org {
  font-size: 0.82rem;
  color: var(--ed-muted);
  text-transform: uppercase;
  letter-spacing: 0.09em;
}
.ed-subhead {
  color: var(--ed-muted);
  font-size: 0.85rem;
  margin: 0 0 1.1rem 0;
}

/* ---------- KPI grid ---------- */
.ed-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 0.7rem;
  margin: 0.2rem 0 0.4rem 0;
}
@media (max-width: 520px) {
  .ed-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.55rem; }
}
@media (max-width: 330px) {
  .ed-grid { grid-template-columns: minmax(0, 1fr); }
}

.ed-card {
  border: 1px solid var(--ed-line);
  border-radius: var(--ed-radius);
  background: var(--ed-surface);
  padding: 0.85rem 0.9rem 0.8rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  min-width: 0;
}
.ed-card .ed-label {
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--ed-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ed-card .ed-value {
  font-size: clamp(1.25rem, 5.2vw, 1.85rem);
  font-weight: 640;
  line-height: 1.1;
  letter-spacing: -0.025em;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.ed-card .ed-delta {
  font-size: 0.78rem;
  font-weight: 550;
  font-variant-numeric: tabular-nums;
  display: flex;
  align-items: baseline;
  gap: 0.3rem;
  flex-wrap: wrap;
}
.ed-card .ed-delta .ed-delta-note {
  font-weight: 400;
  color: var(--ed-muted);
  font-size: 0.72rem;
}
.ed-card .ed-foot {
  font-size: 0.72rem;
  color: var(--ed-muted);
  font-variant-numeric: tabular-nums;
  border-top: 1px dashed var(--ed-line);
  padding-top: 0.4rem;
  margin-top: 0.1rem;
}
.ed-up { color: __POSITIVE__; }
.ed-down { color: __NEGATIVE__; }
.ed-flat { color: var(--ed-muted); }

/* ---------- Section headings ---------- */
.ed-section {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.6rem;
  flex-wrap: wrap;
  margin: 1.6rem 0 0.5rem 0;
  padding-bottom: 0.35rem;
  border-bottom: 1px solid var(--ed-line);
}
.ed-section .ed-section-title {
  font-size: 0.95rem;
  font-weight: 640;
  letter-spacing: -0.01em;
}
.ed-section .ed-section-note {
  font-size: 0.78rem;
  color: var(--ed-muted);
}

/* ---------- Meta strip (refresh state) ---------- */
.ed-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem 1rem;
  font-size: 0.78rem;
  color: var(--ed-muted);
  font-variant-numeric: tabular-nums;
}
.ed-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  border: 1px solid var(--ed-line);
  border-radius: 999px;
  padding: 0.1rem 0.6rem;
  font-size: 0.72rem;
}

/* ---------- Progress bar for period completion ---------- */
.ed-progress {
  height: 4px;
  border-radius: 999px;
  background: var(--ed-line);
  overflow: hidden;
  margin-top: 0.15rem;
}
.ed-progress > div {
  height: 100%;
  background: currentColor;
  opacity: 0.38;
}

/* ---------- Controls ---------- */
/* Full-width, thumb-sized primary action on phones. */
@media (max-width: 640px) {
  [data-testid="stButton"] button { width: 100%; }
  [data-testid="stHorizontalBlock"] { gap: 0.5rem; }
}

div[data-testid="stExpander"] details {
  border: 1px solid var(--ed-line);
  border-radius: var(--ed-radius);
  background: transparent;
}

/* Terminal-style transcript for the admin script runner. */
.ed-console {
  margin: 0;
  border: 1px solid var(--ed-line);
  border-radius: var(--ed-radius);
  background: rgba(128, 128, 128, 0.10);
  padding: 0.7rem 0.85rem;
  max-height: 460px;
  overflow-y: auto;
  /* column-reverse pins the scroll position to the bottom, so live output
     stays in view without any JavaScript. */
  display: flex;
  flex-direction: column-reverse;
}
.ed-console > div {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.78rem;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 640px) {
  .ed-console { max-height: 320px; }
  .ed-console > div { font-size: 0.72rem; }
}
</style>
"""
.replace("__POSITIVE__", POSITIVE)
.replace("__NEGATIVE__", NEGATIVE)
.replace("__ACCENT_DARK__", ACCENT_DARK)
.replace("__ACCENT__", ACCENT)
)


def inject() -> None:
    """Apply the stylesheet once per rerun."""
    st.markdown(_CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Number formatting
# --------------------------------------------------------------------------- #


def format_currency(value: float, symbol: str = "$", decimals: int = 0) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.{decimals}f}"


def format_count(value: float) -> str:
    """Counts are whole unless a configured count column produced fractions."""
    if abs(value - round(value)) < 1e-9:
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def format_value(value: float, is_currency: bool, symbol: str = "$") -> str:
    return format_currency(value, symbol) if is_currency else format_count(value)


def format_compact(value: float, is_currency: bool, symbol: str = "$") -> str:
    """Abbreviated form for dense tables: ``$1.2M``, ``482.3K``."""
    prefix = symbol if is_currency else ""
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if magnitude >= threshold:
            return f"{sign}{prefix}{magnitude / threshold:,.1f}{suffix}"
    return f"{sign}{prefix}{magnitude:,.0f}"


def format_projection(value: float, is_currency: bool, symbol: str = "$") -> str:
    """Like :func:`format_value`, but a projected count is a whole unit.

    "218.8 policies by month end" implies a precision the estimate does not
    have; "219" is the same number, honestly rounded.
    """
    if is_currency:
        return format_currency(value, symbol)
    return format_count(round(value))


def delta_parts(current: float, prior: float) -> tuple[str, str]:
    """``(text, css_class)`` describing the move from ``prior`` to ``current``."""
    change = current - prior
    if prior == 0:
        if current == 0:
            return "no change", "ed-flat"
        return ("new" if change > 0 else "down"), ("ed-up" if change > 0 else "ed-down")
    pct = change / abs(prior) * 100.0
    if abs(pct) < 0.05:
        return "0.0%", "ed-flat"
    arrow = "▲" if change > 0 else "▼"
    css = "ed-up" if change > 0 else "ed-down"
    return f"{arrow} {abs(pct):,.1f}%", css
