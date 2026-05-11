# CLI — `mesa-ducklake record`

What this page covers: the `mesa-ducklake record` command-line tool
used by iRODS rule callbacks (and any other non-Python caller) to
forward AVU change events into mesa-ducklake. The CLI is the only
sanctioned non-Python interface to the library — everything else uses
`DuckLakeClient` directly.

> **Status: in progress.** The CLI itself is being added in parallel
> (`src/mesa_ducklake/cli.py`, exposed via `[project.scripts]` in
> `pyproject.toml`) by a sibling agent. This page documents the
> intended surface per
> [`../../CLAUDE.md`](../../CLAUDE.md) section
> *"iRODS rules, policies, and tickets integration"*. The contract
> below is what the rule callbacks in
> [`../deploy/irods-rules.md`](../deploy/irods-rules.md) will rely
> on.

## When to use the CLI

You only call the CLI directly if you are an iRODS rule or another
non-Python process that observed an AVU change and needs to record
it in mesa-ducklake. Library users should use
`DuckLakeClient.record_changes` from Python (see
[`usage.md`](./usage.md)).

The typical caller chain is:

```
iRODS server fires acPostProcForModifyAVUMetadata
   -> mesa_avu_change.re   (iRODS native rule)
        -> shells out to:   mesa-ducklake record
              -> reads JSON from stdin
              -> writes one snapshot via DuckLakeClient.record_changes
              -> exits 0 on success
```

This catches AVU changes made by `imeta`, `irods-mcp-server`, the
esiil-portal UI, or any other client — anything mesa-mcp does not
touch directly.

## Invocation

```bash
mesa-ducklake record
```

The command takes no positional arguments. It reads its payload from
**stdin** as JSON and emits human-readable status to stderr. Stdout
is reserved for machine-readable result data (currently the assigned
`snapshot_id` on success).

## Configuration

The CLI reads its Postgres DSN from the environment:

```
MESA_DUCKLAKE_DSN   libpq DSN for the catalog database, required.
```

Example:

```bash
export MESA_DUCKLAKE_DSN="postgresql://mesa:mesa@localhost:5432/mesa_ducklake"
```

If `MESA_DUCKLAKE_DSN` is unset, the CLI exits with code 3 (see
[Exit codes](#exit-codes)).

The CLI does **not** read `config.yaml.example`; that file is only a
hint for human operators. It also does not open an iRODS session of
its own — the rule callback that invokes it has already validated
that the user is permitted to change the metadata; the CLI's job is
recording, not authorization.

## Stdin JSON contract

The CLI accepts a single JSON object on stdin. The schema mirrors the
arguments to `DuckLakeClient.record_changes` plus the project lookup
needed to resolve `project_id` from `irods_path`.

```json
{
  "irods_path": "/iplant/home/alice/myproj",
  "actor": "alice",
  "note": "imeta add via rule",
  "changes": [
    {
      "irods_path": "/iplant/home/alice/myproj/file.csv",
      "target_type": "data_object",
      "attribute": "envo.biome",
      "value": "tropical moist broadleaf forest",
      "unit": "ENVO:00000428",
      "op": "add",
      "actor": "alice",
      "source": "irods-rule:acPostProcForModifyAVUMetadata",
      "via_ticket": null,
      "rule_invocation": "mesa_avu_change"
    }
  ]
}
```

Field semantics:

| Field | Required | Notes |
|---|---|---|
| `irods_path` (top-level) | yes | Project root, used to resolve `project_id`. Must be a MESA-enabled project; otherwise exit code 2. |
| `actor` (top-level) | yes | iRODS user who triggered the change; recorded on the snapshot row. |
| `note` (top-level) | no | Optional human-readable commit message. |
| `changes` | yes, non-empty | List of AVU change objects matching the `AvuChange` Pydantic model (see [`../dev/schema.md`](../dev/schema.md)). |
| `changes[].irods_path` | yes | Full iRODS path the AVU is bound to (may be a child of the project root). |
| `changes[].target_type` | yes | One of `data_object`, `collection`, `resource`, `user`. |
| `changes[].attribute` / `value` / `unit` | yes / yes / no | Canonical AVU triple. `unit` defaults to `""`. |
| `changes[].op` | yes | `"add"` or `"delete"`. |
| `changes[].actor` | yes | Per-row actor (usually identical to the top-level `actor`). |
| `changes[].source` | yes | Conventionally `"irods-rule:<event>"` when emitted by a rule. |
| `changes[].via_ticket` | no | iRODS ticket id when the session used a ticket; the rule reads `$ticketUserName`. |
| `changes[].rule_invocation` | no | Name of the iRODS rule that emitted the change (e.g. `"mesa_avu_change"`). |

## Stdout on success

A single JSON object describing the created snapshot is written to
stdout:

```json
{"snapshot_id": 42, "parquet_file": "snapshot_42.parquet"}
```

iRODS rules typically ignore stdout and rely on the exit code.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success. Snapshot was recorded. |
| 1 | Generic error: malformed JSON, validation failure on an `AvuChange`, Postgres or Parquet write failure. Stderr contains the message. |
| 2 | The project at the given `irods_path` is not MESA-enabled (not registered, or its root collection lacks `mesa.enabled=true`). The rule callback should swallow this so non-MESA traffic is not blocked. |
| 3 | The `MESA_DUCKLAKE_DSN` environment variable is missing. Operator error. |

The split between 1 and 2 lets rule callbacks distinguish "this AVU
change isn't for us" (code 2 — silently skip) from "we tried to
record it and something broke" (code 1 — log and alert).

## Minimal example

```bash
export MESA_DUCKLAKE_DSN="postgresql://mesa@/mesa_ducklake"

cat <<'EOF' | mesa-ducklake record
{
  "irods_path": "/iplant/home/alice/myproj",
  "actor": "alice",
  "note": "imeta add via rule",
  "changes": [
    {
      "irods_path": "/iplant/home/alice/myproj/file.csv",
      "target_type": "data_object",
      "attribute": "envo.biome",
      "value": "tropical moist broadleaf forest",
      "unit": "ENVO:00000428",
      "op": "add",
      "actor": "alice",
      "source": "irods-rule:acPostProcForModifyAVUMetadata",
      "rule_invocation": "mesa_avu_change"
    }
  ]
}
EOF

echo "exit: $?"
```

## See also

- [Usage](./usage.md) — the equivalent Python API for in-process
  callers.
- [`../deploy/irods-rules.md`](../deploy/irods-rules.md) — installing
  the rule that invokes this CLI on an iRODS server.
- [`../dev/schema.md`](../dev/schema.md) — the underlying `AvuChange`
  model the JSON payload maps onto.
- [`../../CLAUDE.md`](../../CLAUDE.md) — section *"iRODS rules,
  policies, and tickets integration"* for the design rationale.
