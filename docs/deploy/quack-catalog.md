# Quack catalog server (multi-writer DuckDB)

What this page covers: serving one DuckDB-file catalog to several
writer processes with DuckDB 2.0's **Quack** client-server protocol.
The motivating case is several CyVerse Discovery Environment VMs, each
processing different NEON sites, that all record into one MESA AVU
history. It covers starting the server, pointing clients at it,
exposing it beyond localhost, backups, and the current limits.

> **Beta.** Quack ships with DuckDB 2.0 but is not yet declared stable
> by the DuckDB project: the protocol, function names and defaults may
> change in a 2.x release. The Postgres backend remains the
> conservative choice for a hosted deployment. Use Quack where running
> Postgres is the bigger cost.

## How it fits

```
  DE VM 1 ──┐  quack://catalog-host:9494
  DE VM 2 ──┼────────────────────────────►  mesa-ducklake serve-catalog
  mesa-mcp ─┘   (HTTPS via reverse proxy)      owns catalog.duckdb
```

- `mesa-ducklake serve-catalog` opens the DuckDB catalog file, creates
  the `mesa` schema if needed, and serves it. It is the **only** process
  that opens the file. DuckDB's MVCC serializes the writers.
- Clients use `QuackCatalogStore`, selected by a `quack://host:port`
  DSN. The SQL is the DuckDB-file backend's. Clients never create the
  schema themselves: they refuse to start if the server has not
  created it.
- Only the catalog moves to the server. Each client still writes its
  snapshot Parquet into the project's `.mesa/ducklake/` collection in
  iRODS, exactly as with the other backends.

## Starting the server

```bash
export MESA_DUCKLAKE_DSN=duckdb:///var/lib/mesa-ducklake/catalog.duckdb
export MESA_DUCKLAKE_QUACK_TOKEN="$(cat /etc/mesa-ducklake/quack_token)"
mesa-ducklake serve-catalog --port 9494
# {"serving": "quack:localhost:9494"}
```

- `--host` (default `localhost`) and `--port` (default `9494`) set the
  listen address.
- The token must be at least 4 characters. Use a long random one, e.g.
  `openssl rand -hex 32`. Every client presents the same token.
- The command runs until SIGTERM or SIGINT, then stops the server and
  closes the file cleanly, so DuckDB checkpoints it.
- `deploy/mesa-ducklake-catalog.service` is a systemd unit for this.
  It reads the token from `/etc/mesa-ducklake/quack_token` (create it,
  readable by the service user, before the first start). systemd creates
  `/var/lib/mesa-ducklake` for the catalog file and DuckDB's extension
  cache (`StateDirectory=`), so no other directories need to exist. The
  first start downloads the quack extension, so the host needs outbound
  HTTPS then.

A file catalog that was used directly before can be served as is;
DuckDB 2.0 keeps reading and writing the file in its existing format.

## Pointing clients at it

```bash
export MESA_DUCKLAKE_DSN=quack://catalog-host:9494
export MESA_DUCKLAKE_QUACK_TOKEN=…        # the server's token
```

The same DSN works as `catalog_dsn=` in Python and as
`ducklake.catalog_dsn` in mesa-mcp's `config.yaml` (with the token in
the environment). The token is never part of the DSN.

`mesa-ducklake migrate` against a `quack://` DSN checks that the served
schema exists and reports `applied: 0`.

## Exposing it beyond localhost

Quack refuses to listen on anything but `localhost` unless told
otherwise, and the DuckDB project recommends putting a TLS-terminating
reverse proxy in front of it. Do that:

1. Run `serve-catalog` on `localhost`.
2. Proxy HTTPS on the public port to `http://localhost:9494`, e.g. with
   the nginx already used for mesa-mcp. Clients connecting to a host
   other than `localhost` use HTTPS automatically.
3. Firewall the port so only the DE VMs and mesa-mcp hosts can reach it.

`--allow-other-hostname` binds directly to another interface. It
exists for testing on a closed network; the traffic is then not
behind your TLS proxy.

## Throughput and conflicts

- Each client process serializes its own catalog statements (cursors
  of one DuckDB client share one Quack session), and processes run in
  parallel. In testing, three writer processes each committed 200
  snapshots concurrently with no errors and unique snapshot ids.
- The write protocol only appends snapshots (ids come from the server's
  sequence) and updates each writer's own rows, so conflicts are rare.
  A conflicting statement is aborted without effect and retried up to
  three times with backoff.

## Backups

The server is a single point of failure and the catalog file is its
only copy. Back it up like the DuckDB-file catalog
([`duckdb-catalog.md`](./duckdb-catalog.md#backup)): stop the service,
copy `catalog.duckdb` (and any `.wal`) to the Data Store with
`iput -f`, then start it again. Writers fail while it is down and
`mesa-ducklake recover` drains anything left pending.

## Known DuckDB 2.0.0 issues worked around

- Prepared-statement parameters are not forwarded through Quack
  (`Values were not provided for the following parameters`).
  `QuackCatalogStore` binds each parameter as a session variable and
  reads it back with `getvariable()`. Remove this when DuckDB fixes it.
- `USE` does not carry over to new cursors, so each statement's cursor
  runs `USE` first.

## See also

- [`duckdb-catalog.md`](./duckdb-catalog.md): the single-process file
  backend this serves.
- [`postgres.md`](./postgres.md): the other multi-writer backend.
- [`../dev/duckdb-2-migration.md`](../dev/duckdb-2-migration.md): what
  the DuckDB 2.0 upgrade changed.
