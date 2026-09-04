# Database

The reporting database the dashboard reads from. Excel remains the operational
front end; `tools/etl_excel_to_postgres.py` moves each export in.

## First-time setup

```bash
createdb reporting
createuser dashboard --pwprompt
psql -d reporting -f db/schema.sql
psql -d reporting -c "GRANT SELECT, INSERT, UPDATE ON sales, etl_runs TO dashboard;"
psql -d reporting -c "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO dashboard;"
```

Then point `config/config.yaml` at it:

```yaml
data:
  source_type: "postgres"
  postgres:
    host: "localhost"
    database: "reporting"
    user: "dashboard"
    password_env: "EXEC_DASH_DB_PASSWORD"
    table: "sales"
```

The password never goes in the config file — it is read from the named
environment variable, or left to libpq (`~/.pgpass`, `PGPASSWORD`) when
`password_env` is null.

## Least privilege

The dashboard only ever reads. If you want that enforced rather than trusted,
give it its own role:

```sql
CREATE ROLE dashboard_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE reporting TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON sales TO dashboard_ro;
```

Point the dashboard at `dashboard_ro` and let only the ETL account write.

## Mapping an existing table

If `sales` already exists with different column names, map them rather than
renaming anything:

```yaml
data:
  postgres:
    table: "sales"
    columns:
      date: "effective_date"
      premium: "written_premium"
      agent: "producer_name"
      category: "product_line"
      channel: "lead_source"
      policy_id: "policy_number"
      count: "units"
    where: "status <> 'VOID'"      # optional, without the WHERE keyword
```

Only `date` is required. Anything unmapped falls back to a sensible default
(premium 0, one sale per row, agent `Unassigned`), and Diagnostics shows exactly
what resolved.

## Backups

```bash
python tools/backup_database.py          # dump, verify, rotate (keeps 14)
python tools/backup_database.py --list
```

Dumps land in `runtime/backups/` in PostgreSQL's custom format, and each one is
read back with `pg_restore --list` immediately after it is written — a backup
nobody has ever read is a hope, not a backup. A dump that fails partway is
deleted rather than left behind looking like one.

Restore into a **scratch database**, check it, and only then decide:

```bash
createdb reporting_restore
pg_restore -d reporting_restore --no-owner runtime/backups/reporting_<stamp>.dump
psql -d reporting_restore -c "SELECT count(*), min(sale_date), max(sale_date) FROM sales;"
```

To recover a single table rather than everything:

```bash
pg_restore -d reporting --no-owner --data-only --table=sales runtime/backups/<file>.dump
```

`runtime/backups/` is on the same machine as the database. Point `--dir` at a
share, or copy them off — otherwise one disk failure takes both the database and
its backups.

## Reconciliation

The ETL inserts and updates; it never deletes. A policy voided in Excel keeps
being counted until somebody looks:

```bash
python tools/etl_excel_to_postgres.py --reconcile          # report only
python tools/etl_excel_to_postgres.py --reconcile --prune  # delete, after asking
```

This scans every workbook `data.sources` matches and only judges rows whose
`source_file` is among the files it actually read. Archiving an old export
therefore takes its rows out of scope rather than marking them for deletion.
