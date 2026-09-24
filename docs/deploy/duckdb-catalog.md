# DuckDB-file catalog

What this page covers: running mesa-ducklake's catalog on a single
local DuckDB file instead of Postgres. It explains when that is the
right choice, how to point at the file, how the schema appears, and
how to back the file up. It also covers how mesa-mcp's local-install
mode uses this backend.

The catalog (`mesa.projects`, `mesa.snapshots`, `mesa.pending_pushes`)
is the only part that changes. The data plane is the same on both
backends. The data plane consists of the per-project Parquet files in
iRODS under `<project>/.mesa/ducklake/`, the local cache, and the
push-before-commit sync.

The design record is
[`../design/2026-06-07-duckdb-catalog-backend-design.md`](../design/2026-06-07-duckdb-catalog-backend-design.md).

## When to use it

| Use a DuckDB file when… | Use Postgres when… |
|---|---|
| One user, one process: a laptop, a VICE app, or a local mesa-mcp install. | A hosted mesa-mcp serves many users. |
| You want AVU history without running a database server. | Several processes or hosts write to the same catalog. |
| The catalog only needs to be read by that same process. | iRODS rule callbacks (`mesa-ducklake record`) run on the iRODS server. |
| | You want the daily `pg_dump`-to-iRODS backup pipeline. |

### Single-writer limitation

DuckDB allows **one OS process at a time** to open a database file
for writing. `DuckDBCatalogStore` keeps its connection open for the
life of the client, so while one process holds the catalog, a second
process that opens the same file fails with a lock error. This
applies to a second mesa-mcp, a `mesa-ducklake` CLI invocation, or an
ad-hoc `duckdb` shell.

In practice:

- Run exactly one long-lived process against a given file.
- Stop that process before running `mesa-ducklake migrate`,
  `mesa-ducklake recover`, or a backup copy against the same file.
- Do not point iRODS rule callbacks at a DuckDB catalog. Each AVU
  event spawns its own `mesa-ducklake record` process, and concurrent
  events would collide on the lock.
- Do not put the file on a network filesystem shared between hosts.

## DSN forms

The backend is selected from the same `catalog_dsn` (Python) or
`MESA_DUCKLAKE_DSN` (CLI) value that selects Postgres, by
`mesa_ducklake.catalog.open_catalog`:

| DSN | Resolves to |
|---|---|
| `duckdb:///home/alice/.local/share/mesa/catalog.duckdb` | Absolute path `/home/alice/.local/share/mesa/catalog.duckdb`. **Recommended.** |
| `duckdb://catalog.duckdb` | `catalog.duckdb` relative to the process's working directory |
| `/home/alice/mesa/catalog.duckdb` or `catalog.duckdb` | A bare path works if it ends in `.duckdb` |
| `duckdb://:memory:` or `:memory:` | In-memory database, discarded on close. For tests only. |

A leading `~` is expanded. Parent directories are created on open.
A DSN that matches neither the Postgres forms nor the DuckDB forms
raises `ValueError`.

```python
from mesa_ducklake import DuckLakeClient

with DuckLakeClient(
    catalog_dsn="duckdb:///home/alice/.local/share/mesa/catalog.duckdb",
    irods_session=irods,     # or None for local-only mode
) as client:
    ...
```

## Schema bootstrap (no migrations)

The DuckDB backend has **no migration history** and no
`mesa.schema_versions` table. Every time the file is opened,
`DuckDBCatalogStore` runs the idempotent `CREATE … IF NOT EXISTS`
statements in `_SCHEMA_STATEMENTS`
([`../../src/mesa_ducklake/catalog_duckdb.py`](../../src/mesa_ducklake/catalog_duckdb.py)).
A new file is therefore ready as soon as a client first touches it.

The CLI's `migrate` verb accepts a DuckDB DSN, so operators can use
one command for either backend. It opens the file, which runs the
bootstrap, closes it, and reports that nothing was applied:

```bash
MESA_DUCKLAKE_DSN=duckdb:///home/alice/.local/share/mesa/catalog.duckdb \
    mesa-ducklake migrate
# {"applied": 0, "target": null, "backend": "duckdb"}
```

The tables and columns match Postgres, with a few physical
differences. `project_id` is stored as `TEXT`, there is a sequence
instead of `BIGSERIAL`, and there are no foreign keys or secondary
indexes. [`../dev/schema.md`](../dev/schema.md#duckdb-catalog-differences)
has the full table. Duplicate project registration raises
`duckdb.ConstraintException`; Postgres raises
`psycopg.errors.UniqueViolation`.

When the catalog schema changes, contributors append matching
idempotent statements to `_SCHEMA_STATEMENTS`, so existing files
upgrade on their next open (see
[`../dev/adding-migrations.md`](../dev/adding-migrations.md#keeping-the-duckdb-backend-in-step)).

There is no supported path to move a DuckDB catalog into Postgres, or
the reverse. The Parquet files in iRODS are the same on both backends,
but the catalog rows would have to be copied by hand.

## Backup

The `pg_dump` pipeline in [`backup.md`](./backup.md) does not apply.
Back up the file itself:

1. Stop the process that holds the catalog. A clean close checkpoints
   DuckDB's write-ahead log into the main file.
2. Copy `catalog.duckdb`, plus `catalog.duckdb.wal` if one is present,
   to safe storage. iRODS works well for this: `iput -f`.
3. Restart the process.

Never copy the file while a writer has it open. See
[`backup.md`](./backup.md#backing-up-a-duckdb-file-catalog) for the
restore steps.

The Parquet history itself lives in iRODS when the client has a
session, so losing the catalog file loses the *index*, not the AVU
rows. In local-only mode (no iRODS session), the local cache holds the
only copy. Back up the cache directory too, and set
`cache_cap_bytes=0` so eviction never deletes history.

## How mesa-mcp's local install uses it

[mesa-mcp](https://github.com/idss-mesa/mesa-mcp) reads the catalog DSN
from `ducklake.catalog_dsn` in its `config.yaml`, or from the
`MESA_MCP_DUCKLAKE__CATALOG_DSN` environment variable. For a
single-user local install:

```yaml
ducklake:
  catalog_dsn: duckdb:///home/alice/.local/share/mesa/catalog.duckdb
  data_collection: .mesa/ducklake
```

- A blank `catalog_dsn`, the default, disables AVU-history mirroring.
  AVU writes still succeed but are not recorded.
- A `duckdb:///…` DSN gives the local mesa-mcp process a private
  catalog. mesa-mcp passes the caller's iRODS session on each call
  (`session=`), so each
  snapshot's Parquet is still pushed to the project's
  `.mesa/ducklake/` collection in iRODS.
- Only that one mesa-mcp process may hold the file. Stop it before
  running `mesa-ducklake` CLI verbs against the same DSN.

Install mesa-mcp with its `ducklake` extra so that mesa-ducklake is
importable: `pip install -e "../mesa-mcp[ducklake]"`.

## See also

- [`postgres.md`](./postgres.md): the multi-writer backend.
- [`../user/usage.md`](../user/usage.md#choosing-a-catalog-backend):
  choosing a backend from Python.
- [`../user/cli.md`](../user/cli.md): `MESA_DUCKLAKE_DSN` for the CLI.
- [`../dev/architecture.md`](../dev/architecture.md): the
  `CatalogStore` Protocol and both implementations.
