-- 0001_initial.sql
--
-- Purpose: bootstrap the Postgres `mesa` catalog schema for mesa-ducklake.
--
-- This catalog is an *index* of MESA-enabled iRODS projects and the
-- snapshots taken against them. It deliberately holds no AVU rows
-- itself — the AVU change fact table (`avu_changes`) lives in Parquet
-- files under each project's `<project_root>/.mesa/ducklake/`
-- collection in iRODS, written via DuckLake. "Metadata travels with
-- the data" is a hard project constraint.
--
-- Conceptual schema of the Parquet `avu_changes` fact table (for
-- reference; not created here):
--   project_id       UUID
--   snapshot_id      BIGINT
--   irods_path       TEXT
--   target_type      TEXT       -- data_object | collection | resource | user
--   attribute        TEXT
--   value            TEXT
--   unit             TEXT       -- often the ontology CURIE for OBO/OLS AVUs
--   op               TEXT       -- add | delete
--   actor            TEXT       -- iRODS user who authored the change
--   ts               TIMESTAMPTZ
--   source           TEXT       -- 'mesa-mcp' | 'irods-rule:<event>' | etc.
--   via_ticket       TEXT       -- iRODS ticket id when applicable; NULL otherwise
--   rule_invocation  TEXT       -- name of the iRODS rule that emitted the change
--
-- Next migrations (not in this PR) will add:
--   * `mesa.schema_versions` for the migration runner's bookkeeping.
--   * Whatever additional catalog-side indexing turns out to be needed
--     once the read patterns from mesa-mcp settle.
-- Schema changes are append-only; never edit this file once committed.

BEGIN;

CREATE SCHEMA IF NOT EXISTS mesa;

-- Registry of MESA-enabled iRODS projects.
-- One row per project; the `irods_path` is the root collection.
CREATE TABLE IF NOT EXISTS mesa.projects (
    project_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    irods_path       TEXT NOT NULL UNIQUE,        -- e.g. /iplant/home/alice/myproj
    irods_zone       TEXT NOT NULL,
    ducklake_path    TEXT NOT NULL,               -- e.g. <irods_path>/.mesa/ducklake
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT NOT NULL,               -- iRODS username
    status           TEXT NOT NULL DEFAULT 'active' -- 'active' | 'archived'
);

-- One row per atomic batch of AVU changes (one user action = one snapshot).
-- The actual AVU rows live in the Parquet file referenced by `parquet_file`.
CREATE TABLE IF NOT EXISTS mesa.snapshots (
    snapshot_id      BIGSERIAL PRIMARY KEY,
    project_id       UUID NOT NULL REFERENCES mesa.projects(project_id),
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor            TEXT NOT NULL,               -- iRODS user who made the change
    parent_snapshot  BIGINT REFERENCES mesa.snapshots(snapshot_id),
    note             TEXT,                        -- optional commit message
    parquet_file     TEXT NOT NULL                -- path within ducklake_path
);

CREATE INDEX IF NOT EXISTS snapshots_project_ts_idx
    ON mesa.snapshots (project_id, ts);

COMMIT;
