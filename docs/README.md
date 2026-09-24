# mesa-ducklake documentation

What this page covers: the entry point to mesa-ducklake's user,
developer and deployment documentation. mesa-ducklake is the Python
library that tracks AVU (Attribute / Value / Unit) metadata history
for MESA-enabled iRODS projects. It uses DuckDB's DuckLake lakehouse
pattern: a catalog plus per-project Parquet files. The catalog runs on
Postgres, or on a single DuckDB file for single-user installs.

Start here:

- **Library users** writing Python that imports `mesa_ducklake`: read
  [`user/usage.md`](./user/usage.md).
- **iRODS rule authors and operators** who wire rule callbacks into
  the CLI, or who run `recover` / `migrate`: read
  [`user/cli.md`](./user/cli.md).
- **Analysts and auditors** who need to see what AVUs were set on a
  path at a past timestamp: read [`user/time-travel.md`](./user/time-travel.md).
- **Contributors** changing schema, queries or client code: read
  [`dev/architecture.md`](./dev/architecture.md) and
  [`dev/contributing.md`](./dev/contributing.md).
- **Operators** standing up a deployment: read
  [`deploy/postgres.md`](./deploy/postgres.md) for hosted setups, or
  [`deploy/duckdb-catalog.md`](./deploy/duckdb-catalog.md) for a
  single-user install.

mesa-mcp has three relevant modes:

- **Hosted service:** set `ducklake.catalog_dsn` to a Postgres DSN.
- **Local-install:** set `ducklake.catalog_dsn` to a
  `duckdb:///abs/path.duckdb` DSN for a zero-server catalog.
- **VICE-app, or history not wanted:** leave `ducklake.catalog_dsn`
  blank. AVU writes succeed but are not recorded.

See [mesa-mcp's user docs](https://github.com/idss-mesa/mesa-mcp/tree/main/docs/user)
for those modes.

## Table of contents

### User

- [Usage](./user/usage.md): the `DuckLakeClient` Python API, including
  constructor options (`catalog_dsn`, `cache_dir`, `cache_cap_bytes`,
  `data_collection`) and crash recovery.
- [CLI](./user/cli.md): `mesa-ducklake record` (rule callback),
  `recover` (drain the write-ahead log) and `migrate` (prepare the
  catalog), with exit codes.
- [Time travel](./user/time-travel.md): reading AVUs as of a past
  timestamp; history and diff patterns.

### Developer

- [Architecture](./dev/architecture.md): the catalog / lake split, the
  `CatalogStore` Protocol with its Postgres and DuckDB backends, the
  iRODS sync sidecar, and the push-before-commit write flow.
- [Schema](./dev/schema.md): `mesa.projects`, `mesa.snapshots`,
  `mesa.pending_pushes`, the `parquet_file` sentinels, the DuckDB
  differences, and the Parquet `avu_changes` fact table.
- [Adding migrations](./dev/adding-migrations.md): numbering rule,
  append-only contract, and the DuckDB `_SCHEMA_STATEMENTS` twin.
- [Queries](./dev/queries.md): the `EFFECTIVE_AVUS_AS_OF_SQL` contract,
  and why the partition key is the full triple.
- [Contributing](./dev/contributing.md): PR conventions, test loop,
  and the Claude Code agents and skills.
- [LLM end-to-end tests](./dev/llm-e2e-tests.md): the opt-in
  `live_e2e` and `llm_e2e` tiers.
- [Architecture review, 2026-09](./dev/architecture-review-2026-09.md):
  point-in-time review of the current design.

### Deploy

- [Postgres](./deploy/postgres.md): Ubuntu 24.04 provisioning of the
  catalog database and `mesa-ducklake migrate`.
- [DuckDB catalog](./deploy/duckdb-catalog.md): the single-file,
  single-writer catalog for local installs.
- [Backup](./deploy/backup.md): daily `pg_dump` to iRODS, the
  recovery procedure, and backing up a DuckDB-file catalog.
- [iRODS rules](./deploy/irods-rules.md): installing the rule
  callbacks on an iRODS server (shipped, not yet deployed in
  production).
- [Per-project storage](./deploy/per-project-storage.md): the
  `/.mesa/ducklake/` layout, the configurable `data_collection`, ACLs
  and capacity.

### Design records

- [`design/`](./design/README.md): design records and historical
  implementation plans, such as the DuckDB catalog backend, plus the
  convention for adding new ones.

## See also

- [`../CLAUDE.md`](../CLAUDE.md): full architecture context, hard
  contracts, and the conceptual schema.
- [`../AGENTS.md`](../AGENTS.md): short vendor-neutral guide for
  coding agents.
- [`../README.md`](../README.md): the repo's minimal introduction.
- [`../NEXT_STEPS.md`](../NEXT_STEPS.md): current status and open work.
- [`../migrations/`](../migrations/): the Postgres schema.
