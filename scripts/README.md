# Pipeline scripts

Everything in this folder is discoverable by the dashboard's **Admin** page,
which can order the scripts, run them one after another, and answer any prompt a
script writes to the terminal.

Point the panel somewhere else by editing `admin.scripts_dir` in
`config/config.yaml` — a shared drive, an existing ETL folder, anywhere:

```yaml
admin:
  scripts_dir: "/home/ops/etl"
```

## Writing a script the panel can drive

1. **Print progress as you go.** Output is streamed live; the panel does not
   wait for the script to finish.
2. **Ask questions with plain `input()`.** The panel detects the prompt and
   opens an answer box. The same script still works when run from a real
   terminal.
3. **Exit non-zero on failure.** A failing step stops the rest of the run.
4. **Prefix filenames with a number** (`01_`, `02_`) so the default order is
   already the right one.
5. Files starting with `_` or `.` are hidden from the panel.

```python
"""One-line description — this is what the panel shows next to the filename."""

print("Working...")
answer = input("Publish to the shared drive? [y/N] ")
if answer.strip().lower() not in {"y", "yes"}:
    raise SystemExit("Cancelled by operator.")
print("Published.")
```

Run the same pipeline from a real terminal instead:

```bash
python tools/run_pipeline.py
```
