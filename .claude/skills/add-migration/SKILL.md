---
name: add-migration
description: Use when changing the mesa-ducklake catalog schema. That means adding, altering or indexing a column or table in mesa.projects, mesa.snapshots or mesa.pending_pushes, or adding a column to the Parquet avu_changes fact table. The skill covers the new numbered Postgres migration, its DuckDB twin in catalog_duckdb._SCHEMA_STATEMENTS, the models, docs and round-trip tests. Do not use it for query-only or client-only changes.
---

# Add a catalog migration

Hard rule: **never edit a committed file in `migrations/`**. Every
correction is a new numbered migration. Background:
`docs/dev/adding-migrations.md` and `docs/dev/schema.md`.

## Checklist

1. **Pick the number.** Run `ls migrations/`. The next free prefix is
   the highest plus one, four digits (`0001` and `0002` ship today,
   so the next is `0003`). Say the number in the PR description.
2. **Write `migrations/NNNN_<descriptive_name>.sql`.**
   - Start with a header comment covering the purpose (the
     user-facing problem), backward compatibility, and the line
     "never edit this file once committed".
   - Wrap the DDL in `BEGIN;` / `COMMIT;`.
   - Use `IF NOT EXISTS` where possible. New columns are nullable or
     have a default.
   - Do not touch `mesa.schema_versions`; the runner owns it.
3. **If you add a column to `avu_changes` (Parquet):**
   - The migration comment must say why the AVU triple
     `(attribute, value, unit)` cannot carry the information. The
     triple is a hard contract with iRODS iCAT, and the default
     answer is "use an AVU".
   - Also add a short doc note to `docs/dev/schema.md`, as CLAUDE.md
     requires.
   - Append the column at the **end** of both `_STAGING_DDL` and
     `_PARQUET_COLUMNS` in `src/mesa_ducklake/lake.py`. Never
     reorder. It must be nullable.
4. **Mirror the change in the DuckDB backend:**
   `src/mesa_ducklake/catalog_duckdb.py` → `_SCHEMA_STATEMENTS`.
   - Append new idempotent statements (`CREATE … IF NOT EXISTS`,
     `ALTER TABLE … ADD COLUMN IF NOT EXISTS …`). Do not edit an
     existing `CREATE TABLE` body: existing `.duckdb` files would
     never see the change.
   - No foreign keys. `project_id` stays `TEXT`.
5. **Update the row mappers and column lists in both backends.**
   `_row_to_*` and the `SELECT` / `RETURNING` lists live in
   `src/mesa_ducklake/catalog.py` and `src/mesa_ducklake/catalog_duckdb.py`.
6. **Update `src/mesa_ducklake/models.py`.** Add the field with a
   default, so old callers and old rows still validate.
7. **Change `DuckLakeClient`** (`src/mesa_ducklake/client.py`) only if
   the field is part of the public contract.
8. **Update the docs:**
   - `docs/dev/schema.md`: the table section, plus the "DuckDB catalog
     differences" table if the types differ.
   - `docs/dev/adding-migrations.md`: bump the "next free number".
   - `docs/deploy/postgres.md`: the expected `"applied": N` on a fresh
     database.
9. **Tests:**
   - Postgres: round-trip through `apply_migrations` in
     `tests/test_schema.py` / `tests/test_catalog.py`, marked
     `@pytest.mark.requires_postgres`.
   - DuckDB: round-trip in `tests/test_catalog_duckdb.py`, including
     re-opening a file created *before* the change.
   - Parquet column: add a regression test in `tests/test_lake.py`
     and in `tests/test_client_e2e.py` / `tests/test_client_e2e_duckdb.py`.
10. **Verify:**

    ```bash
    ruff check src/ tests/
    pytest -q                                              # check the skip count
    MESA_DUCKLAKE_TEST_PG_HOST=localhost pytest -q -m requires_postgres
    MESA_DUCKLAKE_DSN=postgresql://postgres:postgres@localhost:5432/scratch mesa-ducklake migrate   # {"applied": 1, ...}
    MESA_DUCKLAKE_DSN=postgresql://postgres:postgres@localhost:5432/scratch mesa-ducklake migrate   # {"applied": 0, ...}
    MESA_DUCKLAKE_DSN=duckdb:///tmp/scratch.duckdb mesa-ducklake migrate                           # {"applied": 0, ..., "backend": "duckdb"}
    ```

11. Ask the `contract-reviewer` agent to review the diff before the PR.
