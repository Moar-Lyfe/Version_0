"""Script discovery, run-order persistence, and the path guard."""

import json

import pytest

from app import paths
from app.admin import registry
from app.admin.registry import Pipeline, Step
from app.settings import AdminSettings


@pytest.fixture
def scripts_dir(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    (root / "01_first.py").write_text('"""Does the first thing."""\nprint("hi")\n')
    (root / "02_second.py").write_text('"""Does the second thing.\n\nMore detail."""\n')
    (root / "_helper.py").write_text('"""Hidden helper."""\n')
    (root / "notes.txt").write_text("not a script")
    (root / "broken.py").write_text("def (:\n")  # unparseable, must not crash
    return root


@pytest.fixture
def admin(scripts_dir) -> AdminSettings:
    return AdminSettings(scripts_dir=str(scripts_dir))


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(paths, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(paths, "PIPELINE_FILE", runtime / "pipeline.json")
    monkeypatch.setattr(paths, "OVERRIDE_FILE", runtime / "admin_override.json")
    monkeypatch.setattr(paths, "RUN_LOG_DIR", runtime / "logs")


def test_discovery_skips_hidden_and_non_python(admin):
    found = registry.discover(admin)
    assert [info.relative for info in found] == ["01_first.py", "02_second.py", "broken.py"]


def test_description_comes_from_the_docstring_first_line(admin):
    by_name = {info.relative: info for info in registry.discover(admin)}
    assert by_name["01_first.py"].description == "Does the first thing."
    assert by_name["02_second.py"].description == "Does the second thing."
    # A file that will not parse simply has no description; it is still listed.
    assert by_name["broken.py"].description == ""


def test_missing_directory_returns_nothing(tmp_path):
    assert registry.discover(AdminSettings(scripts_dir=str(tmp_path / "nope"))) == []


def test_resolve_script_refuses_to_escape_the_directory(admin, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text("print('nope')")
    assert registry.resolve_script(admin, "01_first.py") is not None
    assert registry.resolve_script(admin, "../evil.py") is None
    assert registry.resolve_script(admin, "/etc/passwd") is None
    assert registry.resolve_script(admin, "does_not_exist.py") is None


def test_pipeline_round_trips(admin):
    pipeline = Pipeline(
        steps=[
            Step(script="01_first.py", pause_before=True, args="--month 2026-09"),
            Step(script="02_second.py", enabled=False),
        ]
    )
    registry.save_pipeline(pipeline)
    loaded = registry.load_pipeline()

    assert [step.script for step in loaded.steps] == ["01_first.py", "02_second.py"]
    assert loaded.steps[0].pause_before is True
    assert loaded.steps[0].args == "--month 2026-09"
    # Disabled steps are kept on disk but never run.
    assert [step.script for step in loaded.enabled_steps()] == ["01_first.py"]


def test_corrupt_pipeline_file_degrades_to_empty():
    paths.PIPELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.PIPELINE_FILE.write_text("{ not json")
    assert registry.load_pipeline().steps == []


def test_default_pipeline_follows_filename_order(admin):
    default = registry.default_pipeline(registry.discover(admin))
    assert [step.script for step in default.steps][:2] == ["01_first.py", "02_second.py"]
    assert all(step.enabled for step in default.steps)


def test_directory_override_wins_over_config(admin, tmp_path):
    assert registry.effective_admin_settings(admin).scripts_dir == admin.scripts_dir

    registry.save_dir_override(str(tmp_path / "elsewhere"))
    assert registry.load_dir_override() == str(tmp_path / "elsewhere")
    assert registry.effective_admin_settings(admin).scripts_dir == str(
        tmp_path / "elsewhere"
    )

    registry.save_dir_override(None)
    assert registry.load_dir_override() is None
    assert registry.effective_admin_settings(admin).scripts_dir == admin.scripts_dir


def test_password_gate_reads_the_environment(monkeypatch):
    assert AdminSettings().required_password() is None

    gated = AdminSettings(password_env="TEST_DASH_PASSWORD")
    monkeypatch.delenv("TEST_DASH_PASSWORD", raising=False)
    assert gated.required_password() is None       # unset -> panel stays open
    monkeypatch.setenv("TEST_DASH_PASSWORD", "s3cret")
    assert gated.required_password() == "s3cret"


def test_saved_pipeline_is_readable_json(admin):
    registry.save_pipeline(Pipeline(steps=[Step(script="01_first.py")]))
    payload = json.loads(paths.PIPELINE_FILE.read_text())
    assert payload["steps"][0]["script"] == "01_first.py"
    assert "saved_at" in payload
