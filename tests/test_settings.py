"""Configuration loading must never hard-fail: bad input becomes a warning."""

from datetime import date
from pathlib import Path

from app import paths
from app.settings import load_settings


def write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "config.yaml"
    target.write_text(body, encoding="utf-8")
    return target


def test_the_committed_example_parses_cleanly():
    settings = load_settings(paths.CONFIG_EXAMPLE_FILE)
    assert settings.warnings == ()
    assert settings.app.title == "Executive Dashboard"
    assert settings.data.sources
    assert "Sunday" in settings.calendar.exclude_weekdays
    assert settings.projection.periods == ("month", "year")


def test_missing_file_yields_defaults_and_warnings(tmp_path):
    settings = load_settings(tmp_path / "absent.yaml")
    assert settings.app.title == "Executive Dashboard"
    assert any("No data sources" in w for w in settings.warnings)


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    settings = load_settings(write(tmp_path, "app: [unclosed\n"))
    assert any("Could not parse" in w for w in settings.warnings)


def test_unknown_timezone_falls_back_to_utc(tmp_path):
    settings = load_settings(write(tmp_path, "app:\n  timezone: Mars/Olympus\n"))
    assert any("Unknown timezone" in w for w in settings.warnings)
    assert str(settings.app.tzinfo()) == "UTC"


def test_bad_dates_are_dropped_with_a_warning(tmp_path):
    settings = load_settings(
        write(
            tmp_path,
            "calendar:\n"
            "  holidays:\n"
            "    extra_dates: ['2026-12-24', 'christmas eve']\n",
        )
    )
    assert settings.calendar.holidays.extra_dates == (date(2026, 12, 24),)
    assert any("not a YYYY-MM-DD date" in w for w in settings.warnings)


def test_a_source_without_a_path_is_skipped(tmp_path):
    settings = load_settings(
        write(tmp_path, "data:\n  sources:\n    - name: broken\n      glob: '*.xlsx'\n")
    )
    assert settings.data.sources == ()
    assert any("has no 'path'" in w for w in settings.warnings)


def test_relative_paths_resolve_against_the_project_root(tmp_path):
    settings = load_settings(
        write(tmp_path, "data:\n  sources:\n    - name: rel\n      path: data/sample\n")
    )
    resolved = settings.data.sources[0].resolved_path()
    assert resolved.is_absolute()
    assert resolved == paths.PROJECT_ROOT / "data" / "sample"


def test_category_labels_default_when_absent(tmp_path):
    settings = load_settings(write(tmp_path, "app:\n  title: Test\n"))
    assert settings.data.category_1.label == "Category 1"
    assert settings.data.category_2.label == "Category 2"
