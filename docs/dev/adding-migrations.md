# Adding migrations

What this page covers: the numbered-file rule for evolving the
Postgres `mesa` schema, the append-only contract that freezes
every committed migration forever, the bookkeeping bootstrap, the
parallel edit that keeps the DuckDB-file catalog in step, and the
mechanics of writing the next migration (currently `0003_*.sql`).

Migrations are a **Postgres** concept. The DuckDB-file catalog has
no migration history. Its schema is the idempotent
`_SCHEMA_STATEMENTS` tuple in
[`../../src/mesa_ducklake/catalog_duckdb.py`](../../src/mesa_ducklake/catalog_duckdb.py),
which runs every time the file is opened. Every catalog schema change
therefore touches both places. See
[Keeping the DuckDB backend in step](#keeping-the-duckdb-backend-in-step).

## Rules in one paragraph

Schema changes are append-only. Each change is a new file in
`migrations/` named `NNNN_<description>.sql` with a four-digit
numeric prefix. The runner discovers files, sorts by prefix, applies
in ascending order, and records each application in
`mesa.schema_versions`. Once a migration is committed it is
**frozen** — never edited. Corrections are new migrations.

Shipped so far: `0001_initial.sql` (`mesa.projects`, `mesa.snapshots`)
and `0002_pending_pushes.sql` (`mesa.pending_pushes`). The next free
number is **`0003`**.

## The migration runner

The runner lives at
[`../../src/mesa_ducklake/schema.py`](../../src/mesa_ducklake/schema.py)
and exposes one function. Operators call it through the CLI:

```bash
MESA_DUCKLAKE_DSN=postgresql://mesa:mesa@localhost:5432/mesa_ducklake \
    mesa-ducklake migrate            # or: mesa-ducklake migrate --target 2
```

From Python:

```python
from mesa_ducklake.schema import apply_migrations

newly_applied = apply_migrations(
    dsn="postgresql://mesa:mesa@localhost:5432/mesa_ducklake",
    target=None,   # apply everything pending; or pass an int for an inclusive upper bound
)
```

What it does, in order:

1. Locates the `migrations/` directory at the repo root.
2. Discovers every `NNNN_*.sql` file. Two files with the same
   numeric prefix raise `ValueError` immediately — the apply order
   would be ambiguous.
3. Opens a Postgres connection with `autocommit=True` so each
   migration is its own atomic unit via `conn.transaction()`.
4. Ensures `mesa.schema_versions` exists (bootstrap, see below).
5. Reads which versions have already been applied.
6. For each pending migration:
   - Strips standalone `BEGIN;` / `COMMIT;` lines from the file
     content (the runner already wraps in a transaction; an
     embedded `COMMIT;` would prematurely end it).
   - Executes the SQL inside a fresh transaction.
   - Inserts a row into `mesa.schema_versions`.
7. Returns the count of newly applied migrations.

Idempotent: running twice in a row applies zero migrations the
second time. On error, the failing migration rolls back; the runner
stops and re-raises so the caller can fix the issue and retry.

### Wrapping `BEGIN; / COMMIT;` in migration files

Each migration file may include an explicit `BEGIN;` / `COMMIT;`
pair at the top and bottom (`0001_initial.sql` does). This makes
the file directly runnable through `psql`. The runner strips only
*standalone* `BEGIN;` and `COMMIT;` lines — embedded
`SAVEPOINT`-style transactions (if any future migration needs them)
are preserved. The regex used is:

```python
_STANDALONE_BEGIN  = re.compile(r"^\s*BEGIN\s*;\s*$",  re.IGNORECASE)
_STANDALONE_COMMIT = re.compile(r"^\s*COMMIT\s*;\s*$", re.IGNORECASE)
```

## The bookkeeping table is bootstrap, not migration

`mesa.schema_versions` is *not* declared in any `NNNN_*.sql` file.
It must exist before the runner can read which versions have been
applied — a chicken-and-egg problem solved by creating the table
unconditionally on every runner invocation, using
`CREATE TABLE IF NOT EXISTS`:

```sql
CREATE SCHEMA IF NOT EXISTS mesa;

CREATE TABLE IF NOT EXISTS mesa.schema_versions (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    filename   TEXT NOT NULL
);
```

This bootstrap runs *outside* the per-migration transactions and is
idempotent. Concurrent runner invocations on a fresh database race
harmlessly thanks to `IF NOT EXISTS`.

When you write a new migration, do **not** include
`mesa.schema_versions` DDL — the bootstrap already owns it.

## Why committed migrations are frozen

The append-only contract is not just convention; it is a
correctness requirement.

- Some deployment already has `mesa.schema_versions.version = 1`
  (and `2`) written against the *current* content of those files.
  Suppose the file changes. The bookkeeping still reports "version
  1 is applied", but the live database does not have the new shape,
  and the runner will never reapply it because version 1 is already
  recorded.
- An always-on rule: **never edit a committed migration**.
  Corrections become new migrations.

## Writing a new migration

Suppose you need to add a `mesa.projects.deleted_at TIMESTAMPTZ`
column to support soft-delete. Steps:

1. **Pick the next number.** Look at the current highest prefix in
   `migrations/`. Today that is `0002`, so your new file is `0003`.
   Use four digits, zero-padded. If another open PR also claims
   `0003`, whichever merges second renumbers.
2. **Choose a descriptive name.**
   `0003_projects_soft_delete.sql` is good;
   `0003_changes.sql` is not.
3. **Write the file.** Wrap your DDL in `BEGIN;` / `COMMIT;` so the
   file can be run directly with `psql`. Open with a header comment
   explaining the user-facing problem the change solves. Schema
   changes are expensive, and the justification needs to live with
   them. Example:

   ```sql
   -- 0003_projects_soft_delete.sql
   --
   -- Purpose: support archiving a project without losing its history.
   -- Status today is 'active' | 'archived' but there is no way to
   -- record *when* the archive happened, which the upcoming
   -- export-to-Glacier workflow needs to schedule retention.
   --
   -- Backward compat: column is nullable; nothing reads it yet.
   -- Schema changes are append-only; never edit this file once
   -- committed.

   BEGIN;

   ALTER TABLE mesa.projects
       ADD COLUMN deleted_at TIMESTAMPTZ;

   COMMIT;
   ```

4. **Mirror it in the DuckDB backend.** Edit `_SCHEMA_STATEMENTS` in
   `src/mesa_ducklake/catalog_duckdb.py` (see
   [below](#keeping-the-duckdb-backend-in-step)). Also update the row
   mappers (`_row_to_*`) and the `SELECT`/`RETURNING` column lists in
   *both* `catalog.py` and `catalog_duckdb.py` if the column should
   surface on a model.
5. **Update `models.py`** to reflect the new field, with a sensible
   default so old callers do not break.
6. **Update `DuckLakeClient`** only if the new field is part of
   the public contract. A column that exists purely for catalog
   bookkeeping does not need a public method.
7. **Update the docs.** Add the column to
   [`schema.md`](./schema.md), including the DuckDB-differences
   table if the two backends' types differ.
8. **Write tests.**
   - For Postgres, round-trip the new column through
     `apply_migrations` in `tests/test_schema.py` or
     `tests/test_catalog.py` (marked `requires_postgres`).
   - For DuckDB, round-trip the same column in
     `tests/test_catalog_duckdb.py`. Include re-opening an
     **existing** file, so you prove the bootstrap upgrades it.
   - For Parquet columns, also add a regression test for the
     affected query in `tests/test_lake.py` or
     `tests/test_client_e2e.py` / `tests/test_client_e2e_duckdb.py`.
9. **Apply and verify locally.** Run `mesa-ducklake migrate` against a
   scratch Postgres (expect `"applied": 1`), then run it again (expect
   `"applied": 0`).
10. **Commit and PR.** Call out the new migration number in the PR
    description. See [`contributing.md`](./contributing.md).

A Claude Code skill walks through this checklist:
[`../../.claude/skills/add-migration/SKILL.md`](../../.claude/skills/add-migration/SKILL.md).

### Keeping the DuckDB backend in step

`DuckDBCatalogStore` runs every statement in `_SCHEMA_STATEMENTS` on
every open. That imposes some rules:

- **Every statement must be idempotent**: `CREATE … IF NOT EXISTS`,
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, and so on. There is no
  version table to skip statements that have already run.
- **Append new statements; do not edit old ones in place.** Changing
  an existing `CREATE TABLE IF NOT EXISTS` body affects only *new*
  files. Existing `.duckdb` files already have the table, so the edit
  is silently skipped. To change an existing table, append an
  `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` statement.
- **Keep types compatible, not identical.** For example, `project_id`
  is `UUID` in Postgres and `TEXT` in DuckDB. Record any new
  difference in the [schema differences table](./schema.md#duckdb-catalog-differences).
- **No foreign keys.** The DuckDB schema omits them deliberately.
- `mesa-ducklake migrate` against a DuckDB DSN just opens the file,
  which runs the bootstrap, and reports
  `{"applied": 0, "target": null, "backend": "duckdb"}`.

### Migrations that touch Parquet

The Parquet `avu_changes` schema is mirrored in two places:

- `_STAGING_DDL` in `src/mesa_ducklake/lake.py` — the in-memory
  staging table used for writes.
- `_PARQUET_COLUMNS` in the same file — the column-order tuple
  used for COPY and reads.

A migration that adds a Parquet column must:

1. Add the column at the **end** of both `_STAGING_DDL` and
   `_PARQUET_COLUMNS`. Never reorder.
2. Be nullable. Old Parquet files do not have the column;
   DuckDB's `read_parquet` will read them as `NULL` for the new
   column when schema evolution is enabled.
3. Be reflected in the Pydantic `AvuChange` with a sensible
   default.

There is no per-Parquet-file DDL to migrate (Parquet files are
self-describing). The "migration" for a Parquet column is the
code change in `lake.py` plus the SQL migration that records the
new column in any catalog indexing.

## Manual run for development

To apply migrations from a checkout:

```bash
export MESA_DUCKLAKE_DSN=postgresql://mesa:mesa@localhost:5432/mesa_ducklake
mesa-ducklake migrate              # {"applied": N, "target": null, "backend": "postgres"}
mesa-ducklake migrate --target 2   # applies 0001 and 0002, skips 0003+
```

The Python equivalent is `apply_migrations(dsn, target=2)`.

## See also

- [`schema.md`](./schema.md) — the current Postgres and Parquet
  schemas.
- [`architecture.md`](./architecture.md) — where the migration
  runner sits in the module map.
- [`../deploy/postgres.md`](../deploy/postgres.md) — running
  `mesa-ducklake migrate` against a production catalog database.
- [`../deploy/duckdb-catalog.md`](../deploy/duckdb-catalog.md) — the
  DuckDB-file backend, which has no migrations.
- [`../../src/mesa_ducklake/schema.py`](../../src/mesa_ducklake/schema.py) —
  the runner itself.
