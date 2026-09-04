"""The pipeline runner, including the mid-script prompt path."""

import time

import pytest

from app import paths
from app.admin.registry import Pipeline, Step
from app.admin.runner import PipelineRunner, RunState
from app.settings import AdminSettings

TIMEOUT = 45


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(paths, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(paths, "RUN_LOG_DIR", runtime / "logs")


@pytest.fixture
def scripts(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    (root / "ok.py").write_text('print("all good")\n')
    (root / "boom.py").write_text('import sys\nprint("failing")\nsys.exit(3)\n')
    (root / "ask.py").write_text(
        'name = input("Who is running this? ")\n'
        'print(f"hello {name}")\n'
    )
    (root / "args.py").write_text('import sys\nprint("args:", " ".join(sys.argv[1:]))\n')
    return root


@pytest.fixture
def admin(scripts) -> AdminSettings:
    return AdminSettings(scripts_dir=str(scripts), timeout_seconds=30)


def drain(runner: PipelineRunner, answers=None, approve=False):
    """Run to completion, answering prompts and releasing holds as they appear."""
    answers = list(answers or [])
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        snapshot = runner.snapshot()
        if snapshot.state is RunState.PAUSED and approve:
            runner.resume()
        elif snapshot.state is RunState.WAITING_INPUT and answers:
            runner.send_input(answers.pop(0))
            time.sleep(0.3)
        elif not snapshot.state.is_active:
            return snapshot
        time.sleep(0.15)
    raise AssertionError(f"runner did not settle: {runner.snapshot().state}")


def test_successful_run(admin):
    runner = PipelineRunner(Pipeline(steps=[Step(script="ok.py")]), admin)
    runner.start()
    snapshot = drain(runner)

    assert snapshot.state is RunState.FINISHED
    assert "all good" in snapshot.transcript
    assert snapshot.steps[0].exit_code == 0
    assert snapshot.log_path.exists()


def test_a_failing_step_stops_the_pipeline(admin):
    runner = PipelineRunner(
        Pipeline(steps=[Step(script="boom.py"), Step(script="ok.py")]), admin
    )
    runner.start()
    snapshot = drain(runner)

    assert snapshot.state is RunState.FAILED
    assert snapshot.steps[0].exit_code == 3
    # The second step must never have started.
    assert snapshot.steps[1].state is RunState.IDLE
    assert "all good" not in snapshot.transcript


def test_a_prompt_is_detected_and_answerable(admin):
    runner = PipelineRunner(Pipeline(steps=[Step(script="ask.py")]), admin)
    runner.start()
    snapshot = drain(runner, answers=["Dana"])

    assert snapshot.state is RunState.FINISHED
    assert "Who is running this?" in snapshot.transcript
    assert "hello Dana" in snapshot.transcript


def test_pause_before_holds_until_approved(admin):
    runner = PipelineRunner(
        Pipeline(steps=[Step(script="ok.py", pause_before=True)]), admin
    )
    runner.start()

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if runner.snapshot().state is RunState.PAUSED:
            break
        time.sleep(0.1)
    held = runner.snapshot()
    assert held.state is RunState.PAUSED
    assert held.pending_pause == "ok.py"
    assert "all good" not in held.transcript

    runner.resume()
    assert drain(runner).state is RunState.FINISHED


def test_arguments_reach_the_script(admin):
    runner = PipelineRunner(
        Pipeline(steps=[Step(script="args.py", args="--month 2026-09")]), admin
    )
    runner.start()
    assert "args: --month 2026-09" in drain(runner).transcript


def test_a_missing_script_fails_the_run(admin):
    runner = PipelineRunner(Pipeline(steps=[Step(script="gone.py")]), admin)
    runner.start()
    snapshot = drain(runner)
    assert snapshot.state is RunState.FAILED
    assert "was not found" in snapshot.transcript


def test_path_escape_is_refused(admin):
    runner = PipelineRunner(Pipeline(steps=[Step(script="../outside.py")]), admin)
    runner.start()
    assert drain(runner).state is RunState.FAILED


def test_disabled_steps_are_not_planned(admin):
    runner = PipelineRunner(
        Pipeline(steps=[Step(script="ok.py", enabled=False), Step(script="args.py")]),
        admin,
    )
    assert [step.script for step in runner.snapshot().steps] == ["args.py"]


def test_cancel_stops_a_waiting_script(admin):
    runner = PipelineRunner(Pipeline(steps=[Step(script="ask.py")]), admin)
    runner.start()

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        if runner.snapshot().state is RunState.WAITING_INPUT:
            break
        time.sleep(0.1)

    runner.cancel()
    snapshot = drain(runner)
    assert snapshot.state is RunState.CANCELLED
