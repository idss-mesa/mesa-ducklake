# mesa-ducklake — Where we left off

Snapshot of state and outstanding work, written so a future-you can
pick this up cold.

## What's live

**Postgres catalog** — `mesa_ducklake` database on
`mesa-mcp.cis240692.projects.jetstream-cloud.org` (Postgres 16, local
socket only). Owned by role `mesa`; password at
`/etc/mesa-mcp/secrets/postgres_password` (root:exouser, 0640).

**Schema state** — `mesa.projects`, `mesa.snapshots`,
`mesa.schema_versions` tables present. `0001_initial.sql` applied
and recorded.

**Tests** — 65 pass (44 unit + 21 requires_postgres against the
pytest-postgresql ephemeral instance). Ruff clean. CLI installed:
`.venv/bin/mesa-ducklake` (entry point `mesa_ducklake.cli:_console_main`).

## What's NOT done yet

**iRODS rule callback installation.** [`irods-rules/`](irods-rules/) contains:

- `mesa_avu_change.re` — iRL rule for `acPostProcForModifyAVUMetadata`.
- `mesa_avu_change.py` — Python Rule Engine equivalent.
- `mesa_enroll_policy.re` — auto-enrolls new collections under a
  parent that has `mesa.auto_enroll=true`.

These need to be installed on the CyVerse iRODS server (not this VM —
this VM is the *client*) by an iRODS admin. Until then, AVU writes
made directly via `imeta` or other clients won't reach mesa-ducklake.

See [`docs/deploy/irods-rules.md`](docs/deploy/irods-rules.md) for the
admin install steps and `irule -F` testing.

**No real-world data yet.** No project has been registered via
`DuckLakeClient.register_project`. To smoke-test the catalog end-to-end:

```bash
cd /home/exouser/mesa-ducklake
PG_PASSWORD=$(sudo cat /etc/mesa-mcp/secrets/postgres_password)
.venv/bin/python <<PY
from mesa_ducklake import DuckLakeClient, AvuChange
client = DuckLakeClient(
    postgres_dsn=f"postgresql://mesa:{'$PG_PASSWORD'}@127.0.0.1:5432/mesa_ducklake",
    irods_session=None,           # not used for these calls
)
project = client.register_project(
    irods_path="/iplant/home/tswetnam/mesa-smoke",
    actor="tswetnam",
    zone="iplant",
)
print("registered:", project)
PY
```

(Once mesa-mcp tools are routing AVU writes through DuckLake, this
happens automatically on first write to a MESA-enabled project.)

## To finish the rule-callback path (when ready)

1. Build a test iRODS server (Docker image of `cyverse/iRODS-runner` or
   similar) for local validation.
2. Drop `irods-rules/mesa_avu_change.re` into `/etc/irods/` on the test
   server. Register in `server_config.json` per the README in that dir.
3. From inside the iRODS container:
   ```bash
   irule -F /etc/irods/mesa_avu_change_smoke.r '*path="/tempZone/home/rods/file.txt"' '*attr=k' '*value=v' '*unit='
   ```
4. Confirm the change lands in `mesa_ducklake` via the catalog query.
5. When happy, coordinate with CyVerse ops to deploy on production iRODS.

## Other follow-ups

- **iRODS-backed `LakeStorage` implementation.** The Protocol seam is
  in [`src/mesa_ducklake/lake.py`](src/mesa_ducklake/lake.py); today
  only `LocalLakeStorage` exists (writes Parquet to a local
  filesystem path). Add an `iRODSLakeStorage` that writes Parquet
  blobs to `<project_root>/.mesa/ducklake/` via `python-irodsclient`.
  That is the "metadata travels with the data" promise from
  `CLAUDE.md` — currently the local filesystem stands in.
- **Schema version 0002**, if it becomes needed (e.g., adding a column
  to `mesa.snapshots`). Numbering and append-only rules in
  [`docs/dev/adding-migrations.md`](docs/dev/adding-migrations.md).
- **Production retention policy.** Today nothing prunes old Parquet
  files. Per `docs/deploy/per-project-storage.md`, ~1KB per AVU
  change; small projects won't notice, large ones eventually will.

## Pointers

- Architecture & contracts: [`CLAUDE.md`](CLAUDE.md).
- Docs entry: [`docs/README.md`](docs/README.md).
- The companion server: [`/home/exouser/mesa-mcp/`](../mesa-mcp/) — see
  its `NEXT_STEPS.md` for the OIDC / iRODS-account follow-ups.

## Quick commands

```bash
# Run the test suite
cd /home/exouser/mesa-ducklake
.venv/bin/pytest -q                 # 65 pass

# Inspect the live catalog
PGPASSWORD=$(sudo cat /etc/mesa-mcp/secrets/postgres_password) \
    psql -h 127.0.0.1 -U mesa -d mesa_ducklake -c 'SELECT * FROM mesa.schema_versions;'

# Run the CLI manually with a sample change
PG_PASSWORD=$(sudo cat /etc/mesa-mcp/secrets/postgres_password)
MESA_DUCKLAKE_DSN="postgresql://mesa:${PG_PASSWORD}@127.0.0.1:5432/mesa_ducklake" \
    .venv/bin/mesa-ducklake record < some-change.json
```
