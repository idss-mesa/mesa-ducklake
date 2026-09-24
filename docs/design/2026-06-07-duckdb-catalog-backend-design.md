# DuckDB catalog backend for mesa-ducklake

- **Status:** Approved (design)
- **Date:** 2026-06-07
- **Author:** tswetnam (with Claude Code)
- **Scope:** mesa-ducklake (library); one-line touch in mesa-mcp

## Context & problem

mesa-ducklake tracks AVU history with a **catalog** (project + snapshot registry)
plus a **data plane** (one Parquet file per snapshot, stored in each project's
iRODS `<root>/.mesa/ducklake/`). Today the catalog is **Postgres-only**
(`catalog.py` + `schema.py` use `psycopg`, `gen_random_uuid()`, `BIGSERIAL`,
`%s` params, `RETURNING`, `ON CONFLICT`). A user who wants AVU history must
stand up Postgres.

This blocks the single-user / local-install / VICE-app path: people who just
want history for their own iRODS project shouldn't need an external database.
We want mesa-ducklake to **work on whatever platform a user is comfortable
with** — Postgres for concurrent/hosted deployments, a self-contained DuckDB
file for single-user local use.

Two facts make this cheap:
1. The **data plane (`lake.py`) is already pure DuckDB + plain Parquet** — no
   Postgres anywhere. `COPY … TO … (FORMAT PARQUET)` writes; `read_parquet([…])`
   reads. No change needed.
2. The entire Postgres dependency lives in **one ~430-line module**
   (`catalog.py`) plus its migration SQL. The `DuckLakeClient` facade composes
   catalog + lake + irods_sync, so swapping the catalog is contained.

## Goals

- A `DuckDBCatalogStore` backed by a single local DuckDB file, functionally
  equivalent to the Postgres catalog for the methods `DuckLakeClient` and
  `irods_sync` use.
- Backend selected automatically from the existing `catalog_dsn` (no new config
  surface): `postgresql://`/`postgres://` → Postgres; `duckdb://…` URI or a
  `*.duckdb` filesystem path → DuckDB; blank → disabled.
- Existing Postgres behavior and tests unchanged.
- The data plane (Parquet in iRODS `.mesa/ducklake/`) and the
  `DuckLakeClient` public API are unchanged. AVU history still "travels with
  the data."

## Non-goals (this iteration)

- Replacing the hand-rolled catalog/lake with the real DuckDB **`ducklake`
  extension** (`ATTACH 'ducklake:…'`). Worth a future spec; it re-architects
  working code.
- Multi-writer/concurrent access to a DuckDB-file catalog (single-writer only —
  see Tradeoffs).
- A SQLite backend (the factory leaves room for it later).
- Migrating the iRODS-rule-callback / CLI provenance paths beyond what the
  shared factory gives them for free.

## Design

### Architecture

```
DuckLakeClient (facade, unchanged public API)
  ├── open_catalog(catalog_dsn) ─► CatalogStore  (Protocol)
  │        ├── PostgresCatalogStore   (psycopg)        ← existing, renamed
  │        └── DuckDBCatalogStore     (duckdb file)    ← NEW
  ├── LakeStore (DuckDB + Parquet)                      ← unchanged
  └── irods_sync (push Parquet to iRODS .mesa/ducklake) ← unchanged
```

The catalog is the only swapped layer. With the DuckDB backend the catalog is a
local `*.duckdb` file; the AVU-history Parquet still lands in iRODS.

### Components

**`catalog_base.py` (new) — the interface.**
A `typing.Protocol` named `CatalogStore` enumerating exactly the methods the
rest of the package calls:
`register_project`, `get_project`, `find_project_by_path`, `create_snapshot`,
`update_snapshot_parquet_file`, `delete_snapshot`, `latest_snapshot_id`,
`list_snapshots`, `get_snapshot`, `snapshot_ts`, `insert_pending_push`,
`delete_pending_push`, `bump_pending_push_attempt`, `list_pending_pushes`,
`get_pending_push`, `close`. Methods return the existing Pydantic models
(`Project`, `Snapshot`, `PendingPush`) — those are shared, not duplicated.

**`catalog.py` — Postgres impl + factory.**
- Rename class `CatalogStore` → `PostgresCatalogStore` (body unchanged).
- Add `open_catalog(dsn: str) -> CatalogStore`:
  - `postgresql://` / `postgres://` (or a libpq keyword DSN like `host=… dbname=…`) → `PostgresCatalogStore(dsn)`
  - `duckdb:` URI or a path ending `.duckdb` → `DuckDBCatalogStore(path)`
  - empty/None → `ValueError` (callers gate on "blank → disabled" before calling)
  - unrecognized → `ValueError` with a clear message.
- `duckdb:` path resolution — accept exactly two unambiguous forms (avoids the
  `file://` netloc trap):
  - `duckdb:///abs/path.duckdb` (triple slash) → absolute path `/abs/path.duckdb`
  - a bare filesystem path ending in `.duckdb` (absolute or relative), with no scheme
  - `:memory:` (or `duckdb://:memory:`) → in-memory DuckDB, for tests only.
  Parent dirs are created on open.

**`catalog_duckdb.py` (new) — DuckDB impl.**
`DuckDBCatalogStore(path)` opens one `duckdb.connect(path)`, runs an **idempotent
inline schema bootstrap**, and keeps the connection (closed by `close()`). Same
method surface as Postgres. DuckDB-dialect notes:
- `CREATE SCHEMA IF NOT EXISTS mesa`.
- `project_id UUID DEFAULT uuid()` (DuckDB's `uuid()` = random UUIDv4); bind/return `uuid.UUID`.
- `snapshot_id BIGINT DEFAULT nextval('mesa.snapshots_seq')` with
  `CREATE SEQUENCE IF NOT EXISTS mesa.snapshots_seq`.
- `TIMESTAMPTZ NOT NULL DEFAULT now()`.
- `?` placeholders (qmark); `RETURNING` for generated columns.
- `pending_pushes` insert keeps the **insert-then-select** idempotency the
  Postgres code uses (DuckDB `ON CONFLICT DO NOTHING` if reliable in our pinned
  version, else a pre-check `SELECT`).
- **FK constraints omitted** in the DuckDB schema (DuckDB self-referencing FKs
  are limited); integrity is app-enforced exactly as the write protocol already
  does. `delete_snapshot` deletes the matching `pending_pushes` child explicitly to
  mirror the Postgres `ON DELETE CASCADE`. The schema is bootstrapped inline and
  idempotently (`CREATE … IF NOT EXISTS`); a versioned migration runner for the
  DuckDB dialect is deferred (see Future work) — no `schema_versions` table is
  created in this iteration.
- Row→model: a small `_dicts(cur)` helper zips `cur.description` with each tuple
  so the existing `_row_to_project/_row_to_snapshot/_row_to_pending_push`
  converters are reused.

**`client.py` — facade wiring.**
- Add constructor param `catalog_dsn: str`; keep `postgres_dsn` as a deprecated
  alias (one of the two must be supplied; `postgres_dsn` still accepted so no
  existing caller breaks).
- Replace `CatalogStore(self._postgres_dsn)` with `open_catalog(self._catalog_dsn)`.
- No other facade changes; the lake/sync/cache paths are untouched.

**mesa-mcp wiring.**
- `mesa-mcp/src/mesa_mcp/ducklake/client.py`: change the one kwarg
  `"postgres_dsn": dsn` → `"catalog_dsn": dsn`.
- `DuckLakeConfig.catalog_dsn` is already a free-form optional string with no
  scheme validation, so `duckdb://…` flows through untouched. No config schema
  change required.

### Data flow (unchanged except catalog driver)

`record_changes`: allocate snapshot row (catalog) → WAL row (catalog) → write
Parquet locally (lake) → push Parquet to iRODS `.mesa/ducklake/` (irods_sync) →
flip `parquet_file` to real name (catalog commit point) → drop WAL row. Only the
"catalog" steps change driver; the Parquet/iRODS steps are identical.

### Error handling

- `open_catalog` raises `ValueError` on blank/unrecognized DSN (callers already
  treat blank as "DuckLake disabled" before reaching the factory).
- DuckDB file-lock contention surfaces as the native DuckDB error; documented as
  the single-writer limitation rather than silently swallowed.
- Duplicate `irods_path` still raises (UNIQUE on `mesa.projects.irods_path`),
  matching `test_register_project_rejects_duplicate_path`.

## Tradeoffs

- **Single-writer.** A DuckDB-file catalog is held by one process via a file
  lock. Perfect for the single-user stdio/VICE install (one mesa-mcp process).
  Concurrent/hosted multi-writer deployments must use Postgres. The factory
  makes this a per-deployment config choice, and the limitation is documented in
  `catalog_duckdb.py` and `docs/deploy/`.
- **No external DB vs. operational maturity.** DuckDB gives zero-dependency
  local history; Postgres keeps transactional cross-process snapshot allocation
  for production. We ship both rather than forcing one.

## Testing (TDD)

- `tests/test_catalog_duckdb.py` — mirrors `test_catalog.py` against a
  `tmp_path/*.duckdb` file. No ephemeral Postgres → fast, runs anywhere
  (no `requires_postgres` marker). Covers register/get/find, duplicate-path
  rejection, snapshot chain, `update_snapshot_parquet_file`,
  `latest_snapshot_id` pending-filtering, and pending-push CRUD/idempotency.
- `tests/test_open_catalog.py` — factory dispatch: `postgresql://` →
  `PostgresCatalogStore`; `duckdb://…` and `*.duckdb` → `DuckDBCatalogStore`;
  blank/garbage → `ValueError`.
- `tests/test_client_e2e_duckdb.py` — `DuckLakeClient(catalog_dsn="duckdb://…")`
  in local-only mode (no iRODS session): `register_project` → `record_changes`
  (two snapshots) → `get_history`, `get_avus_as_of`, `diff` return correct
  effective/raw sets. Proves catalog + lake compose over DuckDB end-to-end.
- Existing suite stays green; only `test_catalog.py` (+ conftest references)
  change the import/fixture from `CatalogStore` to `PostgresCatalogStore`.
- `ruff check` + `mypy` clean on changed files.

## Rollout / demo

The venv install is editable, so code changes are picked up on server restart.
1. Land the library change (tests green).
2. Set `mesa-mcp/config.yaml`:
   `ducklake.catalog_dsn: duckdb:///Users/tswetnam/Desktop/mesa-ai-test/.mesa/catalog.duckdb`.
3. **Restart the mesa-mcp MCP server** (reads config at startup → needs a
   reconnect). This briefly interrupts mesa-mcp tools in the session.
4. `mesa_ducklake_init_project` on `/iplant/home/tswetnam/Alcock_leafPhenotypingImages_2016`
   → creates `.mesa/ducklake/`, sets `mesa.enabled=true`, registers the project
   in the DuckDB catalog.
5. Run the paused Haiku metadata workflow → every `mesa_avu_apply_term` /
   `ds_add_avu` mirrors into DuckLake history (catalog in DuckDB, Parquet in
   iRODS). Verify with `DuckLakeClient.get_history` against the catalog file.

## Future work

- Real DuckDB `ducklake` extension as a third backend (catalog = one ATTACH
  string over DuckDB/SQLite/Postgres/MySQL).
- SQLite catalog backend (same factory seam).
- DuckDB-dialect migration files + runner if the inline bootstrap grows beyond a
  single DDL.
