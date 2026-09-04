"""Run pipeline scripts and let a human answer their prompts.

The hard requirement is that a script which stops halfway to ask a question --
``answer = input("Proceed? [y/N] ")`` -- must be answerable from the browser
exactly as it would be from a terminal.

How it works:

* Each script runs in a background thread as a child process, attached to a
  pseudo-terminal where the platform has one (macOS, Linux). ``input()`` then
  behaves normally: the prompt is written and flushed immediately, and the
  answer we send back is echoed into the transcript just as a terminal would
  echo typing. On Windows, where there is no ``pty``, plain pipes plus ``-u``
  (unbuffered) give the same behaviour.
* The reader loop watches for the process going quiet with an unterminated
  line -- which is exactly what a prompt looks like -- and flips the run into
  ``WAITING_INPUT`` so the UI can surface an answer box. Input can be sent at
  any time regardless, so an undetected prompt is never a dead end.
* All shared state sits behind a lock and is read through :meth:`snapshot`,
  because Streamlit reads it from the main thread while the worker writes.

The same pipeline can be run from a real terminal with ``tools/run_pipeline.py``
when nobody wants a browser in the loop.
"""

from __future__ import annotations

import errno
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from app import paths
from app.admin.registry import Pipeline, Step, resolve_script
from app.settings import AdminSettings

try:  # POSIX only
    import pty
    import select

    _HAS_PTY = True
except ImportError:  # pragma: no cover - Windows
    _HAS_PTY = False

# Quiet period after which an unterminated line is treated as a prompt.
PROMPT_IDLE_SECONDS = 0.45
# Cap the in-memory transcript so a chatty script cannot exhaust the browser.
MAX_TRANSCRIPT_CHARS = 400_000


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in (RunState.RUNNING, RunState.WAITING_INPUT, RunState.PAUSED)


@dataclass
class StepRun:
    """Outcome of one script in the run."""

    index: int
    script: str
    state: RunState = RunState.IDLE
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        if not self.started_at:
            return None
        end = self.finished_at or datetime.now().astimezone()
        return (end - self.started_at).total_seconds()


@dataclass
class Snapshot:
    """Immutable view of the run, safe to read from the Streamlit thread."""

    state: RunState
    transcript: str
    steps: list[StepRun]
    current_index: int
    started_at: datetime | None
    finished_at: datetime | None
    log_path: Path | None
    pending_pause: str | None


class PipelineRunner:
    """Executes a :class:`Pipeline` one script at a time."""

    def __init__(self, pipeline: Pipeline, settings: AdminSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._continue = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._write_fd: int | None = None

        self._state = RunState.IDLE
        self._transcript: list[str] = []
        self._steps = [
            StepRun(index=i, script=step.script)
            for i, step in enumerate(pipeline.enabled_steps())
        ]
        self._plan: list[Step] = list(pipeline.enabled_steps())
        self._current = -1
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None
        self._pending_pause: str | None = None
        self._log_path: Path | None = None
        self._log_handle = None

    # -- public API -------------------------------------------------------- #

    @property
    def has_work(self) -> bool:
        return bool(self._plan)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        paths.ensure_runtime_dirs()
        stamp = datetime.now().astimezone()
        self._log_path = paths.RUN_LOG_DIR / f"run_{stamp:%Y%m%d_%H%M%S}.log"
        try:
            self._log_handle = self._log_path.open("a", encoding="utf-8")
        except OSError:
            self._log_handle = None
        with self._lock:
            self._state = RunState.RUNNING
            self._started_at = stamp
        self._thread = threading.Thread(
            target=self._run, name="pipeline-runner", daemon=True
        )
        self._thread.start()

    def send_input(self, text: str) -> bool:
        """Write ``text`` plus a newline to the running script's stdin."""
        payload = (text + "\n").encode()
        with self._lock:
            write_fd = self._write_fd
            process = self._process
        if write_fd is not None:
            try:
                os.write(write_fd, payload)
            except OSError:
                return False
        elif process is not None and process.stdin is not None:
            try:
                process.stdin.write(payload)
                process.stdin.flush()
            except (OSError, ValueError):
                return False
        else:
            return False
        with self._lock:
            if self._state is RunState.WAITING_INPUT:
                self._state = RunState.RUNNING
            if write_fd is None:
                # Pipes do not echo, so mirror the answer into the transcript.
                self._append(text + "\n")
        return True

    def resume(self) -> None:
        """Release a hold placed by a ``pause_before`` step."""
        self._continue.set()

    def cancel(self) -> None:
        """Stop the current script and abandon the rest of the run."""
        self._cancel.set()
        self._continue.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                state=self._state,
                transcript="".join(self._transcript),
                steps=[
                    StepRun(
                        index=s.index,
                        script=s.script,
                        state=s.state,
                        exit_code=s.exit_code,
                        started_at=s.started_at,
                        finished_at=s.finished_at,
                        error=s.error,
                    )
                    for s in self._steps
                ],
                current_index=self._current,
                started_at=self._started_at,
                finished_at=self._finished_at,
                log_path=self._log_path,
                pending_pause=self._pending_pause,
            )

    # -- internals --------------------------------------------------------- #

    def _append(self, text: str) -> None:
        """Append to the transcript. Caller must hold the lock."""
        self._transcript.append(text)
        if self._log_handle:
            try:
                self._log_handle.write(text)
                self._log_handle.flush()
            except (OSError, ValueError):
                pass
        # Trim from the front so the tail -- the useful part -- always survives.
        total = sum(len(chunk) for chunk in self._transcript)
        while total > MAX_TRANSCRIPT_CHARS and len(self._transcript) > 1:
            total -= len(self._transcript.pop(0))

    def _emit(self, text: str) -> None:
        with self._lock:
            self._append(text)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self._settings.environment.items()})
        # Unbuffered output is what makes prompts visible before the answer.
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _working_dir(self, script_path: Path) -> Path:
        if self._settings.working_dir:
            return paths.resolve(self._settings.working_dir)
        return script_path.parent

    def _run(self) -> None:
        try:
            for position, step in enumerate(self._plan):
                if self._cancel.is_set():
                    self._finish(RunState.CANCELLED)
                    return

                with self._lock:
                    self._current = position

                if step.pause_before:
                    self._hold(step.script)
                    if self._cancel.is_set():
                        self._finish(RunState.CANCELLED)
                        return

                ok = self._run_step(position, step)
                if not ok:
                    self._finish(
                        RunState.CANCELLED if self._cancel.is_set() else RunState.FAILED
                    )
                    return

            self._finish(RunState.FINISHED)
        except Exception as exc:  # noqa: BLE001 - never kill the worker silently
            self._emit(f"\n[runner error] {type(exc).__name__}: {exc}\n")
            self._finish(RunState.FAILED)

    def _hold(self, script: str) -> None:
        self._continue.clear()
        with self._lock:
            self._state = RunState.PAUSED
            self._pending_pause = script
            self._append(f"\n--- Paused before {script}. Waiting for approval. ---\n")
        self._continue.wait()
        with self._lock:
            self._pending_pause = None
            if not self._cancel.is_set():
                self._state = RunState.RUNNING
                self._append(f"--- Approved. Running {script}. ---\n")

    def _run_step(self, position: int, step: Step) -> bool:
        record = self._steps[position]
        script_path = resolve_script(self._settings, step.script)

        if script_path is None:
            message = (
                f"[skipped] {step.script} was not found inside "
                f"{self._settings.resolved_scripts_dir()}"
            )
            with self._lock:
                record.state = RunState.FAILED
                record.error = message
                record.finished_at = datetime.now().astimezone()
                self._append(f"\n{message}\n")
            return False

        try:
            extra_args = shlex.split(step.args) if step.args.strip() else []
        except ValueError as exc:
            with self._lock:
                record.state = RunState.FAILED
                record.error = f"Could not parse arguments: {exc}"
                self._append(f"\n[skipped] {step.script}: {record.error}\n")
            return False

        command = [sys.executable, "-u", str(script_path), *extra_args]
        started = datetime.now().astimezone()
        with self._lock:
            record.state = RunState.RUNNING
            record.started_at = started
            self._append(
                f"\n{'=' * 62}\n"
                f"[{position + 1}/{len(self._plan)}] {step.script}"
                f"{' ' + step.args if step.args else ''}\n"
                f"started {started:%H:%M:%S} · cwd {self._working_dir(script_path)}\n"
                f"{'=' * 62}\n"
            )

        exit_code = self._execute(command, self._working_dir(script_path))

        finished = datetime.now().astimezone()
        with self._lock:
            record.exit_code = exit_code
            record.finished_at = finished
            if self._cancel.is_set():
                record.state = RunState.CANCELLED
                self._append(f"\n--- {step.script} cancelled. ---\n")
            elif exit_code == 0:
                record.state = RunState.FINISHED
                self._append(
                    f"\n--- {step.script} finished in "
                    f"{record.duration_seconds:.1f}s (exit 0) ---\n"
                )
            else:
                record.state = RunState.FAILED
                record.error = f"Exited with code {exit_code}"
                self._append(
                    f"\n--- {step.script} FAILED (exit {exit_code}). "
                    f"Pipeline stopped. ---\n"
                )
        return exit_code == 0 and not self._cancel.is_set()

    def _execute(self, command: list[str], cwd: Path) -> int:
        return (
            self._execute_pty(command, cwd)
            if _HAS_PTY
            else self._execute_pipes(command, cwd)
        )

    # -- POSIX: real terminal semantics ------------------------------------ #

    def _execute_pty(self, command: list[str], cwd: Path) -> int:
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(
                command,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=str(cwd),
                env=self._environment(),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            self._emit(f"[launch failed] {exc}\n")
            return 127
        os.close(slave)

        with self._lock:
            self._process = process
            self._write_fd = master

        deadline = time.monotonic() + max(self._settings.timeout_seconds, 1)
        last_output = time.monotonic()

        try:
            while True:
                if self._cancel.is_set():
                    self._terminate(process)
                    break
                if time.monotonic() > deadline:
                    self._emit(
                        f"\n[timeout] exceeded {self._settings.timeout_seconds}s; "
                        f"terminating.\n"
                    )
                    self._terminate(process)
                    break

                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(master, 8192)
                    except OSError as exc:
                        # The slave closing is a normal end-of-process signal.
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    last_output = time.monotonic()
                    with self._lock:
                        self._append(data.decode("utf-8", errors="replace"))
                        if self._state is RunState.WAITING_INPUT:
                            self._state = RunState.RUNNING
                else:
                    if process.poll() is not None:
                        break
                    self._maybe_flag_prompt(last_output)
        finally:
            self._drain_pty(master)
            with self._lock:
                self._write_fd = None
            try:
                os.close(master)
            except OSError:
                pass

        code = process.wait()
        with self._lock:
            self._process = None
            if self._state is RunState.WAITING_INPUT:
                self._state = RunState.RUNNING
        return code

    def _drain_pty(self, master: int) -> None:
        """Collect anything written just before the process exited."""
        for _ in range(50):
            try:
                ready, _, _ = select.select([master], [], [], 0.02)
                if not ready:
                    return
                data = os.read(master, 8192)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._append(data.decode("utf-8", errors="replace"))

    # -- Windows fallback --------------------------------------------------- #

    def _execute_pipes(self, command: list[str], cwd: Path) -> int:  # pragma: no cover
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                env=self._environment(),
                bufsize=0,
            )
        except OSError as exc:
            self._emit(f"[launch failed] {exc}\n")
            return 127

        with self._lock:
            self._process = process

        last_output = time.monotonic()
        stop = threading.Event()

        def pump() -> None:
            nonlocal last_output
            assert process.stdout is not None
            while not stop.is_set():
                chunk = process.stdout.read(1)
                if not chunk:
                    return
                last_output = time.monotonic()
                with self._lock:
                    self._append(chunk.decode("utf-8", errors="replace"))
                    if self._state is RunState.WAITING_INPUT:
                        self._state = RunState.RUNNING

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        deadline = time.monotonic() + max(self._settings.timeout_seconds, 1)
        while process.poll() is None:
            if self._cancel.is_set():
                self._terminate(process)
                break
            if time.monotonic() > deadline:
                self._emit(
                    f"\n[timeout] exceeded {self._settings.timeout_seconds}s; "
                    f"terminating.\n"
                )
                self._terminate(process)
                break
            self._maybe_flag_prompt(last_output)
            time.sleep(0.15)

        stop.set()
        reader.join(timeout=1.0)
        code = process.wait()
        with self._lock:
            self._process = None
            if self._state is RunState.WAITING_INPUT:
                self._state = RunState.RUNNING
        return code

    # -- shared helpers ----------------------------------------------------- #

    def _maybe_flag_prompt(self, last_output: float) -> None:
        """Flip to WAITING_INPUT when output stalls mid-line, i.e. on a prompt."""
        if time.monotonic() - last_output < PROMPT_IDLE_SECONDS:
            return
        with self._lock:
            if self._state is not RunState.RUNNING:
                return
            tail = "".join(self._transcript[-4:])
            if tail and not tail.endswith(("\n", "\r")):
                self._state = RunState.WAITING_INPUT

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        except OSError:
            pass

    def _finish(self, state: RunState) -> None:
        with self._lock:
            self._state = state
            self._finished_at = datetime.now().astimezone()
            self._current = -1
            self._append(f"\n=== Run {state.value} at {self._finished_at:%H:%M:%S} ===\n")
        if self._log_handle:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
