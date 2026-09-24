# Schema

What this page covers: every column in mesa-ducklake's persistent
state, in both Postgres (the catalog) and Parquet (the fact table).
Each column is annotated with its purpose, nullability, and the
hard contract (if any) it participates in. The authoritative
Postgres DDL lives in [`../../migrations/`](../../migrations/)
(`0001_initial.sql`, `0002_pending_pushes.sql`). The DuckDB-file
backend's equivalent is `_SCHEMA_STATEMENTS` in
[`../../src/mesa_ducklake/catalog_duckdb.py`](../../src/mesa_ducklake/catalog_duckdb.py)
(see [DuckDB catalog differences](#duckdb-catalog-differences));
the Pydantic models live in
[`../../src/mesa_ducklake/models.py`](../../src/mesa_ducklake/models.py).

## Postgres catalog (schema `mesa`)

The catalog is an *index*. It does not store AVU rows itself;
those live in the per-project Parquet files. The catalog schema
exists to provide:

- transactional snapshot-id allocation;
- a registry of projects across the whole deployment;
- a single place to ask "where do this project's Parquet files live?";
- a write-ahead log for in-flight iRODS pushes.

### `mesa.projects`

One row per MESA-enabled iRODS project.

```sql
CREATE TABLE IF NOT EXISTS mesa.projects (
    project_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    irods_path       TEXT NOT NULL UNIQUE,
    irods_zone       TEXT NOT NULL,
    ducklake_path    TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active'
);
```

| Column | Type | Notes |
|---|---|---|
| `project_id` | `UUID PK` | Server-generated. Never derived from the path — collections can be renamed. Foreign-keyed from `mesa.snapshots` and stamped on every Parquet row. |
| `irods_path` | `TEXT NOT NULL UNIQUE` | Absolute iRODS path of the project's root collection (e.g. `/iplant/home/alice/myproj`). The `UNIQUE` constraint guarantees one project per path. A duplicate `register_project` raises `psycopg.errors.UniqueViolation` on Postgres and `duckdb.ConstraintException` on DuckDB. |
| `irods_zone` | `TEXT NOT NULL` | iRODS zone (e.g. `iplant`). Lets tooling validate that a path belongs to the expected zone. |
| `ducklake_path` | `TEXT NOT NULL` | iRODS path of the project's Parquet subcollection. It is `<irods_path>/.mesa/ducklake` by default, or `<irods_path>/<data_collection>` when `DuckLakeClient(data_collection=...)` is set. It is computed by `mesa_ducklake.irods_path.ducklake_subpath` and fixed at registration time. |
| `created_at` | `TIMESTAMPTZ NOT NULL` | When the project was registered. Default `now()`. |
| `created_by` | `TEXT NOT NULL` | iRODS username that registered the project. |
| `status` | `TEXT NOT NULL` | `'active'` or `'archived'`. Today only `'active'` is set by the library; `'archived'` is reserved for an operator workflow. |

### `mesa.snapshots`

One row per atomic batch of AVU changes — one user action becomes
one snapshot row plus one Parquet file.

```sql
CREATE TABLE IF NOT EXISTS mesa.snapshots (
    snapshot_id      BIGSERIAL PRIMARY KEY,
    project_id       UUID NOT NULL REFERENCES mesa.projects(project_id),
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor            TEXT NOT NULL,
    parent_snapshot  BIGINT REFERENCES mesa.snapshots(snapshot_id),
    note             TEXT,
    parquet_file     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS snapshots_project_ts_idx
    ON mesa.snapshots (project_id, ts);
```

| Column | Type | Notes |
|---|---|---|
| `snapshot_id` | `BIGSERIAL PK` | Monotonic and server-assigned. The sequence is global across all projects, not per project, so a project's ids have gaps. Embedded in every Parquet row that belongs to this snapshot. Stable: never recycled, never updated. |
| `project_id` | `UUID NOT NULL FK` | Owning project. Cascades are *not* declared — projects are not deleted by the library. |
| `ts` | `TIMESTAMPTZ NOT NULL` | When the snapshot was created. Default `now()`. Used for time-travel queries through the catalog (e.g. "which snapshots before T?"). |
| `actor` | `TEXT NOT NULL` | iRODS user who triggered the batch. Distinct from per-row `actor` in Parquet — they normally match, but a batch could in principle aggregate changes attributed to different per-row actors (this is not used today). |
| `parent_snapshot` | `BIGINT FK` (nullable) | Previous snapshot id for this project, or `NULL` for the first one. Lets clients walk a project's snapshot chain without re-querying by `(project_id, ts)`. |
| `note` | `TEXT` (nullable) | Optional human-readable commit message ("Tagged file.csv with ENVO biome"). |
| `parquet_file` | `TEXT NOT NULL` | Path of the Parquet file relative to `ducklake_path` (always `snapshot_<id>.parquet` once committed), or one of the [sentinels](#parquet_file-sentinels) below. Inserted as `'pending'` by `record_changes` and flipped to the real name at the commit point via `CatalogStore.update_snapshot_parquet_file`. |
| index `snapshots_project_ts_idx` | `(project_id, ts)` | Supports `list_snapshots` and history walks scoped to one project. |

#### `parquet_file` sentinels

| Value | Defined in | Meaning | Visibility |
|---|---|---|---|
| `'pending'` | `models.PARQUET_FILE_PENDING` | The snapshot row exists, but its Parquet has not yet been written, or in sync mode not yet pushed to iRODS. Normally paired with a `mesa.pending_pushes` row. | Filtered out by `list_snapshots` and `latest_snapshot_id` unless `include_pending=True`. Never pulled by `ensure_cached`. |
| `'failed'` | `irods_sync.PARQUET_FILE_FAILED` | `recover_pending_pushes` gave up after `DEFAULT_MAX_ATTEMPTS` (5) attempts. The WAL row is dropped. | **Not** filtered by `list_snapshots` or `latest_snapshot_id`. It is invisible to AVU reads only because `ensure_cached` skips it and no Parquet file exists. |

Sentinel rows are the only rows in `mesa.snapshots` whose
`parquet_file` is ever updated. A committed filename is never changed.

### `mesa.pending_pushes`

Added by
[`0002_pending_pushes.sql`](../../migrations/0002_pending_pushes.sql).
This is the write-ahead log (WAL) for the push-before-commit write
path, with one row per in-flight Parquet upload from the local cache
to iRODS:

1. `record_changes` inserts the row before writing the Parquet.
2. It deletes the row after the snapshot's `parquet_file` is committed.
3. A row that survives a crash is drained by `mesa-ducklake recover` /
   `DuckLakeClient.recover_pending_pushes`.

It is kept in its own table, not as columns on `mesa.snapshots`,
because a retry queue has a mutable lifecycle (`attempts`,
`last_error`) that does not belong on an immutable snapshot record.

```sql
CREATE TABLE IF NOT EXISTS mesa.pending_pushes (
    snapshot_id    BIGINT PRIMARY KEY
                          REFERENCES mesa.snapshots(snapshot_id)
                          ON DELETE CASCADE,
    local_path     TEXT NOT NULL,
    irods_target   TEXT NOT NULL,
    attempts       INT NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pending_pushes_created_at_idx
    ON mesa.pending_pushes (created_at);
```

| Column | Type | Notes |
|---|---|---|
| `snapshot_id` | `BIGINT PK FK` | The snapshot being pushed. `ON DELETE CASCADE` removes the WAL row if the snapshot row is deleted. |
| `local_path` | `TEXT NOT NULL` | Absolute path of the Parquet in the local cache (`<cache_dir>/<project_id>/snapshot_<id>.parquet`). |
| `irods_target` | `TEXT NOT NULL` | Absolute iRODS path the Parquet must land at (`<ducklake_path>/snapshot_<id>.parquet`). |
| `attempts` | `INT NOT NULL` | Failed push attempts so far. At `DEFAULT_MAX_ATTEMPTS` the snapshot is marked `'failed'`. |
| `last_error` | `TEXT` (nullable) | Truncated message from the most recent failure (default cap 2000 chars). |
| `created_at` | `TIMESTAMPTZ NOT NULL` | Insertion time. Recovery drains oldest first (index `pending_pushes_created_at_idx`). |

Pydantic mirror: `models.PendingPush`.

### `mesa.schema_versions` — bootstrap table (Postgres only)

This table is **not** declared in `0001_initial.sql`. It is created
by `mesa_ducklake.schema._ensure_bookkeeping_table` before any
migration runs, because the runner needs to know which migrations
have already been applied *before* it can read its own version log.
The migration files therefore never reference it.

```sql
CREATE TABLE IF NOT EXISTS mesa.schema_versions (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    filename   TEXT NOT NULL
);
```

| Column | Notes |
|---|---|
| `version` | The numeric prefix of the migration file (e.g. `1` for `0001_initial.sql`). |
| `applied_at` | When the runner inserted this row. |
| `filename` | Migration filename as it was discovered on disk. |

See [`adding-migrations.md`](./adding-migrations.md) for the rules
that govern this table.

## DuckDB catalog differences

`DuckDBCatalogStore` exposes the same three tables with the same
column names, so both backends produce identical Pydantic models.
The physical schema differs:

| Aspect | Postgres (`migrations/`) | DuckDB file (`catalog_duckdb._SCHEMA_STATEMENTS`) |
|---|---|---|
| `project_id` type | `UUID DEFAULT gen_random_uuid()` | `TEXT`, a UUID string generated in Python with `uuid4()`. Pydantic coerces it back to `uuid.UUID`. |
| `snapshot_id` | `BIGSERIAL` | `BIGINT DEFAULT nextval('mesa.snapshots_seq')` |
| Foreign keys | `snapshots.project_id`, `snapshots.parent_snapshot`, `pending_pushes.snapshot_id` (`ON DELETE CASCADE`) | None. Integrity is enforced by the `DuckLakeClient` write protocol. |
| Indexes | `snapshots_project_ts_idx`, `pending_pushes_created_at_idx` | None beyond primary keys and `UNIQUE(irods_path)` |
| Schema management | Numbered migrations + `mesa.schema_versions`, applied by `mesa-ducklake migrate` | Idempotent `CREATE … IF NOT EXISTS` on every open. No `schema_versions` table and no migration history. |
| Duplicate `irods_path` | `psycopg.errors.UniqueViolation` | `duckdb.ConstraintException` |
| Concurrency | Many concurrent writers | Single writer. One process holds the file lock. |

Any catalog schema change must be made in **both** places. See
[`adding-migrations.md`](./adding-migrations.md).

## Parquet fact table (`avu_changes`)

The fact table is a conceptual schema, not a DDL. Each Parquet file
is one snapshot's batch of AVU events; the union of every file
under a project's lake root is read as a DuckDB view named
`avu_changes`.

Column order is fixed by `_PARQUET_COLUMNS` in
`src/mesa_ducklake/lake.py`. **Never reorder these columns** —
Parquet files written today must remain readable by future code.
The DuckDB staging DDL inside `lake.py` is the operational source
of truth:

```sql
CREATE TEMP TABLE staging_changes (
    project_id      TEXT,
    snapshot_id     BIGINT,
    irods_path      TEXT,
    target_type     TEXT,
    attribute       TEXT,
    value           TEXT,
    unit            TEXT,
    op              TEXT,
    actor           TEXT,
    ts              TIMESTAMPTZ,
    source          TEXT,
    via_ticket      TEXT,
    rule_invocation TEXT
)
```

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `project_id` | `TEXT` (UUID string form) | no | Owning project. Stored as string because Parquet has no first-class UUID type and string compares cross the DuckDB/Python parameter boundary without a cast. Postgres still uses real UUIDs where it matters for joins. |
| `snapshot_id` | `BIGINT` | no | Snapshot id this row belongs to. Foreign-key in spirit to `mesa.snapshots.snapshot_id`; not enforced because Parquet has no FKs. |
| `irods_path` | `TEXT` | no | Full iRODS path the AVU is bound to. Can be the project root or any descendant. |
| `target_type` | `TEXT` | no | One of `'data_object'`, `'collection'`, `'resource'`, `'user'`. Mirrors iRODS iCAT's notion of what an AVU can be attached to. |
| `attribute` | `TEXT` | no | The `A` of `AVU`. Part of the canonical triple; never split, merged, or renamed. |
| `value` | `TEXT` | no | The `V` of `AVU`. |
| `unit` | `TEXT` | no | The `U` of `AVU`. Stored as `''` (empty string) when no unit is supplied, matching iCAT. Often the ontology CURIE for OBO/OLS-sourced AVUs (e.g. `ENVO:00000428`). |
| `op` | `TEXT` | no | `'add'` or `'delete'`. Corrections are new snapshots — never rewrite a Parquet file. |
| `actor` | `TEXT` | no | iRODS user who authored the change. Always recorded; empty provenance is a bug, not a row. |
| `ts` | `TIMESTAMPTZ` | no | When the change happened (as observed by the writer). TZ-aware UTC. |
| `source` | `TEXT` | no | Origin of the change. Conventions: `'mesa-mcp'`, `'esiil-portal'`, `'irods-rule:<event>'` (where `<event>` is the iRODS event name, e.g. `acPostProcForModifyAVUMetadata`), other client names as adopted. |
| `via_ticket` | `TEXT` | yes | iRODS ticket id when the session used a ticket; `NULL` otherwise. mesa-mcp's `ds_use_ticket` tool sets a session attribute that the write path reads; the rule callback reads `$ticketUserName`. |
| `rule_invocation` | `TEXT` | yes | Name of the iRODS rule that emitted this change when `source` begins with `'irods-rule:'`. `NULL` for changes that did not originate in a rule. |

### Why the AVU triple is canonical

`(attribute, value, unit)` is the same shape that iRODS iCAT stores
and that `python-irodsclient` returns. Keeping it canonical means
mesa-ducklake can round-trip an AVU from iCAT into Parquet and back
without lossy translation. iRODS even permits multiple values per
attribute on the same target — partitioning on the full triple is
what makes that work end-to-end (see
[`queries.md`](./queries.md)).

### Why `via_ticket` and `rule_invocation` exist

A change made through an iRODS ticket has a different actor model
than a direct change: the *ticket* (not the user) authorized the
write. `via_ticket` lets auditors join an AVU change to the ticket
record. `rule_invocation` distinguishes changes captured by the
generic `irods-rule:<event>` source ("the rule fired") from the
specific rule that fired ("which rule"). Both are nullable because
most changes happen outside ticket or rule paths.

### Adding a column to `avu_changes`

The decision criterion is in
[`../../CLAUDE.md`](../../CLAUDE.md): if the existing AVU triple
shape *can* carry the information, use the AVU itself. New columns
exist only when the information is provenance about the change
rather than data inside the change.

If you do need a new column:

1. Write a new migration explaining why (`0003_<description>.sql`, or
   whatever the next free number is),
   even though the column lives in Parquet — the catalog needs to
   know about it for cross-project read paths.
2. Add the column at the **end** of `_PARQUET_COLUMNS` and the
   staging DDL in `lake.py`. Never insert in the middle.
3. Make it nullable. Old Parquet files must remain readable.
4. Add the field to the Pydantic `AvuChange` in `models.py`.
5. Update queries in `queries.py` only if the new column changes
   the time-travel semantics (it usually does not).

See [`adding-migrations.md`](./adding-migrations.md) for the
migration mechanics.

## See also

- [`architecture.md`](./architecture.md) — how the two stores are
  composed at runtime.
- [`adding-migrations.md`](./adding-migrations.md) — the rules for
  evolving the schema.
- [`queries.md`](./queries.md) — how the columns above feed the
  effective-AVU query.
- [`../../migrations/`](../../migrations/) — the frozen Postgres DDL.
- [`../deploy/duckdb-catalog.md`](../deploy/duckdb-catalog.md) — the
  DuckDB-file backend in operation.
- [`../../src/mesa_ducklake/models.py`](../../src/mesa_ducklake/models.py) —
  the Pydantic mirrors of every column.
