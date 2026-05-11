# Per-project storage

What this page covers: the on-disk layout of each project's
DuckLake under `<project_root>/.mesa/ducklake/`, the access
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

- The subdirectory is **always** `.mesa/ducklake/` relative to
  the project root. The path math is in
  [`../../src/mesa_ducklake/irods_path.py`](../../src/mesa_ducklake/irods_path.py)
  (`ducklake_subpath`).
- Each file is **always** `snapshot_<id>.parquet`. The `<id>` is
  the `snapshot_id` from `mesa.snapshots`. Names are stable
  forever once written; the library never renames.
- The filename column stored in `mesa.snapshots.parquet_file` is
  the *relative* name (`snapshot_<id>.parquet`) — not the
  absolute path. The base path comes from
  `mesa.projects.ducklake_path`.

There is no manifest, index, or "current snapshot" pointer file.
The set of `snapshot_*.parquet` files in the directory is the
authoritative state; `LakeStore._open_read_con` discovers them
via a directory glob (`existing_parquet_files`).

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

The convention is part of the contract — do not move the
subcollection or rename it.

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

- The catalog can be restored from `pg_dump`. After restore, run
  `apply_migrations` to apply any newer schema versions.
- The Parquet lakes are restored by the iRODS backup. No
  application-level action is needed.
- If a single Parquet file is lost but the catalog row remains,
  the corresponding snapshot's data is gone; the catalog row can
  be left as an "orphan" or `DELETE`d. Subsequent reads ignore
  missing files (DuckDB lists what exists), but the gap shows
  up in `get_history` as missing events. Recovery from this is
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
