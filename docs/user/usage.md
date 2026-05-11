# Usage — the DuckLakeClient API

What this page covers: how to use `mesa_ducklake.DuckLakeClient` from
Python. This is the only supported entry point of the library; every
other module in `mesa_ducklake` is internal. Examples here mirror the
shapes used by the sibling project `cyverse/mesa-mcp`.

## Installation

`mesa-ducklake` is a regular Python package. Install editable from a
checkout, or pin the version once it is published.

```bash
pip install -e ".[dev]"
```

Runtime dependencies (pulled automatically by `pip`):
`duckdb>=1.1`, `psycopg[binary]>=3.2`, `pydantic>=2.6`,
`python-irodsclient>=2.0`, `pytz`. Python 3.11 or newer is required.

## Importing the public surface

Only the names re-exported from the package root are part of the
public API.

```python
from mesa_ducklake import DuckLakeClient, AvuChange, Project, Snapshot
```

Anything else — `mesa_ducklake.catalog`, `mesa_ducklake.lake`,
`mesa_ducklake.queries`, etc. — is internal and may change without
notice.

## Instantiating the client

`DuckLakeClient` needs a Postgres DSN for the catalog and an
authenticated `python-irodsclient` session. In production the session
is what gives the client access to write Parquet files into iRODS; in
tests and local development you can pass any opaque object and use
`lake_root_override` to point the lake at a local directory.

```python
from mesa_ducklake import DuckLakeClient
from irods.session import iRODSSession

irods = iRODSSession(host="data.cyverse.org", port=1247,
                    user="alice", password="...", zone="iplant")

with DuckLakeClient(
    postgres_dsn="postgresql://mesa:mesa@localhost:5432/mesa_ducklake",
    irods_session=irods,
) as client:
    ...
```

The client is a context manager; `__exit__` closes the Postgres
connection. For tests or short-lived scripts you can also call
`client.close()` directly.

The `lake_root_override` argument lets tests redirect every project's
Parquet directory to a local path (typically `tmp_path`):

```python
with DuckLakeClient(
    postgres_dsn=postgres_dsn,
    irods_session=dummy_session,
    lake_root_override="/tmp/mesa-lakes",
) as client:
    ...
```

In production leave it `None`; the iRODS-backed `LakeStorage` will
plug in when it lands (see
[`../dev/architecture.md`](../dev/architecture.md)).

## Registering a project

A MESA-enabled iRODS project must be registered in the catalog before
any AVU changes can be recorded against it. The project's
`ducklake_path` defaults to `<irods_path>/.mesa/ducklake`.

```python
project = client.register_project(
    irods_path="/iplant/home/alice/myproj",
    actor="alice",
    zone="iplant",
)

project.project_id       # UUID, server-generated
project.ducklake_path    # '/iplant/home/alice/myproj/.mesa/ducklake'
project.status           # 'active'
```

`register_project` inserts a row in `mesa.projects` and returns the
`Project` model. The catalog enforces `UNIQUE(irods_path)`; registering
the same path twice raises a `psycopg.errors.UniqueViolation`.

To look up an existing project:

```python
project = client.get_project(project_id)                 # raises KeyError if missing
maybe   = client.find_project_by_path("/iplant/home/alice/myproj")  # returns None if missing
```

## Recording AVU changes

A *snapshot* is one atomic batch of AVU add/delete events — one user
action, one row in `mesa.snapshots`, one Parquet file. Build the list
of `AvuChange` records and pass them in:

```python
from mesa_ducklake import AvuChange

snap = client.record_changes(
    project_id=project.project_id,
    actor="alice",
    changes=[
        AvuChange(
            irods_path="/iplant/home/alice/myproj/file.csv",
            target_type="data_object",
            attribute="envo.biome",
            value="tropical moist broadleaf forest",
            unit="ENVO:00000428",
            op="add",
            actor="alice",
            source="mesa-mcp",
        ),
    ],
    note="Tagged file.csv with ENVO biome",
)

snap.snapshot_id      # int, monotonic per-project
snap.parquet_file     # 'snapshot_<id>.parquet'
snap.parent_snapshot  # previous snapshot id for this project, or None
```

Rules:

- The `changes` list must be non-empty — `record_changes` raises
  `ValueError` if it is. Empty snapshots are a bug, not a feature.
- Every `AvuChange` must populate `attribute`, `value`, `target_type`,
  `op`, and `actor`. `unit` defaults to `""` (matching iCAT semantics
  for "no unit").
- `source` defaults to `"mesa-mcp"`. Set it to `"irods-rule:<event>"`
  for changes captured through a rule callback (see
  [`cli.md`](./cli.md)).
- `op` is `"add"` or `"delete"`. Corrections are *new* snapshots with
  a delete row followed by an add row in a later snapshot — never
  rewrite a Parquet file.
- `project_id` and `snapshot_id` on the input `AvuChange` are ignored
  if set; `record_changes` stamps the correct values before writing.
- If the Parquet write fails, the snapshot row is rolled back so the
  catalog index does not dangle.

### Provenance fields: `via_ticket` and `rule_invocation`

When the change went through an iRODS ticket, populate `via_ticket`
with the ticket id:

```python
AvuChange(
    irods_path="/iplant/home/alice/myproj/shared.csv",
    target_type="data_object",
    attribute="envo.biome", value="tropical moist broadleaf forest",
    unit="ENVO:00000428",
    op="add",
    actor="bob",
    source="mesa-mcp",
    via_ticket="abc123",
)
```

When the change originated in an iRODS rule, set `source` to
`"irods-rule:<event>"` and `rule_invocation` to the rule's name:

```python
AvuChange(
    irods_path="/iplant/home/alice/myproj/file.csv",
    target_type="data_object",
    attribute="curated.by", value="alice", unit="",
    op="add",
    actor="alice",
    source="irods-rule:acPostProcForModifyAVUMetadata",
    rule_invocation="mesa_avu_change",
)
```

Both fields default to `None` and are nullable in Parquet.

## Reading current AVUs

`get_avus` returns the effective AVU set bound to a path *right now*
(as of `datetime.now(tz=UTC)`):

```python
avus = client.get_avus(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
)

for a in avus:
    print(a.attribute, a.value, a.unit, a.actor, a.ts)
```

The result is a list of `AvuChange` rows with `op="add"` whose
`(attribute, value, unit)` triple has not been superseded by a later
`op="delete"` for the same triple at the same path. The
`target_type` of returned rows is set to `"data_object"` because the
effective-AVU projection does not carry the original target type (see
[`../dev/queries.md`](../dev/queries.md) for why).

## Reading historic AVUs (time travel)

`get_avus_as_of` accepts either a `datetime` or an RFC 3339 string:

```python
avus_then = client.get_avus_as_of(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
    ts="2026-04-01T00:00:00Z",
)
```

See [`time-travel.md`](./time-travel.md) for a worked example.

## Raw history of a path

`get_history` returns every add/delete row for a path, newest first.
Unlike `get_avus`, it does *not* collapse supersedes — you see the
raw event stream.

```python
events = client.get_history(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
    limit=100,
)

for e in events:
    print(e.ts, e.op, e.attribute, e.value, e.unit, e.actor, e.source)
```

## Snapshot listing and diff

`list_snapshots` returns recent snapshots for a project, newest first:

```python
snaps = client.list_snapshots(project.project_id, limit=20)
```

`diff` returns every AVU change strictly between two snapshot ids
(`> from_snapshot` and `<= to_snapshot`), chronological:

```python
changes = client.diff(
    project.project_id,
    from_snapshot=snap_a.snapshot_id,
    to_snapshot=snap_b.snapshot_id,
)
```

If `from_snapshot > to_snapshot`, the endpoints are swapped so the
range is always well-defined.

## Hard contracts to be aware of

- The AVU triple `(attribute, value, unit)` is canonical and matches
  what iRODS iCAT stores. Never split, merge, or rename those three
  fields in your application code.
- Snapshots and Parquet files are append-only. The library never
  rewrites an existing Parquet file; corrections are new snapshots.
- One `record_changes` call equals one snapshot equals one Parquet
  write. Batch related AVU changes into a single call when they were
  caused by a single user action.

## See also

- [CLI](./cli.md) — the `mesa-ducklake record` callback used by
  iRODS rules.
- [Time travel](./time-travel.md) — worked example of as-of-T reads.
- [`../dev/schema.md`](../dev/schema.md) — column-by-column reference
  for the Pydantic models above.
- [`../../CLAUDE.md`](../../CLAUDE.md) — full architecture and
  contracts.
