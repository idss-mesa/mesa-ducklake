# Architecture

What this page covers:

- the catalog / lake split that defines mesa-ducklake;
- the `CatalogStore` protocol and its two backends (Postgres and a
  DuckDB file);
- the `LakeStorage` seam and the iRODS sync sidecar;
- the push-before-commit control flow of `record_changes`, end to end.

## The two-store split

mesa-ducklake is two stores, not one:

- The **catalog** holds the registry of MESA-enabled iRODS projects
  (`mesa.projects`), the index of all snapshots taken against them
  (`mesa.snapshots`), and the push write-ahead log
  (`mesa.pending_pushes`). It sits behind the `CatalogStore` Protocol
  in `catalog_base.py`, which has two implementations:
  - `PostgresCatalogStore` (`catalog.py`) is the default for hosted
    and multi-writer deployments. The schema is managed by numbered
    migrations.
  - `DuckDBCatalogStore` (`catalog_duckdb.py`) uses a single local
    `.duckdb` file for single-user or local installs. It is
    single-writer, and its schema is bootstrapped inline on open.

  `open_catalog(dsn)` in `catalog.py` picks the backend from the DSN.
  See [`../deploy/duckdb-catalog.md`](../deploy/duckdb-catalog.md).
- The **lake** is a directory of Parquet files, one per snapshot,
  named `snapshot_<id>.parquet`. The durable copy of each project's
  lake lives at `<irods_path>/.mesa/ducklake/` *inside iRODS*. The
  sub-collection name is configurable via `data_collection`. Keeping
  it there means the metadata history travels with the data it
  describes. DuckDB needs real filesystem paths, so the library reads
  and writes a **local cache** copy, and the `irods_sync` sidecar
  replicates between the cache and iRODS. The lake is the AVU fact
  table; the catalog is the index.

Postgres was originally chosen for the catalog for two reasons: iRODS
iCAT already runs on Postgres, so one cluster can host both schemas,
and the catalog needs transactional snapshot-id allocation across
concurrent writers. Both still hold for hosted deployments. They do
not hold for a single user on a laptop. There one process owns the
catalog, and a DuckDB file gives the same guarantees without running
a server. That is why the catalog became a Protocol rather than a
Postgres dependency.

```
                         ┌─────────────────────────────┐
                         │       DuckLakeClient        │
                         │   (only public facade)      │
                         └───┬─────────────┬───────┬───┘
                 catalog ops │             │       │ push / pull
                             ▼             │       ▼
            ┌──────────────────────────┐   │   ┌──────────────────────┐
            │ CatalogStore (Protocol)  │   │   │ irods_sync (sidecar) │
            │ catalog_base.py          │   │   │ push + checksum,     │
            │ chosen by open_catalog() │   │   │ ensure_cached (pull),│
            └──────┬─────────────┬─────┘   │   │ recover_pending_...  │
                   │             │         │   └──────────┬───────────┘
                   ▼             ▼         │ data-plane   │
   ┌─────────────────────┐ ┌──────────────────┐   ops     │
   │ PostgresCatalogStore│ │DuckDBCatalogStore│   │       │
   │ catalog.py          │ │catalog_duckdb.py │   ▼       ▼
   │ schema=mesa         │ │ one .duckdb file │ ┌───────────────┐   ┌─────────────────────────┐
   │ projects            │ │ projects         │ │  LakeStore    │   │ iRODS                   │
   │ snapshots           │ │ snapshots        │ │  (lake.py)    │   │ <project>/.mesa/        │
   │ pending_pushes      │ │ pending_pushes   │ │  DuckDB over  │   │   ducklake/             │
   │ schema_versions     │ │ (inline schema,  │ │  local cache  │◄─►│   snapshot_<id>.parquet │
   │ (migrations/)       │ │  single-writer)  │ │  <cache>/<id>/│   │                         │
   └─────────────────────┘ └──────────────────┘ └───────────────┘   └─────────────────────────┘
                                                 LocalLakeStorage     durable copy
                                                 + cache.py (LRU)
```

## Module map

Every module under `src/mesa_ducklake/`:

| Module | Public? | Role |
|---|---|---|
| `__init__.py` | yes | Re-exports `DuckLakeClient`, `AvuChange`, `Project`, `Snapshot`. Nothing else is public. |
| `client.py` | yes (facade) | `DuckLakeClient`. Composes a `CatalogStore` (via `open_catalog`), per-project `LakeStore` instances, the `irods_sync` sidecar and cache eviction. |
| `catalog_base.py` | internal | `CatalogStore` Protocol, the method surface every catalog backend implements. `DuckLakeClient` and `irods_sync` depend only on this. |
| `catalog.py` | internal | `PostgresCatalogStore` (parameterized SQL against the `mesa` schema) and `open_catalog(dsn)`, the backend factory. |
| `catalog_duckdb.py` | internal | `DuckDBCatalogStore` over one `.duckdb` file. The inline schema bootstrap is `_SCHEMA_STATEMENTS`. Single-writer. |
| `lake.py` | internal | `LakeStore`, the `LakeStorage` Protocol and `LocalLakeStorage`. Owns DuckDB connections and Parquet I/O against the local cache. |
| `irods_sync.py` | internal | The iRODS sync sidecar: `push` (with checksum verification), `pull`, `ensure_cached`, `recover_pending_pushes`. Also defines `DEFAULT_MAX_ATTEMPTS` and the `PARQUET_FILE_FAILED` sentinel. |
| `cache.py` | internal | Local cache bookkeeping: `total_bytes` and `evict_if_over` (LRU by mtime). |
| `cli.py` | CLI | The `mesa-ducklake` entry point with its `record`, `recover` and `migrate` verbs. See [`../user/cli.md`](../user/cli.md). |
| `models.py` | internal types | Pydantic `AvuChange`, `Project`, `Snapshot`, `PendingPush` and the `PARQUET_FILE_PENDING` sentinel. |
| `queries.py` | internal | The canonical effective-AVU SQL template. The single source of truth for the query shape. |
| `time_travel.py` | internal | Pure helpers; currently just `parse_as_of`. |
| `schema.py` | internal | Postgres migration runner (`apply_migrations`) and DDL discovery. The DuckDB backend does not use it. |
| `irods_path.py` | internal | Pure string helpers for iRODS path math (`ducklake_subpath`, `DEFAULT_DATA_COLLECTION`). |

## Control flow: one `record_changes` call

```
caller
  │ record_changes(project_id, actor, changes, note, session=None)
  ▼
DuckLakeClient
  │ 1. catalog.get_project(project_id)
  │ 2. parent = catalog.latest_snapshot_id(project_id)     -- latest non-pending
  │ 3. catalog.create_snapshot(parquet_file='pending')      -- hidden from reads
  │    → stable snapshot_id
  │ 4. stamp every AvuChange with project_id + snapshot_id (Pydantic copy)
  │
  ├── with an iRODS session (per-call session= wins over the constructor's):
  │ 5. catalog.insert_pending_push(snapshot_id, local, irods_target)   -- WAL row
  │ 6. lake.write_changes(...)       -- DuckDB COPY … TO '<cache>/<project_id>/snapshot_<id>.parquet'
  │ 7. irods_sync.push(local, irods_target)   -- put + checksum verify
  │ 8. catalog.update_snapshot_parquet_file(real_name)          -- COMMIT POINT
  │ 9. catalog.delete_pending_push(snapshot_id)                 -- drain WAL
  │
  ├── local-only (no session anywhere):
  │ 5'. lake.write_changes(...)
  │     on exception: catalog.delete_snapshot(snapshot_id) and re-raise
  │ 6'. catalog.update_snapshot_parquet_file(real_name)
  │
  │ 10. cache.evict_if_over(cache_root, cache_cap_bytes)        -- if cap > 0
  ▼
returns the committed Snapshot
```

The write inside `lake.write_changes` is:

```
DuckDB :memory: connection
CREATE TEMP TABLE staging_changes (...)
INSERT INTO staging_changes ?  (executemany)
COPY (SELECT ... FROM staging_changes ORDER BY ts, attribute, value, unit)
  TO 'snapshot_<id>.parquet' (FORMAT PARQUET)
```

Three invariants hold in this flow:

1. The snapshot row is allocated *before* the Parquet write. That
   way the `snapshot_id` embedded in the Parquet rows is the one that
   ends up in the catalog.
2. The catalog row stays `'pending'` until the Parquet is durable:
   pushed to iRODS in sync mode, or written locally in local-only
   mode. Reads filter `'pending'`, so a half-finished write is never
   visible.
3. In sync mode, a failure anywhere after step 5 leaves the pending
   row and the WAL row in place for `recover` to finish. The snapshot
   row is **not** deleted. Only local-only mode deletes a snapshot row
   (`CatalogStore.delete_snapshot`), and only when the Parquet write
   itself fails.

## The `LakeStorage` protocol seam

`src/mesa_ducklake/lake.py` defines a small Protocol:

```python
class LakeStorage(Protocol):
    def root(self) -> str: ...
    def absolute_path(self, relative_name: str) -> str: ...
    def existing_parquet_files(self) -> list[str]: ...
```

Only `LocalLakeStorage` (POSIX filesystem) implements it. No
iRODS-backed `LakeStorage` is planned. iRODS durability comes from
the [sync sidecar](#irods-sync-sidecar), which replicates the local
files. The seam still matters because:

- DuckDB's `read_parquet([...])` and `COPY ... TO '<path>' (FORMAT
  PARQUET)` only understand a string path. Whatever gets bytes onto
  local disk (the sidecar today) is kept out of `LakeStore`.
- The "metadata travels with the data" rule means the lake root for
  a given project is *determined by that project's iRODS path*. A
  protocol indirection keeps `LakeStore` agnostic.

`DuckLakeClient._resolve_lake_root` maps a project to its local
cache directory:

```python
def _resolve_lake_root(self, project: Project) -> Path:
    return self._cache_root / str(project.project_id)
```

`_cache_root` is `cache_dir`, or the deprecated `lake_root_override`,
or `platformdirs.user_cache_dir("mesa-ducklake")`. The catalog's
`project.ducklake_path` is the *iRODS* location. It is used only as
the push/pull target and never as a local path.

## Why Parquet and not "just Postgres"

A reasonable alternative would have been to store the AVU change
fact table as another Postgres table inside the `mesa` schema. We
deliberately did not:

- **Locality with science data.** AVU changes are metadata *about*
  the iRODS data they describe. If a researcher exports their
  project to a new tenant, off-line archive, or cold storage, the
  metadata history needs to travel with them. Postgres rows would
  stay in the central catalog.
- **Cheap columnar reads.** Time-travel and history queries are
  read-heavy and aggregate over many rows for a small number of
  paths. Parquet + DuckDB outperforms Postgres for this shape at
  scale.
- **No write contention.** Per-project Parquet files mean two
  projects writing simultaneously never touch the same storage
  object, so we avoid contention without sharding Postgres.

The trade-off is that we cannot join AVU fact rows to the catalog
inside a single SQL query — but the API does not need to. The
catalog tells us *which* Parquet file to open; DuckDB reads it.

## DuckDB / DuckLake usage today

`LakeStore` currently opens an in-memory DuckDB connection per
operation and closes it before return. The `read_parquet` glob over
all `snapshot_*.parquet` files is exposed as a view named
`avu_changes`, against which the canonical queries from
`queries.py` execute.

DuckLake's own catalog and snapshot features (the `ducklake`
extension) are not used. Snapshots are managed in the mesa catalog
(Postgres or DuckDB file), and DuckDB reads the Parquet files
directly. Adopting DuckLake's snapshot-aware reads is an open
design item; the public API is shaped so that change is invisible
to callers.

## iRODS sync sidecar

DuckDB requires real filesystem paths for both `COPY (...) TO '...'`
and `read_parquet([...])` — no bytes-stream interface lets us point
it at an iRODS data object directly. So we use a **sidecar** pattern
instead of replacing `LocalLakeStorage` with an iRODS-backed
implementation:

* `LocalLakeStorage` continues to write/read Parquet on local disk.
* The local directory is now a **cache** under
  `platformdirs.user_cache_dir("mesa-ducklake")/<project_id>/`, not
  the project's iRODS path.
* `mesa_ducklake.irods_sync` replicates each Parquet file into
  `<project.ducklake_path>/snapshot_<id>.parquet` after the local
  write succeeds, with checksum verification.
* Reads call `irods_sync.ensure_cached` before opening DuckDB, which
  pulls any expected Parquet files missing from the local cache
  from iRODS.

### Write protocol (push-before-commit)

See [the control flow above](#control-flow-one-record_changes-call).
`list_snapshots` and `latest_snapshot_id` filter `parquet_file =
'pending'` by default, so reads never see a half-committed snapshot.
Pass `include_pending=True` for recovery or debug queries. Rows that
recovery has marked `'failed'` are **not** filtered by those two
methods. They stay invisible to AVU reads only because
`ensure_cached` skips both sentinels and no Parquet file exists for
them.

### Crash recovery

Any exception between steps 2 and 6 leaves the catalog row pending
and the WAL row in place. `DuckLakeClient.recover_pending_pushes` (or
the `mesa-ducklake recover` CLI) drains the queue:

* **Catalog row already committed** — drop the WAL row.
* **Snapshot row gone** (rare; `ON DELETE CASCADE` usually handles
  it) — drop the orphan WAL row.
* **Attempts ≥ `DEFAULT_MAX_ATTEMPTS`** — mark the catalog row
  `parquet_file='failed'` so it stays invisible to reads, drop the
  WAL row. Operators inspect via
  `list_snapshots(include_pending=True)`.
* **Local file missing** — bump attempts; future retries may recover
  if the cache repopulates, otherwise the attempt cap eventually
  marks it failed.
* **Retry** — re-push to iRODS (idempotent via
  `data_objects.put(..., force=True)` + deterministic Parquet output
  from the `ORDER BY` in `lake.py`), commit the catalog row, drop
  the WAL row.

### Local cache

Default location resolves via `platformdirs.user_cache_dir
("mesa-ducklake")`:

| Platform                                | Path                                                          |
| --------------------------------------- | ------------------------------------------------------------- |
| Linux (XDG)                             | `$XDG_CACHE_HOME/mesa-ducklake` or `~/.cache/mesa-ducklake`   |
| Linux (systemd `CacheDirectory=`)       | the directory systemd creates and exports                     |
| macOS                                   | `~/Library/Caches/mesa-ducklake`                              |

Override at construction (`cache_dir=` / `cache_cap_bytes=`) or via
mesa-mcp's `Config.ducklake.{cache_dir,cache_cap_bytes}` YAML/env.

After every successful commit, `cache.evict_if_over` walks the cache
root oldest-first by `st_mtime` and unlinks files until the total is
back under `cache_cap_bytes` (default 1 GiB; `0` disables). Eviction
runs on writes only — reads populate the cache via `ensure_cached`,
and we want recently-read snapshots to stay hot.

### Local-only mode

`DuckLakeClient(irods_session=None)` *and* no per-call `session=...`
disables the iRODS push entirely. Writes follow the "delete on
failure" rollback, the WAL stays untouched, and the cache *is* the
only copy. Cache eviction can therefore delete history. Size
`cache_cap_bytes` accordingly, or use `0` for unbounded.

Local-only mode is used by:

- the `mesa-ducklake record` CLI, which never opens an iRODS session
  (see [`../user/cli.md`](../user/cli.md#what-record-does-not-do));
- tests such as `tests/test_client_e2e.py` and
  `tests/test_client_e2e_duckdb.py`, which exercise catalog + lake
  without iRODS.

mesa-mcp's local-install mode is a different case. When
`ducklake.catalog_dsn` is blank, mesa-mcp skips mesa-ducklake
entirely. When it is set to a `duckdb:///…` DSN, mesa-mcp uses the
DuckDB catalog and passes the caller's iRODS session per call, so
writes still sync to iRODS.

## See also

- [`schema.md`](./schema.md) — the column-by-column reference for
  every store described here.
- [`queries.md`](./queries.md) — the canonical query templates.
- [`adding-migrations.md`](./adding-migrations.md) — how the
  catalog evolves over time.
- [`../deploy/backup.md`](../deploy/backup.md) — daily `pg_dump` to
  iRODS for catalog durability.
- [`../deploy/duckdb-catalog.md`](../deploy/duckdb-catalog.md) — the
  DuckDB-file catalog backend.
- [`architecture-review-2026-09.md`](./architecture-review-2026-09.md) —
  point-in-time architecture review.
- [`../user/cli.md`](../user/cli.md) — the `mesa-ducklake recover`
  CLI that drains the WAL on demand.
- [`../../CLAUDE.md`](../../CLAUDE.md) — full architecture
  rationale.
