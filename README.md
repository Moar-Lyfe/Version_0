# Executive Dashboard

An internal reporting dashboard for premium and sales production, built on
Streamlit. It shows four KPIs across five reporting windows, projects the month
and the year off a working-day calendar, and breaks any period out by agent.

Excel is the operational front end. An ETL moves each export into a locally
hosted PostgreSQL database, and the dashboard reads from there — so reporting no
longer depends on whether a workbook happens to be open, moved, or mid-save. It
can still read the workbooks directly during the migration: `data.source_type`
picks, and nothing downstream knows the difference.

An **Analytics** page tracks every KPI on 15/30/45/90-day moving averages and
raises an alert when one slips — against a number you set, or against the
metric's own history when nobody has set one. Set targets and it also reports
attainment and the daily pace needed to hit them.

It also ships an **Admin** panel that runs your other Python scripts in a
configurable order and lets you answer their terminal prompts from the browser.

- **Clean and minimal.** No chart junk, no colour that does not mean something.
- **Mobile first.** KPI cards reflow to two across on a phone; everything is
  reachable with a thumb.
- **Portable.** Clone, run one script, edit one path. Nothing is machine-specific
  except `config/config.yaml`, which is git-ignored.

---

## Quick start

### macOS / Linux

```bash
git clone https://github.com/Moar-Lyfe/Version_0.git
cd Version_0
./run.sh
```

### Windows

```bat
git clone https://github.com/Moar-Lyfe/Version_0.git
cd Version_0
run.bat
```

The launcher creates a `.venv`, installs dependencies, copies
`config/config.example.yaml` to `config/config.yaml`, generates sample workbooks
so the dashboard is not empty, and opens <http://localhost:8501>.

<details>
<summary>Manual setup, if you would rather not use the launcher</summary>

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
python tools/generate_sample_data.py       # optional demo data
streamlit run app/main.py
```
</details>

**Requirements:** Python 3.10 or newer, and PostgreSQL if you are using the
database source. No Excel installation is needed at any point.

> Start it from the project root (both launchers `cd` there first). Streamlit
> only reads `.streamlit/config.toml` relative to the working directory, so
> launching from elsewhere loses the theme and port settings.

---

## The pipeline

```
   Excel  ──►  tools/etl_excel_to_postgres.py  ──►  CSV  ──►  PostgreSQL  ──►  Dashboard
 (front end)         read · normalise            (archive)     sales table
```

### One-time database setup

```bash
createdb reporting
psql -d reporting -f db/schema.sql
```

Then in `config/config.yaml`:

```yaml
data:
  source_type: "postgres"
  postgres:
    host: "localhost"
    database: "reporting"
    user: "dashboard"
    password_env: "EXEC_DASH_DB_PASSWORD"   # never the password itself
    table: "sales"
```

See [`db/README.md`](db/README.md) for roles, least privilege, and mapping an
existing `sales` table whose columns are named differently.

### Loading an export

```bash
python tools/etl_excel_to_postgres.py                         # newest matched workbook
python tools/etl_excel_to_postgres.py --file sept.xlsx --sheet Production
python tools/etl_excel_to_postgres.py --dry-run               # read and stage only
python tools/etl_excel_to_postgres.py --csv-only              # stop at the CSV
```

Three visible stages, so a failure is always attributable to one of them:

1. **Read** — the workbook and the named sheet, using exactly the same column
   mapping and cell parsing the dashboard applies. Currency text, accounting
   negatives and blank agents are handled identically at both ends.
2. **Stage** — a CSV is archived under `runtime/etl/`. That file is the record
   of what was handed to the database, and reloading it later needs no Excel.
3. **Load** — `COPY` into a temporary table, then merge into `sales`.

### Running it twice is safe

The merge is keyed on a **natural key** built from each source row — the policy
number when the export has one. That makes the load idempotent:

| Situation | What happens |
|---|---|
| Same workbook loaded again | Nothing. Rows are present and identical |
| New rows appended in Excel | Only those rows insert |
| A premium corrected in Excel | That row **updates**; no duplicate, no double count |
| A row deleted from Excel | Stays in the database — deletion is deliberate, not incidental |

`--on-conflict ignore` switches to insert-only if you would rather corrections
never flow through. The default is `update`, because a corrected row arriving as
a second sale is the worse failure.

Without a policy-number column, rows are keyed by a content hash, which means an
edited row loads as a new one. The ETL says so when it happens — map
`data.columns.policy_id` to avoid it.

### Scheduling

```cron
0 5 * * 1-6  cd /opt/executive-dashboard && .venv/bin/python tools/etl_excel_to_postgres.py >> runtime/logs/etl.log 2>&1
0 6 * * 1-6  cd /opt/executive-dashboard && .venv/bin/python tools/snapshot_kpis.py       >> runtime/logs/snapshots.log 2>&1
0 7 * * 1-6  cd /opt/executive-dashboard && .venv/bin/python tools/check_alerts.py --quiet >> runtime/logs/alerts.log 2>&1
```

Order matters: load, then snapshot what was loaded, then judge it. Exit codes
are 0 loaded, 1 read failure (nothing loaded), 2 database failure (nothing
committed).

`scripts/03_load_excel_to_database.py` does the same thing from the **Admin**
panel, with a confirmation prompt before it writes — for when the load should be
driven by whoever is at the dashboard.

### Load history

Every run appends to an `etl_runs` table: what ran, when, and what it changed.
Diagnostics shows the last ten, which is what answers "why did last night's
numbers move?" without depending on anyone's memory.

---

## Pointing it at your data

Everything lives in `config/config.yaml`. Two sections matter.

### 1. Where the workbooks are

```yaml
data:
  sources:
    - name: "production"
      path: "//fileserver/reports/daily"   # or Z:/Shared/Prod, or /Volumes/Reports
      glob: "*.xlsx"
      sheet: null        # null = first sheet · "Production" = by name · 0 = by index · "*" = all
      header_row: 0      # 2 if there is a two-line title block above the headers
      recursive: false
```

Every matching workbook is read and stacked into one table, so a folder of
monthly exports behaves exactly like a single master file. Add as many sources
as you like — a local folder and a share can coexist. Files Excel leaves behind
while a workbook is open (`~$name.xlsx`) are skipped automatically.

### 2. What the columns are called

The dashboard does not care what your headers say, as long as you list them.
Matching ignores case and extra whitespace, and the first alias present wins:

```yaml
data:
  columns:
    date: ["Date", "Effective Date", "Issue Date"]
    premium: ["Premium", "Written Premium", "AP"]
    agent: ["Agent", "Agent Name", "Producer"]
    category: ["Category", "Product", "Line of Business"]
    channel: ["Channel", "Source", "Lead Source"]
    policy_id: ["Policy Number", "Policy #"]
    count: ["Units", "Count"]        # optional; without it each row is one sale
```

Only `date` is required. Everything else falls back to a sensible default
(premium 0, one sale per row, agent `Unassigned`) and is reported as unmapped.

> **Open the Diagnostics page after any change.** It lists every workbook found,
> every header in the sheet, exactly which canonical field each one resolved to,
> and every row that was dropped. It is the fastest way to fix a mapping.

### 3. The two reported categories

```yaml
data:
  categories:
    category_1:
      label: "Life"                            # what the dashboard prints
      values: ["Term Life", "Whole Life"]      # what the workbook contains
    category_2:
      label: "Medicare"
      values: ["Medicare", "Supplement"]
```

Values matching neither list still count toward **Premium** and **Total Sales**
but are bucketed as *Other*. Diagnostics lists them with row counts so nothing
hides — if `Annuity` shows up there with 400 rows, you know to add it.

### 4. Web sales

```yaml
data:
  web_sales:
    channel_values: ["Web", "Online", "Website"]
    agent_values: ["Web Sales", "House Account"]
```

A row is a web sale when its **channel or its agent** matches. The
**Include web sales** checkbox on the dashboard switches those rows in and out
of every KPI, projection, table and export at once.

---

## What the dashboard shows

### Four KPIs

| KPI | What it counts |
|---|---|
| **Premium** | Sum of the premium column |
| **Total Sales** | One per row, or the `count` column when mapped |
| **Category 1 Sales** | Sales whose category matched `category_1` |
| **Category 2 Sales** | Sales whose category matched `category_2` |

### Five windows

`Today` · `Yesterday` · `Rolling 7 days` · `Month to date` · `Year to date`

Pick one at the top; the four cards, the trend chart and the breakdown all follow
it. Each card carries a like-for-like comparison — today against yesterday,
month-to-date against the same point last month, year-to-date against the same
date last year. The **All periods** table shows every window at once, with the
month-end and year-end projections as their own rows.

### Projections

```
projected = actual to date ÷ working days elapsed × working days in the period
```

A working day is any day that is **not a Sunday** and **not an observed
holiday**:

```yaml
calendar:
  exclude_weekdays: ["Sunday"]
  holidays:
    observed: [new_years_day, memorial_day, independence_day,
               labor_day, thanksgiving, christmas_day]
    shift_to_next_working_day: true    # a Sunday holiday is observed on Monday
    extra_dates: ["2026-12-24"]        # one-off closures
    working_overrides: []              # force a day back to "open"
```

Holidays are computed, not listed, so the calendar stays correct in future years
with no maintenance. Available observances: `new_years_day`, `mlk_day`,
`presidents_day`, `good_friday`, `memorial_day`, `juneteenth`,
`independence_day`, `labor_day`, `columbus_day`, `veterans_day`, `thanksgiving`,
`day_after_thanksgiving`, `christmas_eve`, `christmas_day`, `new_years_eve`.

`projection.count_today_as_elapsed: true` matches "total days ÷ days passed"
literally. Set it to `false` to project off completed days only, which reads
optimistic early in the day rather than pessimistic.

The Diagnostics page prints the working-day count for the current month and
year, the resolved holiday dates, and every non-working day in the next 30 days.

### Refreshing

**Refresh data** re-reads every workbook and stamps the page with the time it
did. Between refreshes the data is cached, so switching periods is instant. The
cache is also keyed to each file's modification time, so a replaced workbook is
picked up on the next interaction even without pressing the button.

### Breaking out by agent

**Expand metrics by agent** opens a pivot: one row per agent, one column per KPI,
sorted by premium, with a share-of-premium bar and a totals row that reconciles
against the cards above. Switch the rows to **Category** or **Channel** with the
group-by control, and export the current view as CSV.

---

## Targets

A projection says where the month will land. A target says whether that is good
enough — and what the remaining working days need to close the gap.

```yaml
targets:
  premium:
    month: 700000
    year: 8500000
  sales:
    month: 200
  overrides:                 # a month that is not like the others
    premium:
      "2026-12": 950000
```

Targets are **period totals**, not daily rates. With one set, a KPI card shows
how much is banked and where the current pace lands, the **All periods** table
gains a target row directly under its projection, and the Dashboard prints the
line people actually act on:

> To reach the September target of $700,000, the remaining 21 working days need
> $27,230/day. Current pace is $32,042/day, ahead of what the target needs.

The required pace is computed against the same working calendar as everything
else, so it excludes Sundays and holidays rather than quietly assuming the team
works through them. A target also becomes a reference line on the
moving-average chart, converted to the daily rate it implies.

**An absent target means "no target", never zero.** Leave a metric out and its
card simply shows the projection instead.

---

## Snapshots — what did we report last Tuesday?

Every figure is recomputed from the workbooks as they stand right now. That is
usually what you want, and it quietly means history moves: a corrected row, a
restated policy, a re-exported month, and last month's premium is no longer the
number anyone saw at the time.

```bash
python tools/snapshot_kpis.py                    # record today
python tools/snapshot_kpis.py --check            # restatements only, write nothing
python tools/snapshot_kpis.py --as-reported 2026-09-01
python tools/snapshot_kpis.py --list
```

Two small append-only CSVs under `runtime/snapshots/`: the headline figures for
every window as they were reported, and the daily totals over a trailing window.
Comparing the two is what makes a **restatement** visible:

```
1 restatement(s) since the snapshot of 2026-09-03:
  2026-08-25  premium     $17,848.80 -> $116,461.18  +98,612.38 (+552.5%)
```

Re-running on the same day replaces that day's rows rather than appending, so
the step is safe to run more than once. Diagnostics shows the archive and any
restatements against current data. Exit code 1 means a closed day has moved.

---

## Analytics — moving averages and alerting

The Dashboard reports the current period. The **Analytics** page watches the
*shape* of each KPI over trailing **15, 30, 45 and 90-day** windows and says
plainly when one is slipping. A summary banner appears on the Dashboard when
anything is wrong, so nobody has to go looking.

**Averages are per working day.** A window holding three Sundays is not
penalised against one holding two, and a window containing a holiday is not read
as a slowdown. That is the same calendar the projections use, so the two
features can never disagree about what a day is worth. Set
`analytics.basis: calendar_days` if you would rather divide by raw days.

### Three kinds of rule

| Kind | You supply | What it does |
|---|---|---|
| `threshold` | a number | **Defined.** The average must hold a floor (or stay under a ceiling) |
| `relative` | a percentage | **Undefined.** The short window is judged against the metric's *own* longer baseline |
| `trend` | a percentage | The same window against where it stood N days ago |

`relative` is the one to reach for by default. It needs no targets, so it works
on a metric nobody has set a goal for, and it keeps working as the business
grows — the bar moves with the book instead of going stale.

```yaml
analytics:
  windows: [15, 30, 45, 90]
  rules:
    - name: "Premium vs 90-day baseline"     # undefined: no target needed
      metric: premium
      kind: relative
      window: 15
      baseline: 90
      warn_pct: 10                            # 15-day 10% under the 90-day
      critical_pct: 20

    - name: "Category 1 daily floor"          # defined: a real floor
      metric: category_1
      kind: threshold
      window: 30
      operator: min
      warn: 2.0                               # sales per working day
      critical: 1.5
```

### Or just use a preset

Leave `rules: []` and a preset runs instead — no targets, no setup:

| Preset | Watches |
|---|---|
| `standard` *(default)* | All four KPIs against their own 90-day baseline |
| `sensitive` | Tighter tolerances, extra windows, plus a 45-day trend rule |
| `minimal` | Premium only |
| `none` | No rules; the page still charts the averages |

### What the page shows

- **Alerts** — one card per breach, worst first, each written out in plain
  English with the actual numbers. Severity is always written as well as
  coloured, so colour never carries the meaning alone.
- **Moving averages** — every window overlaid for the chosen metric, with a
  shared crosshair. This is where a 15-day line pulling away from the 90-day
  becomes obvious. A threshold rule is drawn as a reference line.
- **Agents to watch** — the same self-calibrating rules applied one agent at a
  time, listing only those slipping. Thresholds are deliberately *not* applied
  per agent: a floor written for the whole book says nothing about one person.
  Agents below `agent_min_sales` are skipped, because a percentage swing on
  three sales is noise.
- **Rules** — exactly what is doing the judging, and whether it came from your
  config or a preset.

### Data health — is the feed even working?

A broken export and a bad sales week look identical in a moving average. If the
nightly job dies, recent days become zeros, the 15-day average collapses, and
the page reports a confident 40% premium slide — sending someone after a sales
problem that is really a dead scheduled job, while the real numbers sit unread
in a file.

Three checks separate the two, and they sort *above* the performance rules:

| Check | Catches |
|---|---|
| **Freshness** | Working days between the newest row and today. Counted in working days, so a Sunday or a holiday weekend never reads as a failure |
| **Row volume** | Recent rows per working day vs. a 90-day baseline — the partial export that arrives on time but carries a fraction of the rows |
| **Sources** | A workbook that could not be read, or a configured source matching no files at all |

When a data check goes critical, the Dashboard banner says *"Data problem —
figures below may be wrong"* instead of a performance alert, and the Analytics
page puts a caveat above the moving-average alerts. Fix the data first, then
judge the numbers.

```yaml
analytics:
  data_health:
    stale_warn_days: 2
    stale_critical_days: 4
    volume_warn_pct: 30
    volume_critical_pct: 50
```

### "Not enough data" is a real answer

A 90-day baseline needs 90 days. Rather than average over whatever exists and
report a confident, wrong number, an unanswerable rule says so — including for
an agent who started last month. The same applies to a zero baseline, where a
percentage comparison means nothing.

### Alerting outside the browser

`tools/check_alerts.py` runs the same rules from a terminal and exits non-zero
on a breach, so a scheduler or the Admin pipeline can act on it:

```bash
python tools/check_alerts.py                 # book-wide + per agent
python tools/check_alerts.py --quiet         # only what is wrong
python tools/check_alerts.py --json          # machine-readable
python tools/check_alerts.py --csv out.csv
```

| Exit code | Meaning |
|---|---|
| 0 | Nothing above the warning line |
| 1 | At least one warning |
| 2 | At least one critical |
| 3 | Could not evaluate — no data, or not enough history |

---

## Admin panel

Runs the Python scripts in another folder, in an order you set, with a human in
the loop.

**Point it anywhere.** `admin.scripts_dir` in `config.yaml` is the durable
setting; the panel's *Scripts directory* box overrides it for this machine only
(saved to `runtime/`, never committed).

```yaml
admin:
  scripts_dir: "/home/ops/etl"    # or C:/Reporting/jobs
  scripts_glob: "*.py"
  working_dir: null               # null = each script's own folder
  timeout_seconds: 3600
  password_env: null              # set to gate the panel behind an env var
  environment: {}                 # extra env vars for every script
```

**Set the order.** The run-order table takes an `Order` number, an `Enabled`
box, a `Pause before` box and free-form `Arguments` per step. Renumber, save,
and it persists in `runtime/pipeline.json`. Steps run lowest-number first and
the pipeline stops on the first non-zero exit.

**Only one run at a time.** The runner lives in per-browser state, so without a
guard two people with the panel open — or one person in a browser and a
scheduled job in a terminal — could run the same scripts over the top of each
other. An on-disk lock covers both: the second caller is told who holds it and
since when. A lock left behind by a machine that lost power is detected as stale
(the owning process is gone, or it has outlived the script timeout) and can be
cleared from the panel. `tools/run_pipeline.py` takes the same lock, and
`--force` overrides it when you are certain the holder is dead.

**Answer prompts mid-script.** A script that calls `input()` halfway through
stops and waits, exactly as it would in a terminal. The panel detects the prompt,
shows it, and opens an answer box; what you send is written to the script's stdin
and echoed into the transcript. Scripts run under a pseudo-terminal on macOS and
Linux and under unbuffered pipes on Windows, so prompts appear before the answer
either way.

```python
"""One-line description — the panel shows this next to the filename."""

print("Preparing export...")
if input("Publish to the shared drive? [y/N] ").strip().lower() not in {"y", "yes"}:
    raise SystemExit("Cancelled by operator.")
print("Published.")
```

`Pause before` is the other half: the run holds before that step until someone
clicks **Continue**, which is the one to use before anything destructive.

Every run is transcribed to `runtime/logs/run_<timestamp>.log`.

**Same pipeline, real terminal** — for a scheduler, over SSH, or when nobody
wants a browser in the loop:

```bash
python tools/run_pipeline.py           # the saved order
python tools/run_pipeline.py --list    # show it without running
python tools/run_pipeline.py --all     # every script, filename order
python tools/run_pipeline.py --yes     # auto-approve the pause-before steps
```

---

## Sharing it on the local network

This is a **LAN-only** application. It is meant to run on one machine and be
opened by everyone else over `http://<that-machine>:8501` — including from a
phone, which is what the mobile layout is for. `.streamlit/config.toml` binds to
`0.0.0.0:8501` so that works out of the box; `address = "localhost"` restricts
it to one machine.

**It makes no outbound requests.** Loading every page, switching every period
and rendering the chart contacts nothing but the dashboard's own port —
Streamlit serves its own JavaScript, the chart runtime is bundled rather than
pulled from a CDN, the theme uses system fonts, and telemetry is off. It runs
fine on a machine with no internet access.

Two things LAN-only does *not* solve, both covered in
**[docs/deployment.md](docs/deployment.md)**:

- Streamlit has no user accounts, so anyone who can reach the port sees
  everything. Do not port-forward 8501; if it is needed off-site, use a VPN.
- The Admin panel runs Python as the service user, so anyone who can reach the
  page can run those scripts. Gate it with `admin.password_env`, or set
  `admin.enabled: false` and drive the pipeline from a terminal instead.

That guide also covers binding to a single interface, firewall rules scoped to
your subnet, and starting the dashboard automatically at boot — a ready systemd
unit is in [`deploy/`](deploy/), with the Windows Task Scheduler and launchd
equivalents written out.

---

## Project layout

```
app/
  main.py              Streamlit entrypoint and page navigation
  settings.py          config.yaml -> typed dataclasses
  paths.py             project-root anchoring for every relative path
  core/
    calendar_rules.py  working days, computed holidays
    periods.py         today / yesterday / rolling 7 / MTD / YTD windows
    kpis.py            the four KPIs and the pivot
    projections.py     straight-line month and year projections
    analytics.py       moving averages, alert rules, presets
    data_health.py     freshness, row volume and source checks
    targets.py         goals, attainment and required pace
    snapshots.py       the archive of what was reported
  data/
    schema.py          the canonical table every source becomes
    excel_loader.py    workbook discovery, column mapping, normalisation
    postgres_loader.py the same canonical table, read from the database
    database.py        connections, and errors phrased for a human
    etl.py             Excel -> CSV -> Postgres, as a library
    repository.py      source dispatch, caching and the refresh button
  ui/
    theme.py           the stylesheet and number formatting
    components.py      KPI cards, sections, console
    charts.py          the daily trend chart
  views/
    dashboard.py       the Executive Dashboard page
    analytics.py       moving-average monitoring and alerts
    admin.py           the Admin panel
    diagnostics.py     "where did this number come from"
  admin/
    registry.py        script discovery and run-order persistence
    runner.py          subprocess execution with interactive stdin
    lock.py            single-holder run lock, with stale detection
config/                config.example.yaml (committed) + config.yaml (yours)
db/                    schema.sql and database setup notes
deploy/                systemd unit for running it as a service
docs/                  deployment.md -- LAN setup, firewall, autostart
scripts/               example pipeline scripts
tools/                 ETL, sample data, pipeline runner, alerts, snapshots
tests/                 pytest suite
runtime/               saved run order and run logs (git-ignored)
```

---

## When a number looks wrong

Open **Diagnostics** first. In order, it answers:

1. **Which config file is in force** — the warning banner tells you if you are
   still on the committed example.
2. **Which workbooks were found**, how many rows each contributed, and how many
   were dropped for an unreadable date.
3. **How every column resolved** for a chosen sheet, plus the raw header list.
4. **Which category values fell into "Other"**, with row counts.
5. **How many rows counted as web sales**, broken down by channel.
6. **What the working calendar thinks** — working days this month and year, the
   resolved holidays, and the next 30 days' closures.
7. **The first 50 rows** exactly as the app sees them.

Common fixes:

| Symptom | Cause |
|---|---|
| Everything is zero | `data.sources.path` does not resolve, or `header_row` is wrong |
| Zero rows on the database source | The ETL has not run, or `where` is filtering everything out |
| A load says 0 inserted | Already loaded — the key matched. That is the design |
| Premium is zero but sales are right | The premium column alias is missing |
| Category 1 and 2 are both zero | The workbook's category values are not in `values` |
| Rows are missing | Their date cell is text Excel never parsed — see "Skipped (bad date)" |
| A month projects too high | `count_today_as_elapsed: true` divides by a partial day |
| Everything looks like a collapse | The feed has stopped updating — check the Data health alerts first |
| Last month's figure changed | A row was restated — `python tools/snapshot_kpis.py --check` |

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite covers the working calendar (including holidays that fall on a
Sunday), the period windows and their comparison ranges, KPI aggregation and the
web-sales toggle, the projection maths, moving averages and every alert rule
(including the cases that must report "not enough data" rather than a number),
Excel ingestion against deliberately messy workbooks, the run-order store and
its path guard, and the runner's prompt-and-answer path.
