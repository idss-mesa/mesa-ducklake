# Architecture

What this page covers: the catalog / lake split that defines
mesa-ducklake, the `LakeStorage` protocol seam that lets the lake
back-end change without disturbing the rest of the code, and the
control flow of `record_changes` end-to-end.

## The two-store split

mesa-ducklake is two stores, not one:

- The **catalog** is a Postgres database. It holds the registry of
  MESA-enabled iRODS projects (`mesa.projects`) and the index of all
  snapshots taken against those projects (`mesa.snapshots`). Postgres
  was chosen because iRODS iCAT already uses Postgres — one cluster
  can host both schemas — and because we need transactional snapshot
  id allocation that DuckDB cannot provide alone.
- The **lake** is a directory of Parquet files, one per snapshot,
  named `snapshot_<id>.parquet`. In production each project's lake
  lives at `<irods_path>/.mesa/ducklake/` *inside iRODS itself*, so
  the metadata history travels with the data it describes. The lake
  is the AVU fact table; the catalog is the index.

```
                    ┌─────────────────────────────┐
                    │      DuckLakeClient         │
                    │  (only public facade)       │
                    └────────┬───────────┬────────┘
                             │           │
                  catalog ops│           │data-plane ops
                             ▼           ▼
                  ┌────────────────┐  ┌────────────────┐
                  │  CatalogStore  │  │   LakeStore    │
                  │  (catalog.py)  │  │   (lake.py)    │
                  └───────┬────────┘  └────────┬───────┘
                          │                    │
                          ▼                    ▼
                  ┌────────────────┐  ┌────────────────────────────────┐
                  │   Postgres     │  │  LakeStorage (Protocol)        │
                  │   schema=mesa  │  │  ───────────────────────────── │
                  │                │  │  LocalLakeStorage (shipped)    │
                  │  projects      │  │     -> /tmp/.../snapshot_*.pq  │
                  │  snapshots     │  │  IrodsLakeStorage (planned)    │
                  │  schema_versions│ │     -> /.mesa/ducklake/...     │
                  └────────────────┘  └────────────────────────────────┘
```

## Module map

The code under `src/mesa_ducklake/` mirrors the diagram one-to-one.

| Module | Public? | Role |
|---|---|---|
| `__init__.py` | yes | Re-exports `DuckLakeClient`, `AvuChange`, `Project`, `Snapshot`. Nothing else is public. |
| `client.py` | yes (facade) | `DuckLakeClient` — composes a `CatalogStore` with per-project `LakeStore` instances. Holds no other state. |
| `catalog.py` | internal | `CatalogStore` — parameterized SQL against `mesa.projects` and `mesa.snapshots`. Returns Pydantic models. |
| `lake.py` | internal | `LakeStore` + `LakeStorage` Protocol + `LocalLakeStorage`. Owns DuckDB connections and Parquet I/O. |
| `models.py` | internal types | Pydantic `AvuChange`, `Project`, `Snapshot`. Field shapes match `0001_initial.sql` plus the conceptual Parquet schema. |
| `queries.py` | internal | The canonical effective-AVU SQL template. Single source of truth for the query shape. |
| `time_travel.py` | internal | Pure helpers — currently just `parse_as_of`. |
| `schema.py` | internal | Migration runner and DDL discovery. |
| `irods_path.py` | internal | Pure string helpers for iRODS path math (`<root>/.mesa/ducklake`). |

## Control flow: one `record_changes` call

```
caller
  │
  │ 1. record_changes(project_id, actor, changes, note)
  ▼
DuckLakeClient
  │
  │ 2. catalog.get_project(project_id)        ── Postgres SELECT
  │ 3. catalog.latest_snapshot_id(project_id) ── Postgres SELECT
  │ 4. catalog.create_snapshot(...)           ── Postgres INSERT (parquet_file='pending')
  │    → returns Snapshot with stable snapshot_id
  │
  │ 5. stamp every AvuChange with project_id + snapshot_id (Pydantic copy)
  │
  │ 6. lake.write_changes(project_id, snapshot_id, stamped)
  │       │
  │       │   DuckDB :memory: connection
  │       │   CREATE TEMP TABLE staging_changes (...)
  │       │   INSERT INTO staging_changes ?  (executemany)
  │       │   COPY (SELECT ... FROM staging_changes
  │       │         ORDER BY ts, attribute, value, unit)
  │       │   TO 'snapshot_<id>.parquet' (FORMAT PARQUET)
  │       ▼
  │   returns relative filename "snapshot_<id>.parquet"
  │
  │ 7. on exception: catalog.delete_snapshot(snapshot_id) and re-raise
  │ 8. catalog.update_snapshot_parquet_file(snapshot_id, relative)
  │    → updates mesa.snapshots.parquet_file from 'pending' to real name
  │
  ▼
returns Snapshot
```

Two subtle invariants live in this flow:

1. The snapshot row is allocated *before* the Parquet write so the
   `snapshot_id` embedded in the Parquet rows is the same one that
   ends up in `mesa.snapshots`. The placeholder `parquet_file =
   'pending'` is overwritten in step 8.
2. If the Parquet write fails, the placeholder row is `DELETE`d in
   step 7 so the catalog index does not point at a file that never
   came into existence. This is the *only* path that deletes a row
   from `mesa.snapshots` in normal operation. See
   `CatalogStore.delete_snapshot` for the rationale.

## The `LakeStorage` protocol seam

`src/mesa_ducklake/lake.py` defines a small Protocol:

```python
class LakeStorage(Protocol):
    def root(self) -> str: ...
    def absolute_path(self, relative_name: str) -> str: ...
    def existing_parquet_files(self) -> list[str]: ...
```

Today only `LocalLakeStorage` (POSIX filesystem) implements it. The
production iRODS-backed `LakeStorage` will plug in here without
touching `LakeStore`, `DuckLakeClient`, `CatalogStore`, or any
caller code. The seam exists because:

- DuckDB's `read_parquet([...])` and `COPY ... TO '<path>' (FORMAT
  PARQUET)` only understand a string path. If/when iRODS Parquet
  files have to be staged through a local cache (via FUSE, or by
  download-on-read), the staging is the `LakeStorage`
  implementation's problem, not `LakeStore`'s.
- The "metadata travels with the data" rule means the lake root for
  a given project is *determined by that project's iRODS path*. A
  protocol indirection keeps `LakeStore` agnostic.

`DuckLakeClient._resolve_lake_root` is the current placeholder for
the resolution policy:

```python
def _resolve_lake_root(self, project: Project) -> Path:
    if self._lake_root_override is not None:
        return self._lake_root_override / str(project.project_id)
    return Path(project.ducklake_path)
```

In tests, `lake_root_override` redirects every project to a local
`tmp_path`. In production today, the literal `ducklake_path` is
used and is expected to be a locally-mounted filesystem path. The
real iRODS-backed implementation will replace this branch.

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

DuckLake's full multi-snapshot catalog features are not used yet —
we manage snapshots in Postgres and let DuckDB read the Parquet
files directly. Adopting DuckLake's snapshot-aware reads is an open
design item; the public API is shaped so that change is invisible
to callers.

## See also

- [`schema.md`](./schema.md) — the column-by-column reference for
  every store described here.
- [`queries.md`](./queries.md) — the canonical query templates.
- [`adding-migrations.md`](./adding-migrations.md) — how the
  catalog evolves over time.
- [`../../CLAUDE.md`](../../CLAUDE.md) — full architecture
  rationale.
