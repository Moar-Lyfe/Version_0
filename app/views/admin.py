"""Admin page: point at a script folder, set the run order, run it, answer it.

The panel is a thin shell over :mod:`app.admin.runner`; everything about process
handling lives there. What this file owns is the interaction model:

* while a script is *running* the page polls so output streams in;
* while it is *waiting for input or paused* the page stops polling, so a rerun
  can never wipe out half-typed text in the answer box.
"""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from app import paths
from app.admin import lock as run_lock
from app.admin import registry
from app.admin.registry import Pipeline, Step
from app.admin.runner import PipelineRunner, RunState
from app.settings import AdminSettings, Settings
from app.ui import components
from app.views import auth, widgets

RUNNER_KEY = "pipeline_runner"  # + ":<routine key>"
POLL_SECONDS = 0.7

_STATE_ICONS = {
    RunState.IDLE: "·",
    RunState.RUNNING: "▶",
    RunState.WAITING_INPUT: "⌨",
    RunState.PAUSED: "⏸",
    RunState.FINISHED: "✓",
    RunState.FAILED: "✕",
    RunState.CANCELLED: "⊘",
}


# --------------------------------------------------------------------------- #
# Access gate
# --------------------------------------------------------------------------- #


def _forget_runners() -> None:
    """Drop every routine's runner, e.g. after the scripts folder changes."""
    for key in list(st.session_state):
        if str(key).startswith(RUNNER_KEY):
            st.session_state.pop(key, None)


# --------------------------------------------------------------------------- #
# Scripts directory
# --------------------------------------------------------------------------- #


def _scripts_directory(settings: AdminSettings) -> AdminSettings:
    """Show and optionally override where scripts are read from."""
    active = registry.effective_admin_settings(settings)
    root = active.resolved_scripts_dir()
    override = registry.load_dir_override()

    st.caption(
        "Saved on this machine, overriding config.yaml."
        if override
        else "From config.yaml."
    )

    with st.form("scripts_dir_form"):
        value = st.text_input(
            "Folder containing the Python scripts",
            value=active.scripts_dir,
            help=(
                "Absolute path, or relative to the project root. Saved to "
                "runtime/admin_override.json on this machine only; set "
                "admin.scripts_dir in config.yaml to change it everywhere."
            ),
        )
        left, right = st.columns(2)
        with left:
            saved = st.form_submit_button("Use this folder", type="primary")
        with right:
            reverted = st.form_submit_button("Revert to config.yaml")

    if saved:
        cleaned = value.strip()
        registry.save_dir_override(cleaned or None)
        _forget_runners()
        st.rerun()
    if reverted:
        registry.save_dir_override(None)
        _forget_runners()
        st.rerun()

    if root.is_dir():
        components.meta_strip([f"Reading {root}", f"Pattern {active.scripts_glob}"])
    else:
        st.error(f"`{root}` does not exist or is not a directory.")
    return active


# --------------------------------------------------------------------------- #
# Run order editor
# --------------------------------------------------------------------------- #

_COL_ORDER = "Order"
_COL_SCRIPT = "Script"
_COL_ENABLED = "Enabled"
_COL_PAUSE = "Pause before"
_COL_ARGS = "Arguments"


def _pipeline_editor(
    settings: AdminSettings,
    scripts: list[registry.ScriptInfo],
    key: str = registry.SCRIPTS,
) -> Pipeline:
    available = [info.relative for info in scripts]
    pipeline = registry.load_pipeline(key, scripts)

    components.section("Run order", f"{len(available)} script(s) available")

    if not available:
        components.empty_state(
            "No scripts found",
            "Nothing in that folder matches the configured pattern. Add a `.py` "
            "file, or change the folder above. Files starting with `_` or `.` "
            "are deliberately hidden.",
        )
        return Pipeline()

    frame = pd.DataFrame(
        [
            {
                _COL_ORDER: index + 1,
                _COL_SCRIPT: step.script,
                _COL_ENABLED: step.enabled,
                _COL_PAUSE: step.pause_before,
                _COL_ARGS: step.args,
            }
            for index, step in enumerate(pipeline.steps)
        ]
    )
    if frame.empty:
        frame = pd.DataFrame(
            columns=[_COL_ORDER, _COL_SCRIPT, _COL_ENABLED, _COL_PAUSE, _COL_ARGS]
        )

    edited = st.data_editor(
        frame,
        key=f"pipeline_editor_{key}",
        hide_index=True,
        num_rows="dynamic",
        column_config={
            _COL_ORDER: st.column_config.NumberColumn(
                _COL_ORDER,
                help="Steps run lowest first. Renumber to reorder, then save.",
                min_value=1,
                step=1,
                width="small",
            ),
            _COL_SCRIPT: st.column_config.SelectboxColumn(
                _COL_SCRIPT, options=available, required=True, width="large"
            ),
            _COL_ENABLED: st.column_config.CheckboxColumn(_COL_ENABLED, width="small"),
            _COL_PAUSE: st.column_config.CheckboxColumn(
                _COL_PAUSE,
                help="Hold the run before this step until it is approved.",
                width="small",
            ),
            _COL_ARGS: st.column_config.TextColumn(
                _COL_ARGS, help="Command-line arguments, e.g. --month 2026-09"
            ),
        },
    )

    ordered = edited.dropna(subset=[_COL_SCRIPT]).copy()
    ordered[_COL_ORDER] = pd.to_numeric(ordered[_COL_ORDER], errors="coerce").fillna(
        9_999
    )
    ordered = ordered.sort_values(_COL_ORDER, kind="stable")

    draft = Pipeline(
        key=key,
        label=pipeline.label,
        description=pipeline.description,
        steps=[
            Step(
                script=str(row[_COL_SCRIPT]),
                enabled=bool(row[_COL_ENABLED]),
                pause_before=bool(row[_COL_PAUSE]),
                args=str(row[_COL_ARGS] or ""),
            )
            for _, row in ordered.iterrows()
        ]
    )

    save, reset = st.columns(2)
    with save:
        if st.button("Save run order", type="primary", key=f"save_{key}"):
            registry.save_pipeline(draft)
            st.success("Run order saved.")
    with reset:
        if st.button("Reset to the default", key=f"reset_{key}"):
            registry.reset_pipeline(key)
            st.rerun()

    with st.expander("What each script does", expanded=False, key=f"docs_{key}"):
        widgets.dataframe(
            pd.DataFrame(
                [
                    {
                        "Script": info.relative,
                        "Description": info.description or "—",
                        "Modified": (
                            info.modified_at.strftime("%Y-%m-%d %H:%M")
                            if info.modified_at
                            else "—"
                        ),
                    }
                    for info in scripts
                ]
            ),
            hide_index=True,
        )

    return draft


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


def _status_table(snapshot) -> None:
    if not snapshot.steps:
        return
    rows = []
    for step in snapshot.steps:
        duration = step.duration_seconds
        rows.append(
            {
                "#": step.index + 1,
                "": _STATE_ICONS.get(step.state, "·"),
                "Script": step.script,
                "State": step.state.value.replace("_", " "),
                "Exit": "—" if step.exit_code is None else str(step.exit_code),
                "Elapsed": f"{duration:.1f}s" if duration else "—",
            }
        )
    widgets.dataframe(pd.DataFrame(rows), hide_index=True)


def _run_controls(settings: AdminSettings, draft: Pipeline) -> None:
    components.section("Execution", f"{len(draft.enabled_steps())} enabled step(s)")

    state_key = f"{RUNNER_KEY}:{draft.key}"
    runner: PipelineRunner | None = st.session_state.get(state_key)
    snapshot = runner.snapshot() if runner else None
    active = bool(snapshot and snapshot.state.is_active)

    # The runner lives in per-session state, so a second browser -- or a
    # scheduled terminal run -- is invisible to it. The on-disk lock is what
    # actually stops two runs of the same scripts overlapping.
    holder = run_lock.read()
    held_elsewhere = bool(holder and not active and not holder.is_ours)
    if held_elsewhere:
        stale = run_lock.is_stale(holder, settings.timeout_seconds)
        if stale:
            st.warning(
                f"A run lock is present but looks abandoned — {holder.describe()}. "
                "Release it if you are sure nothing is still running."
            )
            if st.button("Release the stale lock", key=f"release_{draft.key}"):
                run_lock.force_release()
                st.rerun()
        else:
            st.info(
                f"A pipeline run is already in progress: {holder.describe()}. "
                "Wait for it to finish rather than starting a second one over "
                "the top of it."
            )

    start_col, cancel_col = st.columns(2)
    with start_col:
        if st.button(
            "Run pipeline",
            key=f"run_{draft.key}",
            type="primary",
            disabled=active or held_elsewhere or not draft.enabled_steps(),
            help="Runs the enabled steps in order, stopping on the first failure.",
        ):
            new_runner = PipelineRunner(draft, settings)
            if new_runner.start(owner=f"Admin panel — {draft.label}"):
                st.session_state[state_key] = new_runner
            else:
                blocker = new_runner.blocked_by
                st.error(
                    "Could not start: another run took the lock first"
                    + (f" — {blocker.describe()}." if blocker else ".")
                )
            st.rerun()
    with cancel_col:
        if st.button("Cancel run", key=f"cancel_{draft.key}", disabled=not active) and runner:
            runner.cancel()
            st.rerun()

    if runner is None or snapshot is None:
        st.caption("No run yet. Save the order above, then press **Run pipeline**.")
        return

    _status_table(snapshot)

    # --- the human in the loop ------------------------------------------- #
    if snapshot.state is RunState.PAUSED:
        st.warning(f"Paused before **{snapshot.pending_pause}**. Approve to continue.")
        approve, stop = st.columns(2)
        with approve:
            if st.button("Continue", type="primary", key=f"continue_{draft.key}"):
                runner.resume()
                st.rerun()
        with stop:
            if st.button("Stop here", key=f"stop_{draft.key}"):
                runner.cancel()
                st.rerun()

    if snapshot.state is RunState.WAITING_INPUT:
        prompt = snapshot.transcript.rstrip("\n").splitlines()
        st.info(f"The script is asking: **{prompt[-1].strip() if prompt else '…'}**")

    if snapshot.state in (RunState.RUNNING, RunState.WAITING_INPUT):
        with st.form(f"stdin_form_{draft.key}", clear_on_submit=True):
            answer = st.text_input(
                "Reply to the script",
                placeholder="Type the answer and press Send (blank sends Enter)",
                label_visibility="collapsed",
            )
            send, poke = st.columns([2, 1])
            with send:
                submitted = st.form_submit_button("Send", type="primary")
            with poke:
                refreshed = st.form_submit_button("Refresh output")
        if submitted:
            if not runner.send_input(answer):
                st.error("The script is no longer accepting input.")
            time.sleep(0.35)
            st.rerun()
        if refreshed:
            st.rerun()

    components.section("Console", snapshot.state.value.replace("_", " "))
    components.console(snapshot.transcript, "Waiting for the first line of output…")

    if snapshot.log_path:
        st.caption(f"Full transcript: `{snapshot.log_path}`")

    if snapshot.state is RunState.FINISHED:
        st.success("Pipeline finished. All steps exited 0.")
    elif snapshot.state is RunState.FAILED:
        st.error("Pipeline stopped: a step exited non-zero. See the console above.")
    elif snapshot.state is RunState.CANCELLED:
        st.warning("Pipeline cancelled.")

    # Poll only while output is actually moving. Pausing the poll while the
    # script waits keeps a rerun from clearing half-typed input.
    if snapshot.state is RunState.RUNNING:
        time.sleep(POLL_SECONDS)
        st.rerun()


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def _routine(
    settings: AdminSettings, scripts: list[registry.ScriptInfo], key: str
) -> None:
    """One named routine: what it is, its run order, and its console."""
    label, description = registry.PIPELINE_META.get(key, (key, ""))
    if description:
        st.caption(description)

    if not scripts:
        components.empty_state(
            "No scripts found",
            "Nothing in the scripts folder matches the configured pattern. "
            "Check the folder under **Settings** below.",
        )
        return

    draft = _pipeline_editor(settings, scripts, key)
    _run_controls(settings, draft)


def render(settings: Settings) -> None:
    components.masthead("Admin", settings.app.organization)
    components.subhead(
        "Run the reporting routines, and answer them when they ask."
    )

    if not settings.admin.enabled:
        components.empty_state(
            "Admin panel disabled",
            "Set `admin.enabled: true` in `config/config.yaml` to switch it on.",
        )
        return

    if not auth.gate(settings.admin, "The Admin panel", "admin_gate"):
        return

    active = registry.effective_admin_settings(settings.admin)
    scripts = registry.discover(active)

    # One lock covers all three, so a run in progress is visible from whichever
    # tab you happen to be on rather than only the one that started it.
    holder = run_lock.read()
    if holder and not holder.is_ours:
        st.info(f"A run is in progress: {holder.describe()}")

    labels = [registry.PIPELINE_META[key][0] for key in registry.PIPELINE_ORDER]
    for tab, key in zip(st.tabs(labels), registry.PIPELINE_ORDER):
        with tab:
            _routine(active, scripts, key)

    components.section("Settings", "where the scripts live")
    _scripts_directory(settings.admin)

    with st.expander("Recent run logs", expanded=False):
        logs = sorted(paths.RUN_LOG_DIR.glob("run_*.log"), reverse=True)[:10]
        if not logs:
            st.caption("No runs recorded yet.")
        for log in logs:
            st.caption(f"`{log.name}` · {log.stat().st_size / 1024:,.1f} KB")
