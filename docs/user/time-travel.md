# Time travel — reading AVUs at a past timestamp

What this page covers: how mesa-ducklake reconstructs the effective
AVU set for an iRODS path at any historical timestamp, the
append-only model that makes it work, and worked examples for
`get_avus_as_of`, `get_history`, and `diff`.

## The append-only model

Every AVU change in mesa-ducklake is recorded as a new row in the
Parquet `avu_changes` fact table. No row is ever rewritten. There
are exactly two operations:

- `op = "add"` — the AVU triple `(attribute, value, unit)` becomes
  bound to the path.
- `op = "delete"` — the AVU triple becomes unbound from the path.

If a user corrects a mistake (say, fixing a typo in a value), the
"correction" is two new rows in a new snapshot: a `delete` of the
old triple followed by an `add` of the new triple. The original
`add` row stays in its original Parquet file forever.

This is why time travel works at all: the database knows what every
AVU change looked like at the moment it was recorded, plus the
timestamp it was recorded. Reading "what AVUs were set on this path
at 2026-04-01T00:00:00Z" is just a windowed query over the event
stream.

## How the reconstruction works

The effective AVU set for path `P` as of timestamp `T` is computed
by:

1. Take every row from `avu_changes` where `irods_path = P` and
   `ts <= T`.
2. Partition the rows by the full triple `(attribute, value, unit)`.
3. Inside each partition, keep only the latest row (ordered by `ts`
   then `snapshot_id`, both descending).
4. Return the rows where the surviving latest row has `op = "add"`.
   Rows whose latest event is `op = "delete"` are excluded — the AVU
   was unbound by `T`.

The canonical SQL template lives at
[`../../src/mesa_ducklake/queries.py`](../../src/mesa_ducklake/queries.py)
and the developer reference is in
[`../dev/queries.md`](../dev/queries.md).

The crucial design point is that the partition key is the **full
triple**, not the attribute alone. iRODS permits the same attribute
to have multiple values bound at once (e.g. multiple ENVO terms on a
single data object). Partitioning on the attribute would silently
discard valid AVUs; partitioning on the triple preserves them.

## A worked example

Consider one iRODS data object,
`/iplant/home/alice/myproj/file.csv`, that goes through three
snapshots over the course of a project:

| Snapshot | Timestamp | Change |
|---|---|---|
| 7 | 2026-03-01T10:00:00Z | `add envo.biome = "tropical moist broadleaf forest" (ENVO:00000428)` |
| 12 | 2026-04-15T09:30:00Z | `add envo.biome = "savanna" (ENVO:01000204)` |
| 18 | 2026-05-02T14:00:00Z | `delete envo.biome = "tropical moist broadleaf forest" (ENVO:00000428)` |

Reading at three different timestamps gives three different
effective AVU sets.

### Before the first change

```python
avus = client.get_avus_as_of(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
    ts="2026-02-15T00:00:00Z",
)
# avus == []
```

No rows with `ts <= 2026-02-15T00:00:00Z`, so the effective set is
empty.

### Between the second and third snapshots

```python
avus = client.get_avus_as_of(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
    ts="2026-04-20T12:00:00Z",
)

for a in avus:
    print(a.attribute, "=", a.value, "unit=", a.unit)
# envo.biome = tropical moist broadleaf forest unit= ENVO:00000428
# envo.biome = savanna                          unit= ENVO:01000204
```

Both `add` events have happened and neither has been superseded by
a delete on the same triple. Both AVUs are effective. Notice that
the `attribute` is the same on both — partitioning on the full
triple is what keeps both rows.

### After all three snapshots

```python
avus = client.get_avus_as_of(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
    ts="2026-05-10T00:00:00Z",
)

for a in avus:
    print(a.attribute, "=", a.value, "unit=", a.unit)
# envo.biome = savanna unit= ENVO:01000204
```

The `(envo.biome, "tropical moist broadleaf forest", ENVO:00000428)`
triple's latest event is now `op = "delete"`, so it is excluded.
The savanna triple's latest event is still `add`, so it remains.

`client.get_avus(...)` is just `get_avus_as_of(..., now)`; it would
return the same result as the post-snapshot-18 query.

## Inspecting the raw event stream

When you need to see *every* add/delete for a path (not the collapsed
effective set), use `get_history`:

```python
events = client.get_history(
    project.project_id,
    irods_path="/iplant/home/alice/myproj/file.csv",
    limit=100,
)

for e in events:
    print(e.ts.isoformat(), e.op, e.attribute, "=", e.value, "by", e.actor, "via", e.source)
```

Output (newest first):

```
2026-05-02T14:00:00+00:00 delete envo.biome = tropical moist broadleaf forest by alice via mesa-mcp
2026-04-15T09:30:00+00:00 add    envo.biome = savanna                          by alice via mesa-mcp
2026-03-01T10:00:00+00:00 add    envo.biome = tropical moist broadleaf forest by alice via mesa-mcp
```

`get_history` is the right tool when auditing *who* changed *what*
and *when*, including changes that have since been superseded.

## Diffing two snapshots

`diff` returns the AVU rows whose `snapshot_id` is strictly greater
than `from_snapshot` and less than or equal to `to_snapshot`, in
chronological order. It is the cheapest way to answer "what
changed between these two points in the project's history?".

```python
changes = client.diff(
    project.project_id,
    from_snapshot=7,
    to_snapshot=18,
)

for c in changes:
    print(c.snapshot_id, c.op, c.attribute, "=", c.value)
# 12 add    envo.biome = savanna
# 18 delete envo.biome = tropical moist broadleaf forest
```

If `from_snapshot > to_snapshot`, the endpoints are swapped — the
range is always well-defined.

## Timestamp formats

`ts` accepts either a `datetime` (TZ-aware preferred — naive values
are assumed to be UTC) or an RFC 3339-ish string. The trailing `Z`
suffix is supported.

```python
client.get_avus_as_of(pid, p, ts="2026-04-01T00:00:00Z")
client.get_avus_as_of(pid, p, ts="2026-04-01T00:00:00+00:00")
client.get_avus_as_of(pid, p, ts=datetime(2026, 4, 1, tzinfo=UTC))
```

Internally all timestamps are normalized to TZ-aware UTC by
`mesa_ducklake.time_travel.parse_as_of`.

## See also

- [Usage](./usage.md) — the rest of the `DuckLakeClient` API.
- [`../dev/queries.md`](../dev/queries.md) — the
  `EFFECTIVE_AVUS_AS_OF_SQL` template and why it is shaped that way.
- [`../dev/schema.md`](../dev/schema.md) — the `avu_changes`
  conceptual schema with every column annotated.
