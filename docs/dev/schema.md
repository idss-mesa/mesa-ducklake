# Schema

What this page covers: every column in mesa-ducklake's persistent
state, in both Postgres (the catalog) and Parquet (the fact table).
Each column is annotated with its purpose, nullability, and the
hard contract (if any) it participates in. The authoritative DDL
lives in
[`../../migrations/0001_initial.sql`](../../migrations/0001_initial.sql);
the Pydantic models live in
[`../../src/mesa_ducklake/models.py`](../../src/mesa_ducklake/models.py).

## Postgres catalog (schema `mesa`)

The catalog is an *index*. It does not store AVU rows itself —
those live in the per-project Parquet files. The Postgres schema
exists to give us transactional snapshot id allocation, a
cross-project project registry, and a single place to ask "where do
this project's Parquet files live?".

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
| `irods_path` | `TEXT NOT NULL UNIQUE` | Absolute iRODS path of the project's root collection (e.g. `/iplant/home/alice/myproj`). The `UNIQUE` constraint guarantees one project per path; duplicate `register_project` calls raise `psycopg.errors.UniqueViolation`. |
| `irods_zone` | `TEXT NOT NULL` | iRODS zone (e.g. `iplant`). Lets tooling validate that a path belongs to the expected zone. |
| `ducklake_path` | `TEXT NOT NULL` | iRODS path of the project's `/.mesa/ducklake/` subcollection. Defaults to `<irods_path>/.mesa/ducklake` if the caller passes `None`; computed by `mesa_ducklake.irods_path.ducklake_subpath`. |
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
| `snapshot_id` | `BIGSERIAL PK` | Monotonic, server-assigned. Embedded in every Parquet row that belongs to this snapshot. Stable: never recycled, never updated. |
| `project_id` | `UUID NOT NULL FK` | Owning project. Cascades are *not* declared — projects are not deleted by the library. |
| `ts` | `TIMESTAMPTZ NOT NULL` | When the snapshot was created. Default `now()`. Used for time-travel queries through the catalog (e.g. "which snapshots before T?"). |
| `actor` | `TEXT NOT NULL` | iRODS user who triggered the batch. Distinct from per-row `actor` in Parquet — they normally match, but a batch could in principle aggregate changes attributed to different per-row actors (this is not used today). |
| `parent_snapshot` | `BIGINT FK` (nullable) | Previous snapshot id for this project, or `NULL` for the first one. Lets clients walk a project's snapshot chain without re-querying by `(project_id, ts)`. |
| `note` | `TEXT` (nullable) | Optional human-readable commit message ("Tagged file.csv with ENVO biome"). |
| `parquet_file` | `TEXT NOT NULL` | Path of the Parquet file relative to `ducklake_path` (currently always `snapshot_<id>.parquet`). Initially inserted as `'pending'` by `record_changes`, then updated to the real name after the write succeeds — see `CatalogStore.update_snapshot_parquet_file`. |
| index `snapshots_project_ts_idx` | `(project_id, ts)` | Supports `list_snapshots` and history walks scoped to one project. |

### `mesa.schema_versions` — bootstrap table

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

1. Write a new migration explaining why (`0002_<description>.sql`),
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
- [`../../migrations/0001_initial.sql`](../../migrations/0001_initial.sql) —
  the frozen DDL.
- [`../../src/mesa_ducklake/models.py`](../../src/mesa_ducklake/models.py) —
  the Pydantic mirrors of every column.
