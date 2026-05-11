# mesa-ducklake documentation

What this page covers: the entry point to mesa-ducklake's user, developer,
and deployment documentation. mesa-ducklake is the Python library that
tracks AVU (Attribute / Value / Unit) metadata history for MESA-enabled
iRODS projects, using DuckDB's DuckLake lakehouse pattern (Postgres
catalog plus per-project Parquet files).

Start here:

- **Library users** (people writing Python that imports
  `mesa_ducklake`) — read [`user/usage.md`](./user/usage.md).
- **iRODS rule authors and operators** wiring rule callbacks into the
  CLI — read [`user/cli.md`](./user/cli.md).
- **Analysts and auditors** who need to see what AVUs were set on a
  path at a past timestamp — read [`user/time-travel.md`](./user/time-travel.md).
- **Contributors** changing schema, queries, or client code — read
  [`dev/architecture.md`](./dev/architecture.md) and
  [`dev/contributing.md`](./dev/contributing.md).
- **Operators** standing up a deployment — read
  [`deploy/postgres.md`](./deploy/postgres.md) first.

## Table of contents

### User

- [Usage](./user/usage.md) — the `DuckLakeClient` Python API with
  concrete examples.
- [CLI](./user/cli.md) — the `mesa-ducklake record` callback CLI used by
  iRODS rules.
- [Time travel](./user/time-travel.md) — reading AVUs as of a past
  timestamp; history and diff patterns.

### Developer

- [Architecture](./dev/architecture.md) — the catalog / lake split, the
  `LakeStorage` protocol seam.
- [Schema](./dev/schema.md) — Postgres `mesa.projects` /
  `mesa.snapshots` plus the Parquet `avu_changes` fact table.
- [Adding migrations](./dev/adding-migrations.md) — numbering rule,
  append-only contract, the bookkeeping table bootstrap.
- [Queries](./dev/queries.md) — `EFFECTIVE_AVUS_AS_OF_SQL` contract;
  why the partition key is the full triple.
- [Contributing](./dev/contributing.md) — PR conventions, the
  `ducklake-engineer` sub-agent.

### Deploy

- [Postgres](./deploy/postgres.md) — Ubuntu 24.04 provisioning of the
  catalog database.
- [iRODS rules](./deploy/irods-rules.md) — installing the rule
  callbacks on an iRODS server (in progress).
- [Per-project storage](./deploy/per-project-storage.md) —
  `/.mesa/ducklake/` layout, ACLs, capacity expectations.

## See also

- [`../CLAUDE.md`](../CLAUDE.md) — full architecture context, hard
  contracts, and the conceptual schema.
- [`../README.md`](../README.md) — the repo's minimal introduction.
- [`../migrations/0001_initial.sql`](../migrations/0001_initial.sql) —
  the current Postgres schema.
