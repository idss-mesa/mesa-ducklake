# mesa-ducklake

`mesa-ducklake` is a Python library that tracks **AVU (Attribute / Value / Unit)
metadata history** for iRODS collections and data objects in MESA-enabled
projects. It uses DuckDB's DuckLake lakehouse pattern: a Postgres catalog plus
Parquet data files stored at `<project_root>/.mesa/ducklake/` inside the iRODS
project itself, so the metadata history travels with the data.

Its primary consumer is the sibling project [`mesa-mcp`](https://github.com/cyverse/mesa-mcp) —
an MCP server that exposes CyVerse Data Store operations plus OBO/OLS-driven
metadata creation. Every AVU change made through `mesa-mcp` is mirrored into
`mesa-ducklake`.

When mesa-mcp is run in **local-install** or **VICE-app** mode (rather than
the hosted-service mode), the DuckLake catalog is usually skipped — users
leave `catalog_dsn` blank and AVU writes still succeed but are not
recorded. See [mesa-mcp's vice-apps and local-install
docs](https://github.com/cyverse/mesa-mcp/tree/main/docs/user) for those
modes.

## Documentation

Full documentation lives under [`docs/`](./docs/README.md), split into three
audiences:

- **Users** — [`docs/user/usage.md`](./docs/user/usage.md) for the
  `DuckLakeClient` Python API, [`docs/user/cli.md`](./docs/user/cli.md) for the
  `mesa-ducklake record` callback CLI, and
  [`docs/user/time-travel.md`](./docs/user/time-travel.md) for `get_avus_as_of`
  patterns.
- **Developers** — [`docs/dev/architecture.md`](./docs/dev/architecture.md),
  [`docs/dev/schema.md`](./docs/dev/schema.md),
  [`docs/dev/adding-migrations.md`](./docs/dev/adding-migrations.md),
  [`docs/dev/queries.md`](./docs/dev/queries.md), and
  [`docs/dev/contributing.md`](./docs/dev/contributing.md).
- **Operators** — [`docs/deploy/postgres.md`](./docs/deploy/postgres.md),
  [`docs/deploy/backup.md`](./docs/deploy/backup.md) for the daily
  ``pg_dump`` to iRODS and recovery procedure,
  [`docs/deploy/irods-rules.md`](./docs/deploy/irods-rules.md), and
  [`docs/deploy/per-project-storage.md`](./docs/deploy/per-project-storage.md).

The index page is [`docs/README.md`](./docs/README.md).

## Public API

```python
from mesa_ducklake import DuckLakeClient, AvuChange

with DuckLakeClient(postgres_dsn=..., irods_session=...) as client:
    project = client.register_project(
        irods_path="/iplant/home/alice/myproj",
        actor="alice",
        zone="iplant",
    )
    snap = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[AvuChange(...)],
        note="Tagged file.csv with ENVO biome",
    )
    avus = client.get_avus(project.project_id, irods_path=...)
```

`DuckLakeClient` is the only supported entry point. All other modules in
`mesa_ducklake` are internal.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check src/ tests/
```

### Postgres-backed tests

About 40 tests cover the Postgres catalog backend and are marked
`requires_postgres`. They **skip themselves** when no server is
reachable, so `pytest -q` can report all-green while exercising none of
that code — check the skip count, not just the exit status.

Two ways to run them:

```bash
# 1. Let pytest-postgresql start an ephemeral cluster. Needs a Postgres
#    installation on PATH (`pg_ctl`, or `pg_config` pointing at one).
pytest -q -m requires_postgres

# 2. Point the suite at a server you already have — a container, a local
#    instance, whatever. No pg_ctl needed.
docker run --rm -d -p 5432:5432 \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=mesa_test postgres:16
MESA_DUCKLAKE_TEST_PG_HOST=localhost pytest -q -m requires_postgres
```

The second form is what CI uses, with a GitHub Actions service
container. Overrides: `MESA_DUCKLAKE_TEST_PG_{PORT,USER,PASSWORD,DBNAME}`
(defaults `5432` / `postgres` / `postgres` / `mesa_test`). Each test
still gets its own database on the server, so isolation matches the
ephemeral-cluster path.

## Status

Pre-alpha but functional. The `DuckLakeClient` API is implemented:
project register / find, `record_changes`, time-travel reads
(`get_avus`, `get_avus_as_of`, `get_history`), snapshot diff,
recovery (`recover_pending_pushes`). The iRODS sync sidecar replicates
each Parquet snapshot into the project's `/.mesa/ducklake/` collection
with checksum verification and a Postgres-side WAL for crash recovery.
The local Parquet cache lives under `platformdirs.user_cache_dir
("mesa-ducklake")` by default, bounded by an LRU-by-mtime eviction
policy. See [`docs/dev/architecture.md`](./docs/dev/architecture.md)
for the write/read protocols.

Still ahead: iRODS rule callbacks deployed on a production server (so
non-mesa-mcp writes like `imeta` reach the history), snapshot
compaction, and a separate WAL-shipping path for sub-minute RPO on
the catalog backup.

## Project guide

See [`CLAUDE.md`](./CLAUDE.md) for full architecture, schema, and hard
contracts (append-only, canonical AVU shape, per-project storage,
`DuckLakeClient` as sole public surface).
