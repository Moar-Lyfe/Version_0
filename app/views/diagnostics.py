"""Diagnostics: prove the plumbing works before trusting a number.

This is the page to open when the dashboard shows something unexpected. It
answers, in order: which config file is in force, which workbooks were found,
how each sheet's columns resolved, what was thrown away, and which working days
the calendar is counting.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from app.core import snapshots
from app.core.calendar_rules import WorkingCalendar
from app.core.periods import build_periods
from app.data import database, postgres_loader, repository
from app.data import schema as S
from app.data.excel_loader import discover_files
from app.settings import POSTGRES, Settings
from app.ui import components
from app.views import widgets


def _configuration(settings: Settings) -> None:
    components.section("Configuration in force", settings.source_file.name)

    if settings.source_file.name == "config.example.yaml":
        st.warning(
            "Running on the committed example configuration. Copy it to "
            "`config/config.yaml` before pointing this at live workbooks — "
            "`config.yaml` is git-ignored, so local paths stay local."
        )
    st.code(str(settings.source_file), language="text")

    for warning in settings.warnings:
        st.warning(warning)

    components.meta_strip(
        [
            f"Reading from {settings.data.source_type}",
            f"Python {sys.version.split()[0]}",
            f"Streamlit {st.__version__}",
            f"pandas {pd.__version__}",
            f"Timezone {settings.app.timezone}",
        ]
    )


def _database(settings: Settings, result) -> None:
    """Connection, table, mapping and load history for the reporting database."""
    pg = settings.data.postgres
    components.section("Reporting database", pg.qualified_table())

    probe = database.check(pg)
    if probe.ok:
        st.success(probe.message)
    else:
        st.error(probe.message)

    pills = [pg.describe(), f"Table {pg.qualified_table()}"]
    if probe.server_version:
        pills.append(probe.server_version)
    pills.append(
        f"Password from {pg.password_env}"
        if pg.password_env
        else "Password from libpq (~/.pgpass or PGPASSWORD)"
    )
    if pg.where:
        pills.append(f"Filter: {pg.where}")
    components.meta_strip(pills)

    if not probe.ok:
        return

    report = result.files[0] if result.files else None
    if report and report.error:
        st.error(report.error)

    if report and report.matched_columns:
        rows = [
            {
                "Canonical field": canonical,
                "Table column": report.matched_columns.get(canonical) or "— not mapped —",
                "Status": "mapped" if canonical in report.matched_columns else "default",
            }
            for canonical in postgres_loader.SELECTED
        ]
        widgets.dataframe(pd.DataFrame(rows), hide_index=True)
        if report.missing_columns:
            st.caption(
                "Unmapped fields fall back to defaults (premium 0, one sale per "
                "row, agent 'Unassigned'). Map them under `data.postgres.columns` "
                "in config.yaml."
            )

    if report and report.headers:
        with st.expander("Columns present in the table", expanded=False):
            st.code("\n".join(report.headers), language="text")

    st.caption(
        "Load history, table sizes and a query console are on the **Database** "
        "page."
    )


def _sources(settings: Settings) -> None:
    components.section("Sources", f"{len(settings.data.sources)} configured")
    rows = []
    for source in settings.data.sources:
        root = source.resolved_path()
        files = discover_files(source)
        rows.append(
            {
                "Source": source.name,
                "Resolved path": str(root),
                "Exists": "yes" if root.exists() else "NO",
                "Pattern": source.glob,
                "Sheet": "first" if source.sheet is None else str(source.sheet),
                "Header row": source.header_row,
                "Files matched": len(files),
            }
        )
    if rows:
        widgets.dataframe(pd.DataFrame(rows), hide_index=True)


def _files(result) -> None:
    components.section("Workbooks read", f"{result.row_count:,} rows total")
    if not result.files:
        components.empty_state(
            "No workbooks matched",
            "Check the resolved paths above — a wrong drive letter or an "
            "un-mounted share is the usual cause.",
        )
        return

    rows = []
    for report in result.files:
        rows.append(
            {
                "File": Path(report.path).name,
                "Sheet": report.sheet,
                "Rows read": report.rows_read,
                "Rows kept": report.rows_kept,
                "Skipped (bad date)": report.rows_without_date,
                "Modified": (
                    report.modified_at.strftime("%Y-%m-%d %H:%M")
                    if report.modified_at
                    else "—"
                ),
                "Error": report.error or "",
            }
        )
    widgets.dataframe(pd.DataFrame(rows), hide_index=True)


def _column_mapping(settings: Settings, result) -> None:
    components.section("Column mapping", "canonical field → workbook header")
    readable = [report for report in result.files if not report.error]
    if not readable:
        return

    labels = [f"{Path(r.path).name} · {r.sheet}" for r in readable]
    selection = st.selectbox("Sheet", labels, index=0)
    report = readable[labels.index(selection)]

    rows = []
    for canonical in settings.data.columns:
        matched = report.matched_columns.get(canonical)
        rows.append(
            {
                "Canonical field": canonical,
                "Matched header": matched or "— not found —",
                "Status": "mapped" if matched else "unmapped",
            }
        )
    widgets.dataframe(pd.DataFrame(rows), hide_index=True)

    if report.missing_columns:
        st.caption(
            "Unmapped fields fall back to defaults (premium 0, one sale per row, "
            "agent 'Unassigned'). Add the real header to `data.columns` in "
            "config.yaml to fix."
        )
    with st.expander("Headers found in this sheet", expanded=False):
        st.code("\n".join(report.headers) or "(none)", language="text")


def _categories(settings: Settings, result) -> None:
    components.section("Category matching", "values that fell into Other")
    if not result.unmatched_categories:
        st.success("Every non-blank category value matched Category 1 or Category 2.")
        return

    frame = pd.DataFrame(
        sorted(result.unmatched_categories.items(), key=lambda kv: -kv[1]),
        columns=["Value in workbook", "Rows"],
    )
    widgets.dataframe(frame, hide_index=True)
    st.caption(
        "These rows still count toward Premium and Total Sales. To pull one into "
        "a reported category, add it under `data.categories.category_1.values` "
        "(or `category_2`) in config.yaml."
    )


def _web_sales(settings: Settings, result) -> None:
    components.section("Web sales detection", "rows matched by channel or agent")
    if result.frame.empty:
        return
    web = int(result.frame[S.IS_WEB].sum())
    total = len(result.frame)
    share = web / total * 100 if total else 0.0
    components.meta_strip(
        [
            f"{web:,} of {total:,} rows ({share:.1f}%) flagged as web",
            f"Channels: {', '.join(settings.data.web_channel_values) or 'none'}",
            f"Agents: {', '.join(settings.data.web_agent_values) or 'none'}",
        ]
    )
    if S.CHANNEL in result.frame.columns:
        counts = (
            result.frame.groupby([S.CHANNEL, S.IS_WEB])
            .size()
            .reset_index(name="Rows")
            .rename(columns={S.CHANNEL: "Channel", S.IS_WEB: "Counted as web"})
        )
        widgets.dataframe(counts, hide_index=True)


def _calendar(settings: Settings) -> None:
    components.section("Working calendar", "what the projections divide by")
    calendar = WorkingCalendar.from_settings(settings.calendar)
    today = settings.app.today()
    periods = build_periods(today)

    month = periods["month"]
    year = periods["year"]
    components.meta_strip(
        [
            f"This month: {calendar.working_days_between(month.full_start, month.full_end)} working days",
            f"This year: {calendar.working_days_between(year.full_start, year.full_end)} working days",
            f"Excluded weekdays: {', '.join(settings.calendar.exclude_weekdays) or 'none'}",
        ]
    )

    holidays = calendar.holidays_for_year(today.year)
    widgets.dataframe(
        pd.DataFrame(
            [
                {"Date": day.strftime("%Y-%m-%d (%a)"), "Observance": name}
                for day, name in sorted(holidays.items())
            ]
        ),
        hide_index=True,
    )

    with st.expander("Non-working days in the next 30 days", expanded=False):
        skipped = calendar.non_working_days_between(today, today + timedelta(days=30))
        if not skipped:
            st.caption("Every day in the next 30 is a working day.")
        else:
            widgets.dataframe(
                pd.DataFrame(
                    [
                        {"Date": day.strftime("%Y-%m-%d (%a)"), "Reason": reason}
                        for day, reason in skipped
                    ]
                ),
                hide_index=True,
            )


def _snapshots(settings: Settings, result) -> None:
    """The archive of what was reported, and anything that has since moved."""
    components.section("Snapshots", "what was reported, and what changed since")

    if not settings.snapshots.enabled:
        st.caption(
            "Snapshots are off. Set `snapshots.enabled: true` in config.yaml and "
            "schedule `tools/snapshot_kpis.py` to keep a record of what was "
            "reported each day."
        )
        return

    taken = snapshots.snapshot_dates(settings.snapshots)
    if not taken:
        st.info(
            "No snapshots on file yet. Every figure here is recomputed from the "
            "current workbooks, so a corrected row silently changes what last "
            "month *was*. Run `python tools/snapshot_kpis.py` nightly to keep a "
            "record of what was actually reported."
        )
        return

    components.meta_strip(
        [
            f"{len(taken)} snapshot(s)",
            f"Earliest {taken[0]}",
            f"Latest {taken[-1]}",
            f"Stored in {settings.snapshots.resolved_directory()}",
        ]
    )

    restatements = snapshots.find_restatements(
        result.frame, settings.snapshots, settings.app.today()
    )
    if not restatements:
        st.success(
            "No restatements: every day recorded in the last snapshot still "
            "matches the current workbooks."
        )
    else:
        st.warning(
            f"{len(restatements)} day(s) have changed since the snapshot of "
            f"{restatements[0].as_of}. A closed day's figure moving is usually a "
            "corrected row or a re-export — worth knowing before anyone asks why "
            "last month is different."
        )
        widgets.dataframe(
            pd.DataFrame(
                [
                    {
                        "Date": r.day.strftime("%Y-%m-%d"),
                        "Metric": r.metric,
                        "Was": round(r.was, 2),
                        "Now": round(r.now, 2),
                        "Change": round(r.delta, 2),
                        "Change %": (
                            "—" if r.delta_pct is None else f"{r.delta_pct:+.1f}%"
                        ),
                    }
                    for r in restatements[:100]
                ]
            ),
            hide_index=True,
        )

    with st.expander("What was reported on a given day", expanded=False):
        chosen = st.selectbox(
            "Snapshot date", list(reversed(taken)), format_func=lambda d: d.isoformat()
        )
        rows = snapshots.as_reported(settings.snapshots, chosen)
        if rows.empty:
            st.caption("No rows recorded for that date.")
        else:
            pivot = rows.pivot_table(
                index="period_label", columns="metric_label", values="value",
                sort=False, aggfunc="first",
            ).reset_index()
            widgets.dataframe(pivot, hide_index=True)


def render(settings: Settings) -> None:
    components.masthead("Diagnostics", settings.app.organization)
    components.subhead("Where the numbers come from, and what was dropped on the way.")

    result = repository.get_dataset(settings)

    if st.button(repository.refresh_label(settings), type="primary"):
        repository.request_refresh()
        st.rerun()
    components.meta_strip(
        [
            f"Last read {repository.last_refresh_display(result, settings.app.tzinfo())}",
            f"{result.row_count:,} rows from "
            f"{repository.source_description(settings, result)}",
        ]
    )

    _configuration(settings)
    if settings.data.source_type == POSTGRES:
        _database(settings, result)
    else:
        _sources(settings)
        _files(result)
        _column_mapping(settings, result)
    _categories(settings, result)
    _web_sales(settings, result)
    _calendar(settings)
    _snapshots(settings, result)

    if not result.frame.empty:
        components.section("Sample rows", "first 50 rows as the app sees them")
        widgets.dataframe(result.frame.head(50), hide_index=True)
