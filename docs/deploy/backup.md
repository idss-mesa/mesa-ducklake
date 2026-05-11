# Postgres catalog backup

What this page covers: the daily backup pipeline that dumps the
mesa-ducklake Postgres catalog into iRODS, and the procedure to
recover the catalog from one of those dumps after a VM loss.

The catalog is the *index* of every mesa-ducklake snapshot — losing
it means every Parquet history file in iRODS is unreachable until
you can answer "which snapshots belong to which project, in what
order?". So we back it up daily into the same iRODS zone that holds
the Parquet files: one durable store, one recovery procedure.

The Parquet snapshot files themselves are not backed up here. They
already live in iRODS under each project's ``/.mesa/ducklake/``
collection, and iRODS's own replication policy is the durability
story for that data plane.

## RPO and the deferred upgrade

* **Recovery Point Objective: 24 hours.** Worst case after a VM
  loss, you replay from the most recent daily dump, losing up to
  24h of catalog state.
* For sub-minute RPO we'd ship Postgres WAL segments to iRODS as
  they're generated (``archive_command = 'iput …'``) — that's
  deferred future work, intentionally out of scope for the current
  milestone (see ``help-me-come-up-refactored-brooks.md`` plan).

## What gets backed up

* ``pg_dump --format=custom --no-owner --compress=9`` of the
  ``mesa_ducklake`` database. Custom format lets ``pg_restore`` pick
  individual tables and replays parallel-restore-safe.
* Filename pattern: ``mesa-ducklake-YYYY-MM-DD.dump``.
* Upload target:
  ``/iplant/anvil/backup/mesa-ducklake/<hostname>/`` — namespaced per
  source host so multiple deployments don't collide.

## What does NOT get backed up

* Parquet snapshot files (already in iRODS).
* The ``mesa.schema_versions`` table — that's recovered automatically
  by ``pg_restore``.
* The local Parquet cache directory (rebuildable from iRODS).
* iRODS credentials, postgres credentials, mesa-mcp secrets — those
  belong in your secret-management story, not here.

## Install

The two systemd units in ``deploy/`` are the operator-facing
artifacts:

* ``deploy/mesa-ducklake-backup.service`` — one-shot unit that runs
  ``deploy/backup-pg.sh``.
* ``deploy/mesa-ducklake-backup.timer`` — fires daily at 03:00 UTC.

Standard install (matches the mesa-mcp.service layout):

```bash
sudo cp deploy/mesa-ducklake-backup.service /etc/systemd/system/
sudo cp deploy/mesa-ducklake-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mesa-ducklake-backup.timer
```

Verify the timer is queued:

```bash
systemctl list-timers mesa-ducklake-backup.timer
```

Force an immediate run (useful for smoke testing the iRODS pipeline
right after install):

```bash
sudo systemctl start mesa-ducklake-backup.service
journalctl -u mesa-ducklake-backup -f
```

## Environment knobs

``backup-pg.sh`` accepts overrides via environment. Set them via a
systemd drop-in (``sudo systemctl edit mesa-ducklake-backup.service``):

| Variable           | Default                                                 |
| ------------------ | ------------------------------------------------------- |
| ``PG_HOST``        | ``127.0.0.1``                                           |
| ``PG_PORT``        | ``5432``                                                |
| ``PG_DB``          | ``mesa_ducklake``                                       |
| ``PG_USER``        | ``mesa``                                                |
| ``PGPASSWORD_FILE``| ``/etc/mesa-mcp/secrets/postgres_password``             |
| ``IRODS_BACKUP_DIR``| ``/iplant/anvil/backup/mesa-ducklake/<hostname>``     |
| ``RETENTION_DAYS`` | ``30`` (set ``0`` to keep dumps forever)                |

## Verifying a dump

```bash
iget /iplant/anvil/backup/mesa-ducklake/<host>/mesa-ducklake-YYYY-MM-DD.dump /tmp/test.dump
pg_restore --list /tmp/test.dump | head
```

If ``pg_restore --list`` prints the table-of-contents cleanly, the
dump is intact.

## Recovery procedure

When the deploy VM has been lost (or the catalog database itself is
corrupted) and you need to rebuild from the most recent dump:

1. **Stop mesa-mcp** so no new writes land while you restore.

   ```bash
   sudo systemctl stop mesa-mcp.service
   ```

2. **Fetch the latest dump from iRODS.**

   ```bash
   LATEST=$(ils /iplant/anvil/backup/mesa-ducklake/<host>/ \
       | awk '/mesa-ducklake-/ {print $NF}' | sort | tail -1)
   iget /iplant/anvil/backup/mesa-ducklake/<host>/${LATEST} /tmp/restore.dump
   ```

3. **(Re-)create an empty database.** Use a *fresh* database name so
   you can compare the restored state against any partial recovery of
   the old one before swapping.

   ```bash
   sudo -u postgres createdb -O mesa mesa_ducklake_restored
   ```

4. **Restore.** ``--clean`` is unnecessary against an empty target;
   ``--no-owner`` matches the dump option so the role doesn't have to
   exist by name.

   ```bash
   PGPASSWORD=$(sudo cat /etc/mesa-mcp/secrets/postgres_password) \
       pg_restore \
           --host=127.0.0.1 \
           --port=5432 \
           --username=mesa \
           --no-owner \
           --dbname=mesa_ducklake_restored \
           /tmp/restore.dump
   ```

5. **Swap.** Either point mesa-mcp's DSN at the restored database, or
   drop the old database and rename the restored one to take its place:

   ```bash
   sudo -u postgres dropdb mesa_ducklake
   sudo -u postgres psql -c "ALTER DATABASE mesa_ducklake_restored RENAME TO mesa_ducklake"
   ```

6. **Drain the WAL.** Any ``mesa.pending_pushes`` rows that survived in
   the dump represent in-flight pushes from before the failure. Drain
   them before resuming traffic:

   ```bash
   MESA_DUCKLAKE_DSN="postgresql://mesa:$(sudo cat /etc/mesa-mcp/secrets/postgres_password)@127.0.0.1:5432/mesa_ducklake" \
   /home/exouser/mesa-ducklake/.venv/bin/mesa-ducklake recover
   ```

7. **Restart mesa-mcp.**

   ```bash
   sudo systemctl start mesa-mcp.service
   journalctl -u mesa-mcp -f
   ```

8. **Sanity check** by listing a project's snapshots from the
   restored catalog and confirming the count matches what iRODS shows
   for that project's ``.mesa/ducklake/`` collection.

## What can still go wrong after recovery

* **Catalog row for a snapshot the dump didn't capture.** If a write
  landed in iRODS during the 24h before the dump, the Parquet exists
  in iRODS but no catalog row points at it. ``mesa-ducklake fsck``
  (planned for a follow-up milestone) will reconcile by listing
  ``.mesa/ducklake/`` and matching against ``mesa.snapshots``.
* **Catalog row without a Parquet.** The reverse — a row pointing at
  a Parquet that was never pushed. ``recover_pending_pushes`` handles
  the case where the WAL row also survived; otherwise the snapshot's
  ``parquet_file`` stays a real filename but the iRODS object is
  missing. Reads will surface a clear DuckDB error in that case;
  again, ``fsck`` is the long-term cleanup.

## See also

- ``deploy/backup-pg.sh`` — the script the timer runs.
- ``docs/dev/architecture.md`` — why the catalog is the index of truth.
- ``docs/user/cli.md`` — the ``mesa-ducklake recover`` command.
