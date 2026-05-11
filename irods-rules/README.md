# mesa-ducklake iRODS rule callbacks

This directory holds **server-side** rule code that closes the gap
between mesa-mcp and direct iRODS clients (`imeta`, other MCP servers,
the esiil-portal UI). Without these rules, AVU changes made outside
mesa-mcp never reach DuckLake, breaking the "every AVU change is
auditable" invariant documented in `../CLAUDE.md`.

There are three files plus a wrapper script you write locally:

| File                        | Engine             | Purpose                                                          |
| --------------------------- | ------------------ | ---------------------------------------------------------------- |
| `mesa_avu_change.re`        | iRODS Rule Language | Fires on `acPostProcForModifyAVUMetadata`; mirrors AVU to DuckLake. |
| `mesa_avu_change.py`        | Python Rule Engine  | Same as above for sites running the Python rule engine.          |
| `mesa_enroll_policy.re`     | iRODS Rule Language | Auto-enrols new collections under a parent with `mesa.auto_enroll=true`. |

Both `.re` files invoke a wrapper script `mesa-ducklake-record-wrapper.sh`
that sets `MESA_DUCKLAKE_DSN` (and any other env vars the CLI needs)
and execs `mesa-ducklake record`. The wrapper exists so admins can
keep secrets out of the rule base.

## Wrapper script (admin writes)

Save this as `/usr/local/bin/mesa-ducklake-record-wrapper.sh` (or
wherever the iRODS `re_msiexec_cmd_dir` config points) and make it
executable (`chmod +x`):

```bash
#!/usr/bin/env bash
set -uo pipefail
# DSN for the mesa-ducklake catalog. Keep this file 0700 root:root.
export MESA_DUCKLAKE_DSN="postgresql://mesa:mesa@127.0.0.1:5432/mesa_catalog"
# Path to the mesa-ducklake virtualenv (or system install).
export PATH="/opt/mesa-ducklake/.venv/bin:$PATH"
# Bound the CLI so a rule callback can never hang an iRODS agent.
exec timeout 10 mesa-ducklake "$@"
```

## Install — iRODS Rule Language

1. Copy `mesa_avu_change.re` and `mesa_enroll_policy.re` into the
   rule directory configured in
   `/etc/irods/server_config.json`:

   ```jsonc
   "plugin_configuration": {
     "rule_engines": [
       {
         "instance_name": "irods_rule_engine_plugin-irods_rule_language-instance",
         "plugin_specific_configuration": {
           "re_rulebase_set": [
             "core",
             "mesa_avu_change",
             "mesa_enroll_policy"
           ]
         }
       }
     ]
   }
   ```

2. Restart the iRODS server (`sudo systemctl restart irods`).

3. Smoke test the callback. From a client with `imeta`:

   ```bash
   imeta add -d /tempZone/home/alice/test.csv envo.biome forest ENVO:00000446
   ```

   Then on the iRODS server check the catalog:

   ```bash
   psql "$MESA_DUCKLAKE_DSN" -c "SELECT snapshot_id, ts, actor FROM mesa.snapshots ORDER BY snapshot_id DESC LIMIT 5;"
   ```

   You should see the new snapshot row, with the AVU change in the
   corresponding Parquet file.

4. To exercise the rule body directly (without changing a real AVU):

   ```bash
   irule -F mesa_avu_change.re '*Option="add"' '*ItemType="-d"' \
       '*ItemName="/tempZone/home/alice/test.csv"' '*AttrName="k"' \
       '*AttrValue="v"' '*AttrUnit=""'
   ```

## Install — Python Rule Engine

1. Copy `mesa_avu_change.py` to the directory configured by the
   `python` rule engine instance, typically
   `/etc/irods/python_rule_engine_plugins/`.

2. In `server_config.json`, register the file in the Python rule
   engine instance's `plugin_specific_configuration.rule_base_set`.

3. Restart iRODS and re-run the `imeta` smoke test above.

## Troubleshooting

* **Nothing reaches DuckLake** — check the iRODS server log
  (`/var/log/irods/rodsServer.log`) for `mesa-ducklake` lines.
  The wrapper script's stderr is logged there. The most common
  cause is `MESA_DUCKLAKE_DSN` not being set, which the CLI
  reports with exit code 3.

* **CLI exits 2** — the path isn't in a MESA-enabled project.
  This is expected for AVU changes outside MESA projects. If you
  *did* enrol the parent, run mesa-mcp's
  `mesa_ducklake_init_project` to register it in the catalog.

* **CLI exits 1 with `record_failed`** — the catalog write itself
  failed. The error message in the JSON envelope on stderr is
  the underlying psycopg / DuckDB error.

* **Rule body never fires** — the PEP name might differ between
  iRODS versions. iRODS 4.3 introduced new dynamic PEPs; check
  `pep_api_mod_avu_metadata_pre` / `_post` if
  `acPostProcForModifyAVUMetadata` doesn't seem to dispatch on
  your install.

* **High latency on `imeta`** — the rule blocks until the CLI
  returns. The wrapper script's `timeout 10` caps this at 10
  seconds, but if you see persistent slowness, run the CLI
  asynchronously via a delayed rule:

  ```
  delay("<PLUSET>10s</PLUSET>") {
      msiExecCmd("mesa-ducklake-record-wrapper.sh", ...);
  }
  ```
