# CLI: `mesa-ducklake`

What this page covers: the `mesa-ducklake` command-line tool and its
three verbs:

- `record` forwards one AVU change from an iRODS rule callback, or any
  other non-Python caller.
- `recover` drains the crash-recovery write-ahead log.
- `migrate` prepares the catalog schema.

The CLI is the only sanctioned non-Python interface to the library.
Everything else uses `DuckLakeClient` directly (see
[`usage.md`](./usage.md)).

The implementation is in
[`../../src/mesa_ducklake/cli.py`](../../src/mesa_ducklake/cli.py).
Its module docstring is the canonical wire contract.

## Synopsis

```bash
mesa-ducklake [record]                 # JSON on stdin; "record" is the default verb
mesa-ducklake recover                  # no stdin
mesa-ducklake migrate [--target N]     # no stdin
```

There is no `--help` flag. Any first argument other than `record`,
`recover` or `migrate` is rejected with `unknown_verb` (exit 1).

When no verb is given, the CLI runs `record`. This keeps the iRODS rule
callback, which runs plain `mesa-ducklake`, working.

## Configuration

Every verb reads the catalog DSN from the environment:

```
MESA_DUCKLAKE_DSN   catalog DSN, required
```

The backend is chosen from the DSN, as with `DuckLakeClient(catalog_dsn=...)`:

| DSN form | Backend |
|---|---|
| `postgresql://…` or `postgres://…` | Postgres |
| libpq keyword DSN containing `host=` or `dbname=` | Postgres |
| `duckdb:///abs/path/catalog.duckdb` | DuckDB file (recommended form: absolute path) |
| `duckdb://relative.duckdb`, `something.duckdb`, `:memory:` | DuckDB file, resolved relative to the CWD (or in-memory) |

```bash
export MESA_DUCKLAKE_DSN="postgresql://mesa:mesa@localhost:5432/mesa_ducklake"
# or, for a single-user local install:
export MESA_DUCKLAKE_DSN="duckdb:///home/alice/.local/share/mesa/catalog.duckdb"
```

If `MESA_DUCKLAKE_DSN` is unset or empty, every verb exits with code 3
before doing anything else.

A DuckDB-file catalog is **single-writer**: only one OS process can
hold the file at a time. This fits a local install where the same
process both writes and reads. It is a poor fit for the rule-callback
path, where `record` runs on the iRODS server host once per AVU event,
possibly concurrently. Use Postgres there. See
[`../deploy/duckdb-catalog.md`](../deploy/duckdb-catalog.md).

The CLI does not read `config.yaml.example`, which is only a hint for
human operators.

Handled errors are written to **stderr** as a single-line JSON envelope
(see the known gap under [Exit codes](#exit-codes)):

```json
{"code": "invalid_input", "message": "missing required field: 'attribute'"}
```

On success, the result is written to **stdout** as one JSON object.

## `record`: the rule-callback path

`record` is meant for iRODS rules and other non-Python processes
that observed an AVU change and need to record it. Library users
should call `DuckLakeClient.record_changes` from Python instead.

```
iRODS server fires acPostProcForModifyAVUMetadata
   -> mesa_avu_change.re / mesa_avu_change.py
        -> runs: mesa-ducklake record   (JSON on stdin)
              -> resolves the project
              -> writes one snapshot (one AVU change) via DuckLakeClient.record_changes
              -> prints the result on stdout, exits 0
```

This catches AVU changes made by `imeta`, `irods-mcp-server`, the
esiil-portal UI, or any other client that bypasses mesa-mcp.

### Stdin: one flat JSON object

`record` reads **one AVU change** per invocation, as a single flat JSON
object:

```json
{
  "irods_path": "/iplant/home/alice/myproj/file.csv",
  "target_type": "data_object",
  "attribute": "envo.biome",
  "value": "tropical moist broadleaf forest",
  "unit": "ENVO:01000228",
  "op": "add",
  "actor": "alice",
  "source": "irods-rule:acPostProcForModifyAVUMetadata",
  "rule_invocation": "mesa_avu_change",
  "via_ticket": null,
  "ts": "2026-09-24T17:02:11Z"
}
```

| Field | Required | Notes |
|---|---|---|
| `irods_path` | yes | Full iRODS path the AVU is bound to. Without `project_id`, the CLI walks up this path's parents until it finds a registered project root. |
| `target_type` | yes | `data_object`, `collection`, `resource` or `user`. |
| `attribute` | yes | The A of the canonical AVU triple. |
| `value` | yes | The V. |
| `unit` | no | The U. A missing value or `null` becomes `""`, which matches iCAT. |
| `op` | yes | `"add"` or `"delete"`. |
| `actor` | yes | iRODS user who made the change. Must not be empty or whitespace (rejected at the model layer). |
| `source` | no | Provenance. Conventionally `"irods-rule:<event>"`. A missing or empty value becomes `"mesa-ducklake-cli"`. A whitespace-only value is rejected. |
| `project_id` | no | UUID of the project. When present, it skips the path walk. An unknown id fails with `unknown_project`. |
| `ts` | no | RFC 3339 timestamp (a trailing `Z` is accepted). Defaults to now (UTC). |
| `via_ticket` | no | Ticket id when the change went through an iRODS ticket. Empty becomes `null`. |
| `rule_invocation` | no | Name of the rule that emitted the change, e.g. `"mesa_avu_change"`. Empty becomes `null`. |

Keys not in this table are ignored. There is no `note` field: the
snapshot note is generated as `"<op> AVU via <source>"`. Each
invocation creates exactly one snapshot holding one AVU change.

### Stdout on success

```json
{"snapshot_id": 42, "parquet_file": "snapshot_42.parquet", "project_id": "6f1c…"}
```

iRODS rules usually ignore stdout and rely on the exit code.

### What `record` does not do

`record` never opens an iRODS session. It builds its client with
`irods_session=None`, so the write takes the **local-only** path:

- The Parquet file is written to the local cache of the host running
  the CLI (`platformdirs.user_cache_dir("mesa-ducklake")`, usually the
  `irods` service account's cache).
- The file is **not** pushed to `<project>/.mesa/ducklake/` in iRODS,
  and no `mesa.pending_pushes` row is created.

As a result, a later session-backed read from another host sees the
catalog row but cannot pull that Parquet from iRODS. Cache eviction on
the rule host can also remove the only copy. Until `record` gains an
iRODS push, treat rule-captured history as local to the rule host.
This is tracked in [`../../NEXT_STEPS.md`](../../NEXT_STEPS.md).

The CLI also does not authorize anything. The rule callback that
invokes it runs after iRODS has already allowed the metadata change;
the CLI's job is only to record it.

## `recover`: drain the write-ahead log

```bash
mesa-ducklake recover
```

`recover` takes no stdin. It opens an iRODS session from
`IRODS_ENVIRONMENT_FILE`, or from `~/.irods/irods_environment.json`
when that is unset. It then calls
`DuckLakeClient.recover_pending_pushes()`, which drains up to 100 rows
of `mesa.pending_pushes`. For each row it either re-pushes the Parquet
to iRODS and commits, drops an orphaned or already-committed row, or
marks the snapshot `failed` after `DEFAULT_MAX_ATTEMPTS` (5) attempts.
[`../dev/architecture.md`](../dev/architecture.md#crash-recovery)
describes the algorithm.

Run it on service start, after an iRODS outage, and after restoring
the catalog from backup (see [`../deploy/backup.md`](../deploy/backup.md)).

Stdout on success is a counter summary:

```json
{"pushed": 1, "committed": 1, "failed": 0, "orphaned": 0, "missing_local": 0}
```

## `migrate`: prepare the catalog schema

```bash
mesa-ducklake migrate              # apply every pending migration
mesa-ducklake migrate --target 1   # stop after migration 0001 (Postgres only)
```

- **Postgres:** applies every pending `migrations/NNNN_*.sql` file in
  order and records each one in `mesa.schema_versions`. `--target N`
  is an inclusive upper bound. The command is idempotent; re-running
  reports `applied: 0`.

  ```json
  {"applied": 2, "target": null, "backend": "postgres"}
  ```

- **DuckDB file:** there is no migration history. The DuckDB backend
  creates its schema when the file is opened. `migrate` opens the
  file, which creates it and its parent directories if needed, runs
  the idempotent bootstrap, closes it, and reports zero migrations
  applied. `target` echoes whatever `--target` value was passed.

  ```json
  {"applied": 0, "target": null, "backend": "duckdb"}
  ```

`--target` must be followed by an integer. Anything else is
`invalid_input` (exit 1). A migration error is `migration_failed`
(exit 2), and the failing migration is rolled back.

## Exit codes

| Code | Verb | `code` in stderr | Meaning |
|---|---|---|---|
| 0 | all | none | Success. |
| 1 | any | `unknown_verb` | First argument is not `record`, `recover` or `migrate`. |
| 1 | `record` | `invalid_input` | Empty stdin, non-object JSON, a missing required field, bad `ts`, bad `project_id`, or model validation failure (for example empty `actor`, bad `op` or `target_type`). |
| 1 | `record` | `catalog_unreachable` | The catalog client could not be constructed. In practice this rarely fires, because the catalog is opened lazily; see the note below. |
| 1 | `record` | `unknown_project` | `project_id` was given but is not in the catalog. |
| 1 | `record` | `record_failed` | The catalog or Parquet write failed. |
| 1 | `recover` | `irods_env_missing` | The iRODS environment file is not readable. |
| 1 | `recover` | `recover_failed` | Any other failure while draining. |
| 1 | `migrate` | `invalid_input` | Bad `--target` usage. |
| 2 | `record` | `not_mesa_enabled` | No registered project covers `irods_path`. Rule callbacks should treat this as "not for us" and skip it silently. |
| 2 | `migrate` | `migration_failed` | A migration (or the DuckDB bootstrap) raised an error. |
| 3 | all | `missing_dsn` | `MESA_DUCKLAKE_DSN` is unset or empty. Operator error. |

**Known gap:** in `record`, an error while *opening* the catalog (a
Postgres outage, or a DuckDB file locked by another process) happens
during project lookup and is not caught. The process exits 1 with a
Python traceback on stderr instead of a JSON envelope.

For `record`, the split between 1 and 2 lets a rule tell "this AVU
change isn't for us" (2: skip) apart from "we tried to record it and
something broke" (1: log and alert).

## Minimal example

```bash
export MESA_DUCKLAKE_DSN="postgresql://mesa@/mesa_ducklake"

mesa-ducklake migrate

cat <<'EOF' | mesa-ducklake record
{
  "irods_path": "/iplant/home/alice/myproj/file.csv",
  "target_type": "data_object",
  "attribute": "envo.biome",
  "value": "tropical moist broadleaf forest",
  "unit": "ENVO:01000228",
  "op": "add",
  "actor": "alice",
  "source": "irods-rule:acPostProcForModifyAVUMetadata",
  "rule_invocation": "mesa_avu_change"
}
EOF
echo "exit: $?"
```

The project root, `/iplant/home/alice/myproj`, must already be
registered (by mesa-mcp's `mesa_ducklake_init_project` or by
`DuckLakeClient.register_project`). Otherwise the command exits 2.

## See also

- [Usage](./usage.md): the equivalent Python API for in-process
  callers.
- [`../deploy/irods-rules.md`](../deploy/irods-rules.md): installing
  the rules that invoke `record`.
- [`../deploy/postgres.md`](../deploy/postgres.md) and
  [`../deploy/duckdb-catalog.md`](../deploy/duckdb-catalog.md):
  running `migrate` against each backend.
- [`../dev/schema.md`](../dev/schema.md): the `AvuChange` model that
  the JSON payload maps onto.
