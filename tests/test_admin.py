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
    monkeypatch.setattr(paths, "PIPELINES_FILE", runtime / "pipelines.json")
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
        key=registry.SCRIPTS,
        steps=[
            Step(script="01_first.py", pause_before=True, args="--month 2026-09"),
            Step(script="02_second.py", enabled=False),
        ],
    )
    registry.save_pipeline(pipeline)
    loaded = registry.load_pipeline(registry.SCRIPTS)

    assert [step.script for step in loaded.steps] == ["01_first.py", "02_second.py"]
    assert loaded.steps[0].pause_before is True
    assert loaded.steps[0].args == "--month 2026-09"
    # Disabled steps are kept on disk but never run.
    assert [step.script for step in loaded.enabled_steps()] == ["01_first.py"]


def test_each_routine_keeps_its_own_order(admin):
    """The ETL order and the morning order must not overwrite one another."""
    registry.save_pipeline(
        Pipeline(key=registry.ETL, steps=[Step(script="01_first.py")])
    )
    registry.save_pipeline(
        Pipeline(key=registry.MORNING, steps=[Step(script="02_second.py")])
    )

    assert [s.script for s in registry.load_pipeline(registry.ETL).steps] == [
        "01_first.py"
    ]
    assert [s.script for s in registry.load_pipeline(registry.MORNING).steps] == [
        "02_second.py"
    ]


def test_an_unsaved_routine_falls_back_to_its_default(admin):
    scripts = registry.discover(admin)
    etl = registry.load_pipeline(registry.ETL, scripts)
    # The ETL default names a script this fixture does not have, so it is
    # omitted rather than producing a step that cannot run.
    assert etl.steps == []
    assert etl.label == "ETL"

    everything = registry.load_pipeline(registry.SCRIPTS, scripts)
    assert [s.script for s in everything.steps][:2] == ["01_first.py", "02_second.py"]


def test_resetting_a_routine_restores_its_default(admin):
    scripts = registry.discover(admin)
    registry.save_pipeline(
        Pipeline(key=registry.SCRIPTS, steps=[Step(script="02_second.py")])
    )
    assert len(registry.load_pipeline(registry.SCRIPTS, scripts).steps) == 1

    registry.reset_pipeline(registry.SCRIPTS)
    assert len(registry.load_pipeline(registry.SCRIPTS, scripts).steps) == 3


def test_an_order_saved_before_routines_existed_is_carried_over(admin):
    """Upgrade path: the old single-pipeline file becomes the script list."""
    paths.PIPELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.PIPELINE_FILE.write_text(
        json.dumps({"steps": [{"script": "02_second.py", "enabled": True}]}),
        encoding="utf-8",
    )
    loaded = registry.load_pipeline(registry.SCRIPTS, registry.discover(admin))
    assert [s.script for s in loaded.steps] == ["02_second.py"]


def test_corrupt_pipeline_file_degrades_to_the_default(admin):
    paths.PIPELINES_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.PIPELINES_FILE.write_text("{ not json")
    assert registry.load_pipeline(registry.SCRIPTS).steps == []


def test_default_pipeline_follows_filename_order(admin):
    default = registry.default_pipeline(registry.discover(admin))
    assert [step.script for step in default.steps][:2] == ["01_first.py", "02_second.py"]
    assert all(step.enabled for step in default.steps)


def test_every_routine_has_a_label_and_a_description():
    for key in registry.PIPELINE_ORDER:
        label, description = registry.PIPELINE_META[key]
        assert label and description


def test_a_routine_omits_scripts_that_are_not_there(admin):
    """A half-populated scripts folder still gives a usable panel."""
    steps = registry.default_steps(registry.MORNING, registry.discover(admin))
    assert steps == []


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


def test_saved_pipelines_are_readable_json(admin):
    registry.save_pipeline(
        Pipeline(key=registry.ETL, steps=[Step(script="01_first.py")])
    )
    payload = json.loads(paths.PIPELINES_FILE.read_text())
    assert payload["pipelines"]["etl"]["steps"][0]["script"] == "01_first.py"
    assert "saved_at" in payload
