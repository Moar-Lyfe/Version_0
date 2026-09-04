-- Executive Dashboard -- reporting schema
--
-- Apply with:
--     psql -h localhost -U dashboard -d reporting -f db/schema.sql
--
-- Every statement is idempotent, so it is safe to re-run after an upgrade.
--
-- DESIGN NOTE -- facts here, interpretation in config.yaml.
-- The table stores what the workbook said: the raw category string, the raw
-- channel. It does NOT store "is this Category 1" or "is this a web sale",
-- because those are reporting decisions. They are derived when the data is
-- read, so changing which products count as Category 1 is a one-line config
-- edit that moves every figure -- no reload, no migration, and history is not
-- rewritten by a definition change.

CREATE TABLE IF NOT EXISTS sales (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Natural key from the source row, used to make loads idempotent. Built by
    -- the ETL from the configured key fields (policy number by default), so
    -- re-running the same workbook updates rather than duplicates.
    source_key    TEXT        NOT NULL,

    sale_date     DATE        NOT NULL,
    agent         TEXT        NOT NULL DEFAULT 'Unassigned',
    category      TEXT        NOT NULL DEFAULT '',
    channel       TEXT        NOT NULL DEFAULT '',
    policy_id     TEXT        NOT NULL DEFAULT '',
    premium       NUMERIC(14, 2) NOT NULL DEFAULT 0,
    units         NUMERIC(12, 3) NOT NULL DEFAULT 1,

    -- Provenance: which workbook, sheet and spreadsheet row this came from.
    -- The first question after "that number looks wrong" is "where did it come
    -- from", and this answers it without opening a file.
    source_file   TEXT,
    source_sheet  TEXT,
    source_row    INTEGER,

    loaded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The uniqueness that makes an incremental load safe to re-run.
CREATE UNIQUE INDEX IF NOT EXISTS sales_source_key_uidx ON sales (source_key);

-- Reporting reads by date first, always.
CREATE INDEX IF NOT EXISTS sales_sale_date_idx ON sales (sale_date);
CREATE INDEX IF NOT EXISTS sales_agent_idx     ON sales (agent);
CREATE INDEX IF NOT EXISTS sales_category_idx  ON sales (category);

-- Keep updated_at honest without the ETL having to remember.
CREATE OR REPLACE FUNCTION sales_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sales_touch_updated_at ON sales;
CREATE TRIGGER sales_touch_updated_at
    BEFORE UPDATE ON sales
    FOR EACH ROW EXECUTE FUNCTION sales_touch_updated_at();


-- Load history. What ran, when, and what it did -- so "why did last night's
-- numbers move?" has an answer that does not depend on anyone's memory.
CREATE TABLE IF NOT EXISTS etl_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    source_file   TEXT,
    source_sheet  TEXT,
    csv_path      TEXT,
    rows_read     INTEGER NOT NULL DEFAULT 0,
    rows_staged   INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    rows_updated  INTEGER NOT NULL DEFAULT 0,
    rows_skipped  INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'running',
    message       TEXT
);

CREATE INDEX IF NOT EXISTS etl_runs_started_at_idx ON etl_runs (started_at DESC);
