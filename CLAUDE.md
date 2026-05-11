# mesa-ducklake — Project Guide for Claude Code

## What this project is

**mesa-ducklake** is a Python library that tracks **AVU
(Attribute/Value/Unit) metadata history** for iRODS collections and data
objects in a MESA-enabled project. It uses **DuckDB's DuckLake** lakehouse
pattern: a **Postgres** catalog (matching iRODS iCAT's engine) plus
**Parquet** data files stored at `/.mesa/ducklake/` *inside the iRODS
project itself*, so the metadata history travels with the data.

Its primary consumer is the sibling project **`cyverse/mesa-mcp`** — an
MCP server that exposes CyVerse Data Store operations plus OBO/OLS-driven
metadata creation. Every AVU change made through mesa-mcp is mirrored
into mesa-ducklake.

## Goals

1. **Mirror iCAT AVU semantics.** A row in mesa-ducklake represents an
   `(attribute, value, unit)` triple bound to an iRODS path at a point in
   time — *the same shape* iRODS iCAT stores and `python-irodsclient`
   returns.
2. **Append-only, time-travel by default.** Every add/delete is a new row
   tied to a snapshot; the effective AVU set for a path at time T is
   reconstructed by reading rows where `ts <= T` and the row hasn't been
   superseded by a later delete.
3. **Per-project isolation.** Each MESA-enabled iRODS collection has its
   own DuckLake (its own Parquet files under `/.mesa/ducklake/` inside the
   collection). The Postgres catalog holds the *index* of all projects;
   the bulk data lives next to the science data it describes.
4. **Narrow Python API.** Expose a single `DuckLakeClient` facade so
   mesa-mcp (and future clients) have a small, stable surface. SQL and
   schema details live behind it.

## Architecture overview

```
                      ┌──────────────────────────┐
                      │       mesa-mcp           │
                      │  (MCP server, Python)    │
                      └──────────┬───────────────┘
                                 │  imports
                                 ▼
                      ┌──────────────────────────┐
                      │     mesa-ducklake        │
                      │   DuckLakeClient facade  │
                      └──────┬────────────┬──────┘
                             │            │
                  catalog    │            │  data files
                             ▼            ▼
              ┌─────────────────┐   ┌─────────────────────────────┐
              │   Postgres      │   │  iRODS                      │
              │   (mesa schema) │   │  /iplant/home/<u>/<proj>/   │
              │                 │   │      .mesa/ducklake/        │
              │  - projects     │   │        snapshot=*.parquet   │
              │  - snapshots    │   │        manifest.json        │
              │  - avu_changes  │   └─────────────────────────────┘
              └─────────────────┘
```

**Why this split:** Postgres gives us transactional snapshot allocation
and cross-project indexing; DuckLake/Parquet gives us cheap columnar
storage co-located with each project's data; DuckDB gives us a fast
in-process query engine that reads both transparently.

## Resolved decisions (from the mesa-mcp design conversation)

- **Catalog engine:** Postgres (same as iCAT — one cluster can host
  both schemas).
- **Data file format:** Parquet, written via DuckLake.
- **Data file location:** `/.mesa/ducklake/` inside each MESA-enabled
  iRODS collection (the "metadata travels with the data" rule).
- **API surface:** Python package, imported in-process by mesa-mcp. A
  single `DuckLakeClient` class is the only public entry point.
- **MESA marker:** A collection is MESA-enabled when its root has the
  AVU `mesa.enabled=true` *and* a `/.mesa/ducklake/` child collection.
  `mesa_ducklake_init_project` (in mesa-mcp) creates both.

## Initial schema

The Postgres `mesa` schema holds the **catalog** — pointers, snapshots,
project registry. The Parquet files hold the **fact table** of AVU
changes (one row per add/delete event).

```sql
-- catalog (Postgres, schema = mesa)

CREATE TABLE mesa.projects (
    project_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    irods_path       TEXT NOT NULL UNIQUE,         -- e.g. /iplant/home/alice/myproj
    irods_zone       TEXT NOT NULL,
    ducklake_path    TEXT NOT NULL,                -- e.g. <irods_path>/.mesa/ducklake
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT NOT NULL,                -- iRODS username
    status           TEXT NOT NULL DEFAULT 'active' -- active | archived
);

CREATE TABLE mesa.snapshots (
    snapshot_id      BIGSERIAL PRIMARY KEY,
    project_id       UUID NOT NULL REFERENCES mesa.projects(project_id),
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor            TEXT NOT NULL,                -- iRODS user who made the change
    parent_snapshot  BIGINT REFERENCES mesa.snapshots(snapshot_id),
    note             TEXT,                          -- optional commit message
    parquet_file     TEXT NOT NULL                  -- path within ducklake_path
);

CREATE INDEX ON mesa.snapshots (project_id, ts);

-- fact table (Parquet via DuckLake, conceptual schema)

avu_changes:
    project_id       UUID
    snapshot_id      BIGINT
    irods_path       TEXT       -- full iRODS path the AVU is bound to
    target_type      TEXT       -- 'data_object' | 'collection' | 'resource' | 'user'
    attribute        TEXT
    value            TEXT
    unit             TEXT       -- often the ontology CURIE for OBO/OLS-sourced AVUs
    op               TEXT       -- 'add' | 'delete'
    actor            TEXT       -- iRODS user who authored the change
    ts               TIMESTAMPTZ
    source           TEXT       -- 'mesa-mcp' | 'esiil-portal' | 'irods-rule:on_data_obj_modify' | etc.
    via_ticket       TEXT       -- ticket id when the change went through an iRODS ticket; NULL otherwise
    rule_invocation  TEXT       -- name of the iRODS rule that emitted this change (when source is 'irods-rule:*')
```

**Effective AVU set at time T for path P** is reconstructed by:

```sql
WITH events AS (
    SELECT attribute, value, unit, op, ts,
           ROW_NUMBER() OVER (
               PARTITION BY attribute, value, unit
               ORDER BY ts DESC
           ) AS rn
    FROM avu_changes
    WHERE irods_path = $1
      AND ts <= $2
)
SELECT attribute, value, unit
FROM events
WHERE rn = 1 AND op = 'add';
```

DuckLake snapshots map 1:1 to rows in `mesa.snapshots`. Time-travel
queries either set the DuckDB snapshot context or filter on `ts <=`.

## Package layout

```
mesa-ducklake/
├── src/mesa_ducklake/
│   ├── __init__.py            # re-exports DuckLakeClient
│   ├── client.py              # DuckLakeClient — the only public facade
│   ├── catalog.py             # Postgres catalog ops (project + snapshot tables)
│   ├── lake.py                # DuckDB / DuckLake connection + Parquet writes
│   ├── schema.py              # SQL DDL + migration runner
│   ├── models.py              # Pydantic models: Project, Snapshot, AvuChange
│   ├── queries.py             # Effective-AVU and history reconstruction queries
│   ├── time_travel.py         # As-of-T and diff helpers
│   └── irods_path.py          # Helpers: project root detection, .mesa path math
├── migrations/
│   └── 0001_initial.sql       # mesa schema DDL
├── tests/
│   ├── conftest.py            # pytest fixtures: ephemeral Postgres, tmp ducklake
│   ├── test_catalog.py
│   ├── test_lake.py
│   ├── test_time_travel.py
│   └── test_e2e.py
├── pyproject.toml
├── config.yaml.example
└── README.md
```

## Public API sketch (DuckLakeClient)

```python
from mesa_ducklake import DuckLakeClient, AvuChange

client = DuckLakeClient(
    postgres_dsn="postgresql://...",
    irods_session=irods_session,   # python-irodsclient session
)

# project lifecycle
project = client.register_project(irods_path="/iplant/home/alice/myproj",
                                  actor="alice")

# write
snap = client.record_changes(
    project_id=project.project_id,
    actor="alice",
    changes=[
        AvuChange(irods_path="/iplant/home/alice/myproj/file.csv",
                  target_type="data_object",
                  attribute="envo.biome", value="tropical moist broadleaf forest",
                  unit="ENVO:01000228", op="add", source="mesa-mcp"),
    ],
    note="Tagged file.csv with ENVO biome",
)

# read — current state
avus = client.get_avus(project.project_id,
                       irods_path="/iplant/home/alice/myproj/file.csv")

# read — time travel
avus_then = client.get_avus_as_of(project.project_id,
                                  irods_path="/iplant/home/alice/myproj/file.csv",
                                  ts="2026-04-01T00:00:00Z")

# history
history = client.get_history(project.project_id,
                             irods_path="/iplant/home/alice/myproj/file.csv")

# diff between snapshots
diff = client.diff(project.project_id,
                   from_snapshot=snap_a.snapshot_id,
                   to_snapshot=snap_b.snapshot_id)
```

## Dependencies

- **Required:** `duckdb` (>=1.1), `psycopg[binary]` (Postgres driver),
  `pydantic` (models), `python-irodsclient` (for reading/writing
  `/.mesa/ducklake/` Parquet files into iRODS).
- **Dev:** `pytest`, `pytest-postgresql` (ephemeral Postgres for tests),
  `ruff`, `mypy`.
- Avoid Django, Flask, requests-heavy stacks — keep this slim.

## iRODS rules, policies, and tickets integration

mesa-ducklake's history is only complete if **every** AVU change reaches
it — not just those made through mesa-mcp. AVUs can also be written by
the iCAT directly (`imeta`), other MCP clients (`irods-mcp-server`,
`terrain-mcp` via Terrain's metadata endpoints), the esiil-portal UI,
or ticket-mediated sessions. To close those gaps:

- **Rule-engine callbacks (preferred catch-all).** A small iRODS rule
  (in `irods-rules/` of this repo, installed once per server by an
  admin) fires on `acPostProcForModifyAVUMetadata` and analogous
  events. The rule shells out to the `mesa-ducklake record` CLI (or
  invokes a Python entry point) with the AVU triple, path, actor,
  and event name. mesa-ducklake records the change with
  `source="irods-rule:<event>"` and `rule_invocation=<rule_name>`.
  This catches `imeta` and any non-MCP client.
- **Ticket provenance.** Whenever the iRODS session that made a change
  was opened against a ticket, the ticket id is recorded in the
  `via_ticket` column. mesa-mcp's `ds_use_ticket` tool sets a session
  attribute so write paths know to populate this. The rule-based
  callback reads `$ticketUserName` (set by the iRODS ticket
  middleware) to do the same.
- **Policy-driven enrollment.** A MESA-defined policy in the iRODS
  Policy Composition Framework can auto-enroll new collections under a
  parent path (e.g., everything under `/iplant/home/<u>/projects/`
  becomes MESA-enabled at creation time). The policy fires
  `mesa_ducklake_init_project` via a rule hook.

This repo therefore contains, beyond the Python library:

```
irods-rules/
├── mesa_avu_change.re          # iRL rule: on AVU change, call back to mesa-ducklake
├── mesa_avu_change.py          # Python rule engine equivalent (alternative)
├── mesa_enroll_policy.re       # Policy: auto-enroll new collections under a parent
└── README.md                   # Install instructions for iRODS admins
```

The CLI entry point used by rules is `mesa-ducklake record`, installed
by `pyproject.toml`'s `[project.scripts]`. It reads change data from
stdin as JSON and exits non-zero on failure (so the rule can react).
The CLI is the **only** sanctioned non-Python interface; everything
else uses `DuckLakeClient`.

## How mesa-mcp uses this library

mesa-mcp's `src/mesa_mcp/ducklake/client.py` imports and wraps
`DuckLakeClient`. The wrapper:
1. Reads mesa-mcp's `config.yaml` for the Postgres DSN.
2. Reuses mesa-mcp's authenticated iRODS session.
3. Exposes the methods needed by the `mesa_ducklake_*` MCP tools.

For local development the same Postgres instance can be reused; for
production each CyVerse deployment runs its own.

## Conventions

- **AVU shape stays canonical:** `(attribute, value, unit)`. Don't add
  fields. Use `source` and `actor` columns for provenance, not the AVU
  triple itself.
- **Snapshots are immutable.** Never UPDATE/DELETE rows in
  `mesa.snapshots` or in any Parquet file. Corrections are new
  snapshots that supersede prior values.
- **Provenance is mandatory.** Every `AvuChange` must populate `actor`
  and `source`. `via_ticket` and `rule_invocation` are populated when
  applicable. A change with empty provenance is a bug — reject it at
  the model layer.
- **Project ids are UUIDs**, generated server-side; never derived from
  paths (collections can be renamed).
- **All timestamps are TIMESTAMPTZ**, stored UTC, serialized as RFC3339.
- **Reads are cheap, writes are auditable.** Every `record_changes`
  call creates a snapshot row; batch related AVU changes into one
  snapshot when they were caused by a single user action.

## Reference repositories

All cloned as siblings under `/home/exouser/`. Read these before making
non-obvious changes:

| Repo | Why it matters |
|---|---|
| `cyverse/mesa-mcp` | Primary consumer; its `CLAUDE.md` has the full architecture context. |
| `cyverse/irods-mcp-server` | AVU tool shapes (Go) — used to keep our AVU model compatible. |
| `cyverse/esiil-portal` | Already writes AVUs with OBO/OLS CURIE units; we must round-trip those without loss. See `portal/services/ols_transform.py`. |

## Working with this repo

- The repo is empty today (LICENSE + .gitignore + README only). The
  first PR should bootstrap `pyproject.toml`, the package skeleton, the
  `0001_initial.sql` migration, and a `DuckLakeClient` whose methods
  raise `NotImplementedError` — proving the import surface and test
  harness before any DuckDB code lands.
- Schema changes are migrations, never in-place edits. Numbered files
  in `migrations/`. The migration runner is the only thing that
  touches the live Postgres schema.
- When adding a new column to `avu_changes`, **also** write a small
  doc note explaining why the AVU triple shape isn't enough — that
  shape is a hard contract with iRODS.
- For non-trivial design work (new query patterns, schema changes,
  snapshot semantics), invoke the `ducklake-engineer` sub-agent.
