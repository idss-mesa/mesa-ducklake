# Per-project storage

What this page covers: the layout of each project's DuckLake under
`<project_root>/.mesa/ducklake/` (the default sub-collection), the access
controls expected on that subcollection, capacity expectations,
and the (currently nonexistent) pruning policy. This is the bulk
of mesa-ducklake's data — the Postgres catalog is small, but the
per-project Parquet files grow with every AVU change.

## Why per-project storage

The rule "metadata travels with the data" is a hard project
constraint. AVU history is metadata about the iRODS data it
describes; if a researcher exports their project to a new tenant
or archives it to cold storage, the metadata must travel with
it. That is only possible if the metadata files live in the
project, not in a central database. The Postgres catalog is
deliberately a thin index — `mesa.projects` says where to look,
`mesa.snapshots` says which file in that location belongs to
which snapshot, and that is it.

## Directory layout

For a project at `/iplant/home/alice/myproj`, the DuckLake lives
at:

```
/iplant/home/alice/myproj/
├── (the project's own science data)
└── .mesa/
    └── ducklake/
        ├── snapshot_1.parquet
        ├── snapshot_2.parquet
        ├── snapshot_3.parquet
        └── ...
```

Naming rules:

- The sub-collection is `.mesa/ducklake/` relative to the project
  root by default. The path math is in
  [`../../src/mesa_ducklake/irods_path.py`](../../src/mesa_ducklake/irods_path.py)
  (`ducklake_subpath`, `DEFAULT_DATA_COLLECTION`). See
  [Configuring the sub-collection](#configuring-the-sub-collection).
- Each file is **always** `snapshot_<id>.parquet`. The `<id>` is
  the `snapshot_id` from `mesa.snapshots`. Names are stable
  forever once written; the library never renames.
- The filename column stored in `mesa.snapshots.parquet_file` is
  the *relative* name (`snapshot_<id>.parquet`) — not the
  absolute path. The base path comes from
  `mesa.projects.ducklake_path`.

There is no manifest, index, or "current snapshot" pointer file.
The catalog's committed `mesa.snapshots` rows say which files belong
to the project. Before a read, `irods_sync.ensure_cached` pulls any
of those files that are missing into the local cache
(`<cache_dir>/<project_id>/`). `LakeStore` then reads every
`snapshot_*.parquet` it finds in that cache directory
(`existing_parquet_files`).

Two more points about the files:

- **Gaps in the numbering are normal.** `snapshot_id` comes from one
  sequence that the whole catalog shares, so other projects' snapshots
  use up numbers too. A snapshot that failed before it committed also
  leaves a gap.
- **The files are written locally first.** DuckDB can't write to
  iRODS, so each file is written to the local cache and then pushed
  here with a checksum check. Its catalog row is committed only after
  the push. See [the write protocol](../dev/architecture.md#write-protocol-push-before-commit).

## Reading a project's history without the catalog

The catalog is not stored in iRODS. It lives wherever the
deployment's `catalog_dsn` points, for example a Postgres server or
a local install's `~/.mesa/ducklake/catalog.duckdb`. Even so, a
project's `.mesa/ducklake/` can be read on its own, because every
Parquet row carries its `project_id`, `snapshot_id`, `ts`, `actor`,
`source` and `op`. That makes the files plain Parquet. They are
**not** a DuckLake-extension lake, so `ATTACH 'ducklake:…'` won't
open them (see
[architecture](../dev/architecture.md#duckdb--ducklake-usage-today)).

Read-only access needs nothing but DuckDB. With iRODS tools, copy the
files first (`gocmd get` / `iget -r <project>/.mesa/ducklake`). A
collection that is deliberately published anonymously, as
neon-ducklake is, can be read in place over HTTPS. Publishing the
project publishes its AVU history too. That is the exception to
[ACLs and ownership](#acls-and-ownership), not a reason to relax it
for other projects. `read_parquet` can't glob an HTTP directory, so list the files
yourself:

```sql
INSTALL httpfs; LOAD httpfs;
SET VARIABLE base = 'https://data.cyverse.org/dav-anon/iplant/home/tswetnam/neon-ducklake/.mesa/ducklake/';
CREATE VIEW avu_changes AS
SELECT * FROM read_parquet([
    getvariable('base') || 'snapshot_5.parquet',
    getvariable('base') || 'snapshot_10.parquet'   -- one entry per file in the listing
]);

SELECT snapshot_id, count(*) AS n, min(ts), max(ts), any_value(source)
FROM avu_changes GROUP BY 1 ORDER BY 1;
```

The effective-AVU query in [`../dev/queries.md`](../dev/queries.md)
runs unchanged against that view.

What the files alone don't give you:

- **The snapshot index.** That means notes, `parent_snapshot`, and
  which snapshots are `pending` or `failed`. The push happens before
  the commit, so a crash between the two leaves a file here for a
  snapshot that stays `pending` until `mesa-ducklake recover` runs.
  Apart from that window, the files here are the committed history.
- **`DuckLakeClient` reads.** The client lists snapshots from its
  catalog before it reads any Parquet. A fresh catalog doesn't know
  about the copied files, and a project registered there gets a new
  `project_id` that the copied rows don't carry. There is no "rebuild
  the catalog from Parquet" command today. To test the client against
  real history, use plain SQL over a read-only copy, or record the
  copied rows into a scratch project with `record_changes`.

Never copy history files into another project's `.mesa/ducklake/`.
They would mix two projects' rows in one collection, and that
project's catalog would not list them.

## Configuring the sub-collection

The sub-collection name is configurable per client:

```python
DuckLakeClient(catalog_dsn=..., irods_session=..., data_collection="_history")
# new projects get <project_root>/_history
```

mesa-mcp exposes this setting as `ducklake.data_collection` in its
config (default `.mesa/ducklake`). It forwards non-default values to
mesa-ducklake versions that accept the parameter.

The setting applies **only when a project is registered**.
`register_project` resolves the full path and stores it in
`mesa.projects.ducklake_path`, and every later push and pull uses that
stored value. So:

- Changing `data_collection` for an existing deployment does **not**
  move any data. Existing projects keep reading and writing their
  original location.
- Only projects registered after the change use the new name.
- Moving an existing project's files means moving the iRODS collection
  *and* updating that project's `ducklake_path` row. This is a manual
  operator task that the library does not support.

Keep the leading dot unless you have a reason not to (see below).

## ACLs and ownership

The `.mesa/ducklake/` subcollection should be:

- **Owned by the project owner** (typically the iRODS user who
  registered the project, recorded as `mesa.projects.created_by`).
  Owner has read/write/own.
- **Readable by the same set of users as the project root.**
  AVU history is no more secret than the AVUs themselves — but
  no less, either. Inheriting ACLs from the project root is the
  practical default.
- **Not world-readable.** Default permissions on a CyVerse home
  collection do not grant `public` access; `.mesa/ducklake/`
  should not relax that.

The library does not set ACLs itself. The
`mesa_ducklake_init_project` tool in mesa-mcp creates the
subcollection with `ichmod inherit` against the project root, so
new files inherit the project's ACL set automatically. iRODS
admins should verify this on a fresh deployment:

```bash
ils -A /iplant/home/alice/myproj/.mesa/ducklake
```

The expected output shows the owner with `own` and (depending
on the project's policy) the project's collaborators with
`read`.

## Why a hidden subcollection

The leading dot in `.mesa/` is deliberate:

- iRODS clients and most web UIs hide dot-prefixed entries by
  default, keeping the user's project view uncluttered.
- A dot-prefix is a convention for "metadata about this
  directory, not data inside it" that crosses platforms (Unix
  `.git`, `.cache`, `.svn`, etc.).
- It signals to other tooling that this subcollection is
  managed and should not be edited by hand.

The convention is part of the contract. Do not move or rename an
existing project's sub-collection. A new deployment may pick a
different `data_collection` before it registers any projects.

## Capacity expectations

Each row in `avu_changes` is roughly:

- 13 columns of mostly short strings + two integers + one
  timestamp.
- Parquet compresses the columnar layout aggressively;
  repetition (the same `actor`, `source`, `project_id`,
  `target_type` across a snapshot's rows) compresses very well.

A reasonable rule of thumb is **~1 KB per AVU change on disk**
after compression, dominated by `irods_path` and `value`. A
project with:

- 10,000 data objects
- 5 AVUs each (50,000 AVU events at first ingest)
- 10% churn per year (5,000 more events per year)

would use ~50 MB at first ingest and grow ~5 MB per year. Even
the largest projects in CyVerse are well under 1 GB of Parquet
history per year.

The per-file size depends on the snapshot's batch size. A
single-AVU snapshot is a small Parquet file with non-trivial
fixed overhead — typically a few KB regardless of payload. For
projects with many small interactive edits, the per-file
overhead dominates the row payload.

## Pruning policy

**There is no automatic pruning today.** AVU change history is
permanent by design: it is what makes time travel work for
arbitrary past timestamps, and what supports audit requests
from compliance and grant reviewers.

If a project's history grows large enough to be a problem, the
manual workflow is:

1. An iRODS administrator coordinates with the project owner to
   identify the cutoff timestamp `T`.
2. The owner exports the effective AVU set as of `T` via
   `DuckLakeClient.get_avus_as_of` (one call per path, or a
   bulk variant once one exists).
3. A new project is registered alongside the old one with
   `register_project`.
4. The owner records the exported AVUs into the new project as
   a single bootstrap snapshot.
5. The old project is `status = 'archived'` and its lake is
   moved to cold storage. The old `.mesa/ducklake/` files
   remain readable for compliance lookups.

This is intentionally heavyweight — pruning loses information,
and a manual flow forces explicit operator consent. There is no
"prune older than 90 days" knob and there is not planned to be
one.

## Backups

The per-project Parquet files are backed up by **the iRODS
deployment's backup policy** — typically the same nightly
replication or snapshot that protects the rest of
`/iplant/home/<user>/`. mesa-ducklake does not need its own
backup story for the lakes; it does for the catalog (see
[`postgres.md`](./postgres.md)).

In a disaster scenario:

- The catalog can be restored from `pg_dump` (see
  [`backup.md`](./backup.md)). After restore, run
  `mesa-ducklake migrate` to apply any newer schema versions, then
  `mesa-ducklake recover`.
- The Parquet lakes are restored by the iRODS backup. No
  application-level action is needed.
- If a single Parquet file is lost but the catalog row remains,
  the corresponding snapshot's data is gone; the catalog row can
  be left as an "orphan" or `DELETE`d. Subsequent reads ignore
  missing files: `ensure_cached` logs a warning and DuckDB reads
  what exists. The gap shows up in `get_history` as missing events.
  `ensure_cached` also stops at the first failed pull, so snapshots
  after the missing one may not be pulled into a cold cache for that
  read either. Recovery from this is
  a one-off operator task.

## See also

- [`irods-rules.md`](./irods-rules.md) — how rule-driven writes
  populate the lakes.
- [`postgres.md`](./postgres.md) — the catalog that indexes
  these lakes.
- [`../dev/architecture.md`](../dev/architecture.md) — how
  `LakeStore` and `LakeStorage` read these files.
- [`../dev/schema.md`](../dev/schema.md) — the conceptual schema
  of the Parquet rows.
- [`../../CLAUDE.md`](../../CLAUDE.md) — "Resolved decisions"
  section, item *Data file location*.
