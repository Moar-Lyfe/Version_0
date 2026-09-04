"""Typed configuration loading.

``config/config.yaml`` is the single place an operator edits when moving the
dashboard to a new machine. This module reads it, falls back to the committed
example when it is absent, and hands the rest of the app plain dataclasses so no
other module has to know the YAML shape.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from app import paths

# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AppSettings:
    title: str = "Executive Dashboard"
    organization: str = ""
    timezone: str = "America/New_York"
    currency_symbol: str = "$"

    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def now(self) -> datetime:
        return datetime.now(self.tzinfo())

    def today(self) -> date:
        return self.now().date()


@dataclass(frozen=True)
class SourceSettings:
    name: str
    path: str
    glob: str = "*.xlsx"
    sheet: Any = None
    header_row: int = 0
    recursive: bool = False

    def resolved_path(self) -> Path:
        return paths.resolve(self.path)


@dataclass(frozen=True)
class CategorySettings:
    key: str
    label: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class DedupeSettings:
    enabled: bool = False
    keys: tuple[str, ...] = ("policy_id", "date")


@dataclass(frozen=True)
class DataSettings:
    sources: tuple[SourceSettings, ...] = ()
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    category_1: CategorySettings = field(
        default_factory=lambda: CategorySettings("category_1", "Category 1", ())
    )
    category_2: CategorySettings = field(
        default_factory=lambda: CategorySettings("category_2", "Category 2", ())
    )
    other_label: str = "Other"
    web_channel_values: tuple[str, ...] = ()
    web_agent_values: tuple[str, ...] = ()
    dedupe: DedupeSettings = field(default_factory=DedupeSettings)


@dataclass(frozen=True)
class HolidaySettings:
    observed: tuple[str, ...] = ()
    shift_to_next_working_day: bool = True
    extra_dates: tuple[date, ...] = ()
    working_overrides: tuple[date, ...] = ()


@dataclass(frozen=True)
class CalendarSettings:
    exclude_weekdays: tuple[str, ...] = ("Sunday",)
    holidays: HolidaySettings = field(default_factory=HolidaySettings)


@dataclass(frozen=True)
class ProjectionSettings:
    enabled: bool = True
    count_today_as_elapsed: bool = True
    periods: tuple[str, ...] = ("month", "year")


@dataclass(frozen=True)
class AlertRuleSettings:
    """One monitoring rule.

    Three kinds, covering the two ways a metric can be judged:

    * ``threshold`` -- **defined**. You state the number the moving average must
      hold (a floor, or a ceiling).
    * ``relative`` -- **undefined**. No number needed: the short window is
      judged against the metric's own longer baseline, so the rule calibrates
      itself and keeps working as the business grows.
    * ``trend`` -- the same window compared with where it stood N days ago,
      which catches a slide that a stable baseline would mask.
    """

    name: str
    metric: str                       # premium | sales | category_1 | category_2
    kind: str = "relative"            # threshold | relative | trend
    window: int = 15
    baseline: int = 90                # relative: the longer window to judge against
    lookback_days: int = 30           # trend: how far back to compare
    operator: str = "min"             # threshold: min = floor, max = ceiling
    warn: float | None = None         # threshold: per-day value
    critical: float | None = None
    warn_pct: float | None = None     # relative/trend: shortfall that warns
    critical_pct: float | None = None
    enabled: bool = True

    @property
    def is_threshold(self) -> bool:
        return self.kind == "threshold"


@dataclass(frozen=True)
class AnalyticsSettings:
    enabled: bool = True
    # Moving-average windows offered on the page, in days.
    windows: tuple[int, ...] = (15, 30, 45, 90)
    # working_days divides by days the business is actually open, so a window
    # holding three Sundays is not penalised against one holding two.
    basis: str = "working_days"
    default_baseline: int = 90
    # A window shorter than this much history is reported as "not enough data"
    # rather than quietly averaging over a partial window.
    min_history_days: int = 30
    monitor_agents: bool = True
    # Agents below this many sales in the baseline window are too small for a
    # percentage swing to mean anything.
    agent_min_sales: float = 5.0
    preset: str = "standard"
    rules: tuple[AlertRuleSettings, ...] = ()


@dataclass(frozen=True)
class AdminSettings:
    enabled: bool = True
    scripts_dir: str = "scripts"
    scripts_glob: str = "*.py"
    recursive: bool = False
    working_dir: str | None = None
    timeout_seconds: int = 3600
    password_env: str | None = None
    environment: dict[str, str] = field(default_factory=dict)

    def resolved_scripts_dir(self) -> Path:
        return paths.resolve(self.scripts_dir)

    def required_password(self) -> str | None:
        """The expected password, or ``None`` when the panel is ungated."""
        if not self.password_env:
            return None
        value = os.environ.get(self.password_env, "").strip()
        return value or None


@dataclass(frozen=True)
class Settings:
    app: AppSettings
    data: DataSettings
    calendar: CalendarSettings
    projection: ProjectionSettings
    analytics: AnalyticsSettings
    admin: AdminSettings
    source_file: Path
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if str(item).strip())


def _as_date_tuple(value: Any, warnings: list[str], label: str) -> tuple[date, ...]:
    out: list[date] = []
    for item in _as_str_tuple(value):
        try:
            out.append(date.fromisoformat(item.strip()))
        except ValueError:
            warnings.append(f"{label}: '{item}' is not a YYYY-MM-DD date; ignored.")
    return tuple(out)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_category(raw: Any, key: str, default_label: str) -> CategorySettings:
    raw = raw if isinstance(raw, dict) else {}
    return CategorySettings(
        key=key,
        label=str(raw.get("label") or default_label),
        values=_as_str_tuple(raw.get("values")),
    )


VALID_RULE_KINDS = ("threshold", "relative", "trend")
VALID_METRICS = ("premium", "sales", "category_1", "category_2")
VALID_BASES = ("working_days", "calendar_days")


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_rule(raw: Any, index: int, warnings: list[str]) -> AlertRuleSettings | None:
    """One entry of ``analytics.rules``; ``None`` when it cannot be honoured."""
    label = f"analytics.rules[{index}]"
    if not isinstance(raw, dict):
        warnings.append(f"{label} is not a mapping; ignored.")
        return None

    metric = str(raw.get("metric") or "").strip()
    if metric not in VALID_METRICS:
        warnings.append(
            f"{label}: metric {metric or '(missing)'!r} is not one of "
            f"{', '.join(VALID_METRICS)}; rule ignored."
        )
        return None

    kind = str(raw.get("kind") or "relative").strip().lower()
    if kind not in VALID_RULE_KINDS:
        warnings.append(f"{label}: unknown kind {kind!r}; rule ignored.")
        return None

    operator = str(raw.get("operator") or "min").strip().lower()
    if operator not in ("min", "max"):
        warnings.append(f"{label}: operator must be min or max; using min.")
        operator = "min"

    rule = AlertRuleSettings(
        name=str(raw.get("name") or f"{metric} {kind}"),
        metric=metric,
        kind=kind,
        window=_as_int(raw.get("window"), 15),
        baseline=_as_int(raw.get("baseline"), 90),
        lookback_days=_as_int(raw.get("lookback_days"), 30),
        operator=operator,
        warn=_as_float(raw.get("warn")),
        critical=_as_float(raw.get("critical")),
        warn_pct=_as_float(raw.get("warn_pct")),
        critical_pct=_as_float(raw.get("critical_pct")),
        enabled=bool(raw.get("enabled", True)),
    )

    # A rule that cannot fire is a configuration mistake, not a silent no-op.
    if rule.is_threshold and rule.warn is None and rule.critical is None:
        warnings.append(f"{label}: a threshold rule needs 'warn' or 'critical'; ignored.")
        return None
    if not rule.is_threshold and rule.warn_pct is None and rule.critical_pct is None:
        warnings.append(
            f"{label}: a {kind} rule needs 'warn_pct' or 'critical_pct'; ignored."
        )
        return None
    if rule.window < 1:
        warnings.append(f"{label}: window must be at least 1 day; ignored.")
        return None
    if rule.kind == "relative" and rule.baseline <= rule.window:
        warnings.append(
            f"{label}: baseline ({rule.baseline}d) must be longer than window "
            f"({rule.window}d); ignored."
        )
        return None
    return rule


def _parse_analytics(raw: dict[str, Any], warnings: list[str]) -> AnalyticsSettings:
    defaults = AnalyticsSettings()

    windows = tuple(
        w for w in (_as_int(v, 0) for v in (raw.get("windows") or ())) if w > 0
    )
    if raw.get("windows") and not windows:
        warnings.append("analytics.windows held no positive integers; using defaults.")

    basis = str(raw.get("basis") or defaults.basis).strip().lower()
    if basis not in VALID_BASES:
        warnings.append(
            f"analytics.basis {basis!r} is not one of {', '.join(VALID_BASES)}; "
            "using working_days."
        )
        basis = defaults.basis

    rules: list[AlertRuleSettings] = []
    for index, item in enumerate(raw.get("rules") or []):
        rule = _parse_rule(item, index, warnings)
        if rule is not None:
            rules.append(rule)

    return AnalyticsSettings(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        windows=tuple(sorted(set(windows))) or defaults.windows,
        basis=basis,
        default_baseline=_as_int(raw.get("default_baseline"), defaults.default_baseline),
        min_history_days=_as_int(raw.get("min_history_days"), defaults.min_history_days),
        monitor_agents=bool(raw.get("monitor_agents", defaults.monitor_agents)),
        agent_min_sales=_as_float(raw.get("agent_min_sales")) or defaults.agent_min_sales,
        preset=str(raw.get("preset") or defaults.preset).strip().lower(),
        rules=tuple(rules),
    )


def _parse(raw: dict[str, Any], source_file: Path) -> Settings:
    warnings: list[str] = []

    app_raw = raw.get("app") or {}
    app = AppSettings(
        title=str(app_raw.get("title") or "Executive Dashboard"),
        organization=str(app_raw.get("organization") or ""),
        timezone=str(app_raw.get("timezone") or "America/New_York"),
        currency_symbol=str(app_raw.get("currency_symbol") or "$"),
    )
    try:
        ZoneInfo(app.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        warnings.append(f"Unknown timezone '{app.timezone}'; falling back to UTC.")

    data_raw = raw.get("data") or {}

    sources: list[SourceSettings] = []
    for index, item in enumerate(data_raw.get("sources") or []):
        if not isinstance(item, dict) or not item.get("path"):
            warnings.append(f"data.sources[{index}] has no 'path'; skipped.")
            continue
        sources.append(
            SourceSettings(
                name=str(item.get("name") or f"source_{index + 1}"),
                path=str(item["path"]),
                glob=str(item.get("glob") or "*.xlsx"),
                sheet=item.get("sheet"),
                header_row=_as_int(item.get("header_row"), 0),
                recursive=bool(item.get("recursive", False)),
            )
        )
    if not sources:
        warnings.append("No data sources configured -- the dashboard will be empty.")

    columns = {
        canonical: _as_str_tuple(aliases)
        for canonical, aliases in (data_raw.get("columns") or {}).items()
    }
    for required in ("date", "premium"):
        if not columns.get(required):
            warnings.append(f"data.columns.{required} has no aliases configured.")

    categories_raw = data_raw.get("categories") or {}
    web_raw = data_raw.get("web_sales") or {}
    dedupe_raw = data_raw.get("dedupe") or {}

    data = DataSettings(
        sources=tuple(sources),
        columns=columns,
        category_1=_parse_category(
            categories_raw.get("category_1"), "category_1", "Category 1"
        ),
        category_2=_parse_category(
            categories_raw.get("category_2"), "category_2", "Category 2"
        ),
        other_label=str(categories_raw.get("other_label") or "Other"),
        web_channel_values=_as_str_tuple(web_raw.get("channel_values")),
        web_agent_values=_as_str_tuple(web_raw.get("agent_values")),
        dedupe=DedupeSettings(
            enabled=bool(dedupe_raw.get("enabled", False)),
            keys=_as_str_tuple(dedupe_raw.get("keys")) or ("policy_id", "date"),
        ),
    )

    cal_raw = raw.get("calendar") or {}
    hol_raw = cal_raw.get("holidays") or {}
    calendar = CalendarSettings(
        exclude_weekdays=_as_str_tuple(cal_raw.get("exclude_weekdays")),
        holidays=HolidaySettings(
            observed=_as_str_tuple(hol_raw.get("observed")),
            shift_to_next_working_day=bool(
                hol_raw.get("shift_to_next_working_day", True)
            ),
            extra_dates=_as_date_tuple(
                hol_raw.get("extra_dates"), warnings, "calendar.holidays.extra_dates"
            ),
            working_overrides=_as_date_tuple(
                hol_raw.get("working_overrides"),
                warnings,
                "calendar.holidays.working_overrides",
            ),
        ),
    )

    proj_raw = raw.get("projection") or {}
    projection = ProjectionSettings(
        enabled=bool(proj_raw.get("enabled", True)),
        count_today_as_elapsed=bool(proj_raw.get("count_today_as_elapsed", True)),
        periods=_as_str_tuple(proj_raw.get("periods")) or ("month", "year"),
    )

    analytics = _parse_analytics(raw.get("analytics") or {}, warnings)

    admin_raw = raw.get("admin") or {}
    environment = {
        str(k): str(v) for k, v in (admin_raw.get("environment") or {}).items()
    }
    admin = AdminSettings(
        enabled=bool(admin_raw.get("enabled", True)),
        scripts_dir=str(admin_raw.get("scripts_dir") or "scripts"),
        scripts_glob=str(admin_raw.get("scripts_glob") or "*.py"),
        recursive=bool(admin_raw.get("recursive", False)),
        working_dir=(
            str(admin_raw["working_dir"]) if admin_raw.get("working_dir") else None
        ),
        timeout_seconds=_as_int(admin_raw.get("timeout_seconds"), 3600),
        password_env=(
            str(admin_raw["password_env"]) if admin_raw.get("password_env") else None
        ),
        environment=environment,
    )

    return Settings(
        app=app,
        data=data,
        calendar=calendar,
        projection=projection,
        analytics=analytics,
        admin=admin,
        source_file=source_file,
        warnings=tuple(warnings),
    )


def config_path() -> Path:
    """The config file actually in force: the local one, else the example."""
    override = os.environ.get("EXEC_DASH_CONFIG")
    if override:
        return paths.resolve(override)
    if paths.CONFIG_FILE.exists():
        return paths.CONFIG_FILE
    return paths.CONFIG_EXAMPLE_FILE


def load_settings(path: Path | None = None) -> Settings:
    """Read and validate configuration. Never raises on a bad file."""
    target = path or config_path()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return _parse({}, target)
    except yaml.YAMLError as exc:
        settings = _parse({}, target)
        return Settings(
            app=settings.app,
            data=settings.data,
            calendar=settings.calendar,
            projection=settings.projection,
            analytics=settings.analytics,
            admin=settings.admin,
            source_file=target,
            warnings=(f"Could not parse {target.name}: {exc}",) + settings.warnings,
        )
    if not isinstance(raw, dict):
        raw = {}
    return _parse(raw, target)
