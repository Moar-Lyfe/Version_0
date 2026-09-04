"""Reusable HTML fragments.

The KPI cards are hand-rendered rather than built from ``st.metric`` because a
CSS grid is the only way to get cards that genuinely reflow on a phone.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

import streamlit as st


@dataclass(frozen=True)
class Card:
    label: str
    value: str
    delta: str = ""
    delta_class: str = "ed-flat"
    delta_note: str = ""
    footnote: str = ""
    progress: float | None = None


def _card_html(card: Card) -> str:
    parts = [
        '<div class="ed-card">',
        f'<div class="ed-label">{escape(card.label)}</div>',
        f'<div class="ed-value">{escape(card.value)}</div>',
    ]
    if card.delta:
        note = (
            f'<span class="ed-delta-note">{escape(card.delta_note)}</span>'
            if card.delta_note
            else ""
        )
        parts.append(
            f'<div class="ed-delta {card.delta_class}">'
            f"<span>{escape(card.delta)}</span>{note}</div>"
        )
    if card.footnote:
        parts.append(f'<div class="ed-foot">{escape(card.footnote)}</div>')
    if card.progress is not None:
        pct = max(0.0, min(card.progress, 1.0)) * 100
        parts.append(
            f'<div class="ed-progress"><div style="width:{pct:.1f}%"></div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def kpi_grid(cards: list[Card]) -> None:
    """Render cards into a responsive grid."""
    if not cards:
        return
    body = "".join(_card_html(card) for card in cards)
    st.markdown(f'<div class="ed-grid">{body}</div>', unsafe_allow_html=True)


def masthead(title: str, organization: str = "") -> None:
    org = f'<span class="ed-org">{escape(organization)}</span>' if organization else ""
    st.markdown(
        f'<div class="ed-masthead"><h1>{escape(title)}</h1>{org}</div>',
        unsafe_allow_html=True,
    )


def subhead(text: str) -> None:
    st.markdown(f'<p class="ed-subhead">{escape(text)}</p>', unsafe_allow_html=True)


def section(title: str, note: str = "") -> None:
    note_html = f'<span class="ed-section-note">{escape(note)}</span>' if note else ""
    st.markdown(
        f'<div class="ed-section"><span class="ed-section-title">{escape(title)}'
        f"</span>{note_html}</div>",
        unsafe_allow_html=True,
    )


def meta_strip(items: list[str]) -> None:
    if not items:
        return
    pills = "".join(f'<span class="ed-pill">{escape(item)}</span>' for item in items)
    st.markdown(f'<div class="ed-meta">{pills}</div>', unsafe_allow_html=True)


def console(text: str, placeholder: str = "No output yet.") -> None:
    """Render a terminal transcript verbatim.

    Two details matter here. Newlines become ``<br>`` so the whole block is a
    single line of HTML: left as real newlines, Streamlit's Markdown pass reads
    a run of ``===`` under a line of text as a setext heading and renders the
    transcript as an ``<h1>``. And a pseudo-terminal emits CRLF, which would
    otherwise double-space every line.
    """
    body = (text or placeholder).replace("\r\n", "\n").replace("\r", "\n")
    html = escape(body).replace("\n", "<br>")
    st.markdown(
        f'<div class="ed-console"><div>{html}</div></div>', unsafe_allow_html=True
    )


def empty_state(title: str, body: str) -> None:
    st.info(f"**{title}**\n\n{body}")


__all__ = [
    "Card",
    "console",
    "empty_state",
    "kpi_grid",
    "masthead",
    "meta_strip",
    "section",
    "subhead",
]
