# Installing iRODS rule callbacks

What this page covers: how an iRODS administrator installs the
mesa-ducklake rule callbacks so AVU changes made *outside*
mesa-mcp — through `imeta`, the esiil-portal UI, other MCP
clients, or any ticket-mediated session — still land in
mesa-ducklake's history. The rules shell out to the
`mesa-ducklake record` CLI documented at
[`../user/cli.md`](../user/cli.md).

> **Status: in progress.** The `irods-rules/` directory and the
> rule scripts described below are being added in parallel by a
> sibling agent. This page documents the intended deployment
> shape per
> [`../../CLAUDE.md`](../../CLAUDE.md) section *"iRODS rules,
> policies, and tickets integration"*. The contract between the
> rules and the CLI is fixed; the rule files themselves are
> still landing.

## Why rule callbacks exist

mesa-ducklake's history is only complete if **every** AVU change
reaches it. mesa-mcp covers everything it does directly, but
AVUs can also be written by:

- `imeta` on the iRODS shell.
- The esiil-portal UI's metadata editor.
- Other MCP clients (`irods-mcp-server`, `terrain-mcp`).
- Ticket-mediated sessions opened by sharing-link consumers.

A small iRODS rule that fires on
`acPostProcForModifyAVUMetadata` (and analogous events) catches
those changes by calling the `mesa-ducklake record` CLI with the
AVU triple, path, actor, and event name. The CLI records the
change with `source="irods-rule:<event>"` and
`rule_invocation=<rule_name>`.

## Contents of `irods-rules/`

The repo's `irods-rules/` directory ships these files (when the
parallel agent's work lands):

| File | Purpose |
|---|---|
| `mesa_avu_change.re` | iRODS native rule language (iRL) implementation. Fires on `acPostProcForModifyAVUMetadata` and shells out to `mesa-ducklake record`. |
| `mesa_avu_change.py` | Python rule engine equivalent for sites using the Python plugin. Same contract; same CLI invocation. |
| `mesa_enroll_policy.re` | Policy hook that auto-enrolls new collections under a configured parent (e.g. everything under `/iplant/home/<u>/projects/`) by calling `mesa_ducklake_init_project`. |
| `README.md` | Per-rule install notes (file checksums, supported iRODS versions, troubleshooting). |

Until the parallel agent's PR merges, treat the file list above
as the contract.

## Prerequisites on the iRODS server

- iRODS 4.3.x with the `irods-server` package installed.
- Network access from the iRODS server to the Postgres catalog
  (see [`postgres.md`](./postgres.md)).
- A Python virtualenv reachable as `mesa-ducklake-record-cli` (or
  similar) on the iRODS server's PATH, with `mesa-ducklake`
  installed.
- The `MESA_DUCKLAKE_DSN` environment variable set in the iRODS
  service's environment (typically `/etc/irods/service_account.config`
  via `systemd` drop-in, or a wrapper script invoked by the rule).

A quick sanity check:

```bash
sudo -u irods env | grep MESA_DUCKLAKE_DSN
sudo -u irods which mesa-ducklake
sudo -u irods bash -c 'echo "{}" | mesa-ducklake record; echo "exit: $?"'
```

The last command should exit `1` with a "malformed input" message
on stderr — proving the CLI is on `$PATH` and the DSN is being
read.

## Installing the native iRL rule

1. **Copy the rule file to `/etc/irods/`.** iRODS resolves rule
   files relative to its rule path, configured in
   `/etc/irods/server_config.json`.

   ```bash
   sudo cp irods-rules/mesa_avu_change.re /etc/irods/
   sudo chown irods:irods /etc/irods/mesa_avu_change.re
   sudo chmod 0644 /etc/irods/mesa_avu_change.re
   ```

2. **Register the rule in `server_config.json`.** Add the file to
   the `re_rulebase_set` list, *before* `core` so it takes
   precedence:

   ```json
   "plugin_configuration": {
     "rule_engines": [
       {
         "instance_name": "irods_rule_engine_plugin-irods_rule_language-instance",
         "plugin_name": "irods_rule_engine_plugin-irods_rule_language",
         "plugin_specific_configuration": {
           "re_data_variable_mapping_set": ["core"],
           "re_function_name_mapping_set": ["core"],
           "re_rulebase_set": ["mesa_avu_change", "core"],
           "regexes_for_supported_peps": [
             "ac[^ ]*",
             "msi[^ ]*",
             "[^ ]*pep_[^ ]*_(pre|post|except|finally)"
           ]
         },
         "shared_memory_instance": "irods_rule_language_rule_engine"
       }
     ]
   }
   ```

3. **Reload iRODS.**

   ```bash
   sudo systemctl restart irods
   sudo systemctl status irods
   ```

   Watch the logs for rule-language parse errors:

   ```bash
   sudo journalctl -u irods -f
   ```

## Installing the Python rule (alternative)

For deployments using the Python rule engine plugin,
`mesa_avu_change.py` plugs in as a normal Python rule script:

```bash
sudo cp irods-rules/mesa_avu_change.py /etc/irods/
sudo chown irods:irods /etc/irods/mesa_avu_change.py
sudo chmod 0644 /etc/irods/mesa_avu_change.py
```

Register it in `server_config.json` under the Python rule
engine's `re_rulebase_set` instead of the native plugin's. Pick
*one* of the two implementations per server — running both
double-records every change.

## Testing with `irule`

Once installed, you can fire the rule manually with `irule -F`:

```bash
cat > /tmp/test_avu_change.r <<'EOF'
test_mesa_avu_change {
    mesa_avu_change(
        "/iplant/home/alice/myproj/file.csv",
        "data_object",
        "envo.biome",
        "tropical moist broadleaf forest",
        "ENVO:00000428",
        "add",
        "alice"
    )
}

INPUT null
OUTPUT ruleExecOut
EOF

irule -F /tmp/test_avu_change.r
```

Then verify the change landed:

```bash
PGPASSWORD=... psql "postgresql://mesa@catalog/mesa_ducklake" <<'SQL'
SELECT s.snapshot_id, s.ts, s.actor, s.note
FROM mesa.snapshots s
JOIN mesa.projects p ON p.project_id = s.project_id
WHERE p.irods_path = '/iplant/home/alice/myproj'
ORDER BY s.snapshot_id DESC
LIMIT 5;
SQL
```

A new snapshot row should appear. Reading the corresponding
Parquet file (in `/.mesa/ducklake/` inside the project) via
`DuckLakeClient.get_history` will show the rule-sourced row with
`source = 'irods-rule:acPostProcForModifyAVUMetadata'`.

## Ticket provenance

Whenever the iRODS session that made the change was opened
against a ticket, the rule reads `$ticketUserName` (set by the
iRODS ticket middleware) and passes it to the CLI as
`via_ticket`. mesa-mcp's `ds_use_ticket` tool sets a session
attribute that the in-process write path reads to do the same
thing. The end result: every AVU change that flowed through a
ticket has the ticket id stored in `avu_changes.via_ticket`,
regardless of which client made the change.

## Policy-driven enrollment

`mesa_enroll_policy.re` auto-enrolls new collections under a
configured parent path. The default policy is:

> Any collection created under
> `/iplant/home/<user>/projects/` is registered as MESA-enabled
> at creation time. Its root gets `mesa.enabled=true` and the
> `/.mesa/ducklake/` subcollection is created.

The parent paths are configured by editing constants at the top
of the rule file. Restart iRODS after edits.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| AVU changes via `imeta` are not appearing in mesa-ducklake. | Rule not loaded — check `server_config.json` order. Or the CLI exits non-zero — check `journalctl -u irods` for stderr. |
| CLI exits with code 3. | `MESA_DUCKLAKE_DSN` not set in iRODS service environment. |
| CLI exits with code 2. | The path is not MESA-enabled. Expected behavior for non-MESA collections; the rule should swallow it silently. |
| Doubled rows in `avu_changes` for a single `imeta` call. | Both the native and Python rule are registered. Pick one. |
| `via_ticket` always NULL even on ticketed sessions. | The rule is not reading `$ticketUserName`. Check the rule version matches the CLI's expected contract. |

## See also

- [`../user/cli.md`](../user/cli.md) — the `mesa-ducklake record`
  CLI contract this page's rules invoke.
- [`per-project-storage.md`](./per-project-storage.md) — what the
  rule-driven writes produce in `/.mesa/ducklake/`.
- [`postgres.md`](./postgres.md) — the catalog the rules write
  to via the CLI.
- [`../../CLAUDE.md`](../../CLAUDE.md) — section *"iRODS rules,
  policies, and tickets integration"* for the design rationale.
