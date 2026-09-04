"""Read every configured workbook into one canonical table.

The loader is deliberately forgiving: a workbook with a missing optional column,
a stray total row, or a text date does not break the dashboard. Everything it
could not interpret is recorded in :class:`LoadResult` and surfaced on the
Diagnostics page instead of being thrown away silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.data import schema as S
from app.settings import DataSettings, SourceSettings


@dataclass
class FileReport:
    """What happened to one sheet of one workbook."""

    path: str
    sheet: str
    rows_read: int = 0
    rows_kept: int = 0
    rows_without_date: int = 0
    matched_columns: dict[str, str] = field(default_factory=dict)
    missing_columns: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    modified_at: datetime | None = None
    error: str | None = None


@dataclass
class LoadResult:
    """The dataset plus a full account of how it was built."""

    frame: pd.DataFrame
    loaded_at: datetime
    files: list[FileReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmatched_categories: dict[str, int] = field(default_factory=dict)
    duplicates_removed: int = 0

    @property
    def row_count(self) -> int:
        return int(len(self.frame))

    @property
    def file_count(self) -> int:
        return len({report.path for report in self.files if report.error is None})

    @property
    def has_errors(self) -> bool:
        return any(report.error for report in self.files)

    def date_range(self) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        if self.frame.empty:
            return None
        return self.frame[S.DATE].min(), self.frame[S.DATE].max()


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

# Excel writes a lock file alongside an open workbook; reading it fails.
_TEMP_PREFIXES = ("~$", ".~")


def discover_files(source: SourceSettings) -> list[Path]:
    """Workbooks matching one configured source, sorted for stable ordering."""
    root = source.resolved_path()
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    pattern = f"**/{source.glob}" if source.recursive else source.glob
    return sorted(
        path
        for path in root.glob(pattern)
        if path.is_file() and not path.name.startswith(_TEMP_PREFIXES)
    )


def fingerprint(sources: tuple[SourceSettings, ...]) -> str:
    """A cheap signature of the data on disk.

    Feeding this to the cache means the dashboard notices a replaced workbook by
    itself, and the Refresh button simply recomputes it.
    """
    parts: list[str] = []
    for source in sources:
        for path in discover_files(source):
            try:
                stat = path.stat()
                parts.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                parts.append(f"{path}:missing")
    return "|".join(parts) or "empty"


# --------------------------------------------------------------------------- #
# Column resolution
# --------------------------------------------------------------------------- #


def resolve_columns(
    headers: list[Any], aliases: dict[str, tuple[str, ...]]
) -> dict[str, str]:
    """Map canonical field -> actual header, using the configured aliases.

    Exact (normalised) alias matches are preferred. Only if none is found does it
    fall back to a whole-word containment match, so ``"Agent Name (Primary)"``
    still resolves to ``agent`` without ``"Premium Paid Date"`` hijacking
    ``date``.
    """
    lookup: dict[str, str] = {}
    for header in headers:
        key = S.normalise_key(header)
        if key and key not in lookup:
            lookup[key] = header

    resolved: dict[str, str] = {}
    claimed: set[str] = set()

    for canonical, alias_list in aliases.items():
        for alias in alias_list:
            key = S.normalise_key(alias)
            if key in lookup and lookup[key] not in claimed:
                resolved[canonical] = lookup[key]
                claimed.add(lookup[key])
                break

    for canonical, alias_list in aliases.items():
        if canonical in resolved:
            continue
        for alias in alias_list:
            key = S.normalise_key(alias)
            if not key:
                continue
            for header_key, header in lookup.items():
                if header in claimed:
                    continue
                words = header_key.split()
                alias_words = key.split()
                if any(
                    words[i : i + len(alias_words)] == alias_words
                    for i in range(len(words) - len(alias_words) + 1)
                ):
                    resolved[canonical] = header
                    claimed.add(header)
                    break
            if canonical in resolved:
                break

    return resolved


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def _normalise_sheet(
    raw: pd.DataFrame,
    settings: DataSettings,
    report: FileReport,
    header_row: int = 0,
) -> pd.DataFrame:
    columns = resolve_columns(list(raw.columns), settings.columns)
    report.matched_columns = dict(columns)
    report.headers = tuple(str(c) for c in raw.columns)
    report.missing_columns = tuple(
        name for name in settings.columns if name not in columns
    )

    if S.DATE not in columns:
        report.error = (
            "No date column found. Add the workbook's date header to "
            "data.columns.date in config.yaml."
        )
        return S.empty_frame()

    out = pd.DataFrame(index=raw.index)

    dates = pd.to_datetime(raw[columns[S.DATE]], errors="coerce")
    out[S.DATE] = dates.dt.normalize()

    if S.PREMIUM in columns:
        out[S.PREMIUM] = [S.to_number(v) for v in raw[columns[S.PREMIUM]]]
    else:
        out[S.PREMIUM] = 0.0

    if "count" in columns:
        counts = [S.to_number(v) for v in raw[columns["count"]]]
        # A blank count column still means one sale, not zero.
        out[S.SALES] = [c if c else 1.0 for c in counts]
    else:
        out[S.SALES] = 1.0

    if S.AGENT in columns:
        out[S.AGENT] = [
            S.clean_text(v, S.UNASSIGNED_AGENT) for v in raw[columns[S.AGENT]]
        ]
    else:
        out[S.AGENT] = S.UNASSIGNED_AGENT

    if S.CATEGORY in columns:
        out[S.CATEGORY] = [S.clean_text(v, "") for v in raw[columns[S.CATEGORY]]]
    else:
        out[S.CATEGORY] = ""

    out[S.CHANNEL] = (
        [S.clean_text(v, "") for v in raw[columns[S.CHANNEL]]]
        if S.CHANNEL in columns
        else ""
    )
    out[S.POLICY_ID] = (
        [S.clean_text(v, "") for v in raw[columns[S.POLICY_ID]]]
        if S.POLICY_ID in columns
        else ""
    )

    out[S.SOURCE_FILE] = report.path
    out[S.SOURCE_SHEET] = report.sheet
    # The frame index is the zero-based offset below the header row, so
    # +header_row+2 gives the 1-based row number an operator sees in Excel.
    out[S.SOURCE_ROW] = [int(i) + header_row + 2 for i in raw.index]

    report.rows_read = int(len(out))
    undated = int(out[S.DATE].isna().sum())
    report.rows_without_date = undated
    out = out.loc[out[S.DATE].notna()]
    report.rows_kept = int(len(out))

    # Category bucketing and web detection are shared with the database reader.
    return S.derive(out, settings)


def _read_sheets(path: Path, source: SourceSettings) -> dict[str, pd.DataFrame]:
    """Read the configured sheet(s) from one workbook, keyed by real sheet name.

    Resolving through :class:`pandas.ExcelFile` rather than passing an index
    straight to ``read_excel`` means Diagnostics can report "Production" instead
    of "0" when the config just says "first sheet".
    """
    sheet = source.sheet
    with pd.ExcelFile(path) as book:
        names = list(book.sheet_names)
        if not names:
            return {}

        if isinstance(sheet, str) and sheet.strip() == "*":
            targets = names
        elif sheet is None or (isinstance(sheet, str) and not sheet.strip()):
            targets = names[:1]
        elif isinstance(sheet, bool):  # YAML `sheet: false` -- treat as first
            targets = names[:1]
        elif isinstance(sheet, int) or (
            isinstance(sheet, str) and sheet.strip().lstrip("-").isdigit()
        ):
            index = int(sheet)
            if not -len(names) <= index < len(names):
                raise ValueError(
                    f"Sheet index {index} is out of range; the workbook has "
                    f"{len(names)} sheet(s): {', '.join(names)}"
                )
            targets = [names[index]]
        else:
            if sheet not in names:
                raise ValueError(
                    f"Sheet {sheet!r} not found; available: {', '.join(names)}"
                )
            targets = [str(sheet)]

        return {
            name: book.parse(name, header=source.header_row, dtype=object)
            for name in targets
        }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def load_dataset(settings: DataSettings) -> LoadResult:
    """Read every configured workbook and return one canonical dataset."""
    loaded_at = datetime.now().astimezone()

    reports: list[FileReport] = []
    warnings: list[str] = []
    frames: list[pd.DataFrame] = []

    for source in settings.sources:
        root = source.resolved_path()
        files = discover_files(source)
        if not files:
            warnings.append(
                f"Source '{source.name}': no files matched {source.glob} in {root}"
            )
            continue

        for path in files:
            try:
                modified_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                modified_at = None
            try:
                sheets = _read_sheets(path, source)
            except Exception as exc:  # noqa: BLE001 - surfaced in Diagnostics
                reports.append(
                    FileReport(
                        path=str(path),
                        sheet="-",
                        modified_at=modified_at,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            for sheet_name, raw in sheets.items():
                report = FileReport(
                    path=str(path), sheet=sheet_name, modified_at=modified_at
                )
                if raw is None or raw.empty:
                    report.error = "Sheet is empty."
                    reports.append(report)
                    continue
                raw = raw.dropna(how="all")
                frame = _normalise_sheet(
                    raw, settings, report, header_row=source.header_row
                )
                reports.append(report)
                if not frame.empty:
                    frames.append(frame)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
    else:
        combined = S.empty_frame()

    duplicates_removed = 0
    if settings.dedupe.enabled and not combined.empty:
        keys = [key for key in settings.dedupe.keys if key in combined.columns]
        if keys:
            before = len(combined)
            combined = combined.drop_duplicates(subset=keys, keep="first")
            duplicates_removed = before - len(combined)

    if not combined.empty:
        combined = combined.sort_values(S.DATE, kind="stable").reset_index(drop=True)

    unmatched: dict[str, int] = {}
    if not combined.empty:
        other = combined.loc[combined[S.CATEGORY_KEY] == S.CATEGORY_OTHER, S.CATEGORY]
        counts = other.loc[other.astype(str).str.len() > 0].value_counts()
        unmatched = {str(k): int(v) for k, v in counts.items()}

    for report in reports:
        if report.error:
            warnings.append(f"{Path(report.path).name} [{report.sheet}]: {report.error}")
        elif report.rows_without_date:
            warnings.append(
                f"{Path(report.path).name} [{report.sheet}]: "
                f"{report.rows_without_date} row(s) skipped -- unreadable date."
            )

    return LoadResult(
        frame=combined,
        loaded_at=loaded_at,
        files=reports,
        warnings=warnings,
        unmatched_categories=unmatched,
        duplicates_removed=duplicates_removed,
    )
