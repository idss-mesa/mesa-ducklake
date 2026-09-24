# Queries

What this page covers: the canonical SQL templates that turn the
append-only `avu_changes` fact table into useful answers, the
contract around `EFFECTIVE_AVUS_AS_OF_SQL`, and why the partition
key has to be the full `(attribute, value, unit)` triple. The
authoritative template lives at
[`../../src/mesa_ducklake/queries.py`](../../src/mesa_ducklake/queries.py);
this page is a reference for the contract.

## Single source of truth

The canonical query templates live in `src/mesa_ducklake/queries.py`.
That module exists *only* so that the SQL shape has a single home —
execution wiring lives in `lake.py` and `time_travel.py`, but the
shape of the query is part of the project's contract. Any change to
the template must be mirrored in the time-travel tests in
`tests/test_lake.py` and `tests/test_client_e2e.py` / `tests/test_client_e2e_duckdb.py`.

## `EFFECTIVE_AVUS_AS_OF_SQL`

The reconstruction query reads:

```sql
WITH events AS (
    SELECT attribute, value, unit, op, ts, actor, snapshot_id,
           source, via_ticket, rule_invocation,
           ROW_NUMBER() OVER (
               PARTITION BY attribute, value, unit
               ORDER BY ts DESC, snapshot_id DESC
           ) AS rn
    FROM avu_changes
    WHERE project_id = $1
      AND irods_path = $2
      AND ts <= $3
)
SELECT attribute, value, unit, actor, ts, snapshot_id,
       source, via_ticket, rule_invocation
FROM events
WHERE rn = 1 AND op = 'add';
```

Parameters (positional, libpq `$N` style — DuckDB's `?` placeholder
form is produced by `.replace("$1", "?")` etc. in
`LakeStore.read_effective_avus`):

| Param | Type | Meaning |
|---|---|---|
| `$1` | UUID (as string) | Owning project. |
| `$2` | TEXT | iRODS path of the AVU target. |
| `$3` | TIMESTAMPTZ | Inclusive upper bound on the `ts` window. |

Result columns (in this exact order — `LakeStore.read_effective_avus`
unpacks by position):

`attribute, value, unit, actor, ts, snapshot_id, source, via_ticket, rule_invocation`

`target_type` is intentionally not in the projection. The current
client uses `"data_object"` as a placeholder when rehydrating into
`AvuChange`. If you need the original target type in the result, add
it to both the projection and the `_row_to_change`-equivalent in
`read_effective_avus`.

## Why partition on the full triple

iRODS permits multiple values per attribute on the same target. The
following two AVUs bound to the same `data_object` are both valid
and both meaningful:

- `("envo.biome", "tropical moist broadleaf forest", "ENVO:00000428")`
- `("envo.biome", "savanna", "ENVO:01000204")`

Partitioning the events CTE on `attribute` alone would force one of
these into "supersede" position — the row with the larger `ts` would
win and the other would silently disappear from the effective set.
That is a *correctness* bug, not a performance optimization.
Partitioning on the full triple `(attribute, value, unit)` treats
each (a, v, u) combination as an independent identity and preserves
both rows.

The corollary: do **not** "improve" the query by partitioning on a
shorter key in pursuit of fewer DuckDB temp groups. The contract is
the full triple.

## Why `op='delete'` supersedes earlier `op='add'`

Within each `(attribute, value, unit)` partition the `ROW_NUMBER`
ordering is `ts DESC, snapshot_id DESC`. The latest event wins. The
final `WHERE rn = 1 AND op = 'add'` filter keeps the row only when
the latest event is an `add`.

Two cases:

1. The triple's history ends with `add` — the AVU is currently
   bound. Row is returned.
2. The triple's history ends with `delete` — the AVU is currently
   unbound. Row is dropped.

This matches iRODS iCAT semantics: `imeta rm` followed by `imeta
add` of the same triple is a real "rebind", and the latest
operation determines current state. Corrections are *new* events;
the historical `add` row remains in its original Parquet file
forever.

## Tiebreaker: `snapshot_id DESC`

`ts` alone is not a strict total order — two events recorded in
quick succession can share a timestamp at sub-second granularity,
or be back-dated for replay. `snapshot_id` is monotonic and unique
per project, so `(ts DESC, snapshot_id DESC)` is a total order. Use
this exact tiebreaker in any new query that walks `avu_changes`;
otherwise tests will flake on identical-timestamp inputs.

## Execution in DuckDB

`LakeStore.read_effective_avus` adapts the libpq `$N` placeholders
to DuckDB's `?` placeholders:

```python
sql = EFFECTIVE_AVUS_AS_OF_SQL.replace("$1", "?").replace("$2", "?").replace("$3", "?")
```

This is a string substitution because the template is a constant —
there is no user input to escape. The `con.execute(sql, [...])`
call binds the three parameters positionally.

The `avu_changes` view itself is created on connection open by
`LakeStore._open_read_con`:

- If any `snapshot_*.parquet` files exist in the lake, the view is
  `read_parquet([...explicit list...])`. Reading via an explicit
  list (not a glob) avoids DuckDB's "no files matched" error
  surfacing through deeper code paths.
- If the lake is empty, the view is a typed empty view over a
  temp table with the same shape. Empty-lake reads return no rows
  rather than erroring out.

## Related queries

### History

`LakeStore.read_history` returns every add/delete row for one path,
newest first, no supersede collapse:

```sql
SELECT project_id, snapshot_id, irods_path, target_type, attribute,
       value, unit, op, actor, ts, source, via_ticket, rule_invocation
FROM avu_changes
WHERE project_id = ?
  AND irods_path = ?
ORDER BY ts DESC, snapshot_id DESC
LIMIT ?
```

This is the right shape for audit views. The tiebreaker on
`snapshot_id DESC` matches `EFFECTIVE_AVUS_AS_OF_SQL`.

### Diff

`LakeStore.diff` returns every row whose `snapshot_id` falls
strictly above `from_snapshot` and up to and including
`to_snapshot`:

```sql
SELECT project_id, snapshot_id, irods_path, target_type, attribute,
       value, unit, op, actor, ts, source, via_ticket, rule_invocation
FROM avu_changes
WHERE project_id = ?
  AND snapshot_id > ?
  AND snapshot_id <= ?
ORDER BY ts ASC, snapshot_id ASC
```

If the caller passes `from_snapshot > to_snapshot`, the endpoints
are swapped before the query runs so the range is always well
defined.

## Authoring new queries

When you add a new query shape:

1. **Put the template in `queries.py`.** Even if it is one-line,
   one-use, the single-source-of-truth rule applies.
2. **Use named docstring constants** so the parameter contract
   ($1 / $2 / $3) is documented next to the template.
3. **Mirror the partition / order conventions.** Same partition
   key (full triple) for any "what is currently bound" shape;
   same `ts DESC, snapshot_id DESC` tiebreaker.
4. **Add a regression test** in `tests/test_lake.py` and `tests/test_client_e2e.py` / `tests/test_client_e2e_duckdb.py`
   covering empty lake, single snapshot, and supersede chains.
5. **Don't expose raw SQL through `DuckLakeClient`.** Add a
   method that returns Pydantic models; consumers do not parse
   query results.

## See also

- [`schema.md`](./schema.md) — what every column in `avu_changes`
  means.
- [`architecture.md`](./architecture.md) — how `LakeStore`
  composes the catalog and the lake at read time.
- [`../user/time-travel.md`](../user/time-travel.md) — the
  user-facing walkthrough of the same query.
- [`../../src/mesa_ducklake/queries.py`](../../src/mesa_ducklake/queries.py) —
  the canonical template.
- [`../../src/mesa_ducklake/lake.py`](../../src/mesa_ducklake/lake.py) —
  the execution wiring.
