# Provisioning Postgres for the catalog

What this page covers: installing and configuring a Postgres
database to host mesa-ducklake's catalog schema on Ubuntu 24.04.
The catalog only needs the `mesa` schema — it can coexist with the
iRODS iCAT schema in the same cluster, or run standalone for
development.

## Requirements

- Ubuntu 24.04 LTS (or Debian 12+). Other Linux distributions work;
  paths to `pg_hba.conf` and the service name differ.
- Postgres 16 (Ubuntu 24.04's default). Postgres 14+ is enough
  feature-wise (`gen_random_uuid()` from `pgcrypto` is built-in
  since 13).
- ~1 GB free disk in `/var/lib/postgresql/` for a typical
  development catalog. The catalog itself is small — the bulk
  storage is the per-project Parquet lakes in iRODS (see
  [`per-project-storage.md`](./per-project-storage.md)).

## Install Postgres

```bash
sudo apt update
sudo apt install -y postgresql-16 postgresql-contrib-16
sudo systemctl enable --now postgresql
sudo systemctl status postgresql
```

Verify the service is up and the cluster is at the expected port:

```bash
sudo -u postgres psql -c "SHOW server_version;"
sudo -u postgres psql -c "SHOW port;"
```

## Create the role and database

mesa-ducklake conventionally uses the role `mesa` and the database
`mesa_ducklake`. Adjust to taste; the rest of the docs assume these
names.

```bash
sudo -u postgres createuser --pwprompt mesa
sudo -u postgres createdb --owner=mesa mesa_ducklake
```

`createuser --pwprompt` will ask for a password interactively; the
DSN later becomes
`postgresql://mesa:<password>@localhost:5432/mesa_ducklake`.

For a development setup where you do not want a password, use
peer authentication instead (see below).

## Authentication

mesa-ducklake's runtime opens connections via libpq DSNs. The two
practical authentication setups are:

### Local socket peer auth (development)

For a single-host dev setup where the OS user `mesa` owns the
catalog connections, edit
`/etc/postgresql/16/main/pg_hba.conf` and ensure these lines are
above the more permissive defaults:

```
# TYPE  DATABASE        USER       ADDRESS         METHOD
local   mesa_ducklake   mesa                       peer
local   all             postgres                   peer
```

Reload Postgres:

```bash
sudo systemctl reload postgresql
```

Verify:

```bash
sudo -u mesa psql -d mesa_ducklake -c "SELECT current_user, current_database();"
```

With peer auth in place, the DSN omits the password:

```
postgresql://mesa@/mesa_ducklake
```

### TCP password auth (production)

For production where the catalog must accept TCP from another
host, edit `/etc/postgresql/16/main/postgresql.conf`:

```
listen_addresses = 'localhost,10.0.0.5'    # add the catalog NIC
```

Then in `pg_hba.conf`, allow the specific CIDR for the `mesa`
role:

```
# TYPE  DATABASE        USER   ADDRESS          METHOD
host    mesa_ducklake   mesa   10.0.0.0/24      scram-sha-256
```

Reload and verify:

```bash
sudo systemctl reload postgresql
PGPASSWORD=<password> psql "postgresql://mesa@10.0.0.5/mesa_ducklake" -c "SELECT 1"
```

Use `scram-sha-256`, never `md5` or `trust`, for any TCP path.

## Apply migrations

The migration runner is part of the Python package. Once the role
and database exist, install mesa-ducklake (in a venv on the
catalog host or on any host that can reach it) and run the
migrations:

```bash
python -m venv /opt/mesa-ducklake/.venv
source /opt/mesa-ducklake/.venv/bin/activate
pip install mesa-ducklake     # or pip install -e . from a checkout

python -c "
from mesa_ducklake.schema import apply_migrations
n = apply_migrations('postgresql://mesa@/mesa_ducklake')
print(f'applied {n} migrations')
"
```

Expected output on a fresh database:

```
applied 1 migrations
```

Re-running is idempotent:

```
applied 0 migrations
```

For TCP setups, pass the full DSN with credentials:

```bash
python -c "
from mesa_ducklake.schema import apply_migrations
import os
print(apply_migrations(os.environ['MESA_DUCKLAKE_DSN']))
"
```

The runner creates `mesa.schema_versions` automatically (it is the
bootstrap table that records which migrations have been applied —
see [`../dev/adding-migrations.md`](../dev/adding-migrations.md)).

## Verify the schema

```bash
sudo -u mesa psql -d mesa_ducklake <<'SQL'
\dn mesa
\dt mesa.*
SELECT version, filename, applied_at FROM mesa.schema_versions ORDER BY version;
SQL
```

You should see the `mesa` schema, the `mesa.projects` and
`mesa.snapshots` tables, the `mesa.schema_versions` bookkeeping
table, and at least one row for `0001_initial.sql`.

## Routine operations

### Back up the catalog

The catalog is small but precious — it is the index of every
project's Parquet lake. A regular `pg_dump` is sufficient:

```bash
sudo -u postgres pg_dump -Fc mesa_ducklake > /backup/mesa_ducklake-$(date +%F).dump
```

The Parquet lakes themselves live in iRODS and are backed up by
the iRODS deployment, not by `pg_dump`.

### Restore the catalog

```bash
sudo -u postgres dropdb mesa_ducklake
sudo -u postgres createdb --owner=mesa mesa_ducklake
sudo -u postgres pg_restore -d mesa_ducklake /backup/mesa_ducklake-2026-05-10.dump
```

After restore, run `apply_migrations` to apply any newer schema
versions that were not in the dump.

### Inspecting catalog state

```sql
-- How many projects are registered?
SELECT count(*) FROM mesa.projects;

-- Most recently registered projects.
SELECT project_id, irods_path, created_by, created_at
FROM mesa.projects
ORDER BY created_at DESC
LIMIT 10;

-- Snapshot rate over the last 24 hours.
SELECT date_trunc('hour', ts) AS hr, count(*)
FROM mesa.snapshots
WHERE ts > now() - interval '24 hours'
GROUP BY 1 ORDER BY 1;

-- Find a project's most recent snapshots.
SELECT snapshot_id, ts, actor, note
FROM mesa.snapshots
WHERE project_id = '...'
ORDER BY snapshot_id DESC
LIMIT 20;
```

## Pitfalls

- **Do not run `apply_migrations` against the iCAT database.**
  Even though the schemas can coexist in one cluster, give the
  catalog its own database. Migrations only touch the `mesa`
  schema, but an accidental DSN swap is best made impossible
  rather than recoverable.
- **Never `md5` or `trust` over TCP.** Use `scram-sha-256`.
- **Do not put the role password in version control.** Use a
  systemd unit's `EnvironmentFile=` or a secrets manager. The
  `config.yaml.example` is illustrative; nothing in mesa-ducklake
  reads it at runtime.

## See also

- [`per-project-storage.md`](./per-project-storage.md) — where
  the actual AVU rows live.
- [`irods-rules.md`](./irods-rules.md) — installing the rule
  callbacks that depend on this catalog.
- [`../dev/adding-migrations.md`](../dev/adding-migrations.md) —
  the migration runner and the numbering rule.
- [`../dev/schema.md`](../dev/schema.md) — column-by-column
  reference for the tables created here.
