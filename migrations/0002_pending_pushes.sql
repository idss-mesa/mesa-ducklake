-- 0002_pending_pushes.sql
--
-- Purpose: add a write-ahead log of in-flight Parquet pushes from local
-- cache to iRODS. Crash-recovery layer for the new "push-before-commit"
-- write flow added by PR 2.
--
-- Why a separate table (not a column on mesa.snapshots): a pending push
-- is a *retry queue* with its own lifecycle (attempts, last_error) that
-- doesn't fit on the immutable snapshot record. After a push commits,
-- the row here is deleted; the snapshot record's `parquet_file` is
-- flipped from the sentinel literal 'pending' to the real filename via
-- the existing `update_snapshot_parquet_file` path. That UPDATE is the
-- atomic commit point.
--
-- One row per in-flight snapshot push. Re-pushing the same snapshot
-- after a crash is safe: `python-irodsclient.data_objects.put(force=True)`
-- + deterministic Parquet output (`ORDER BY` clause in lake.py) make
-- the operation idempotent.
--
-- Schema changes are append-only; never edit this file once committed.

BEGIN;

CREATE TABLE IF NOT EXISTS mesa.pending_pushes (
    snapshot_id    BIGINT PRIMARY KEY
                          REFERENCES mesa.snapshots(snapshot_id)
                          ON DELETE CASCADE,
    local_path     TEXT NOT NULL,         -- absolute path in the local cache
    irods_target   TEXT NOT NULL,         -- absolute iRODS path under <project>/.mesa/ducklake/
    attempts       INT NOT NULL DEFAULT 0,
    last_error     TEXT,                  -- truncated stringified exception from the most recent failure
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Recovery scans run oldest-first so the queue drains FIFO.
CREATE INDEX IF NOT EXISTS pending_pushes_created_at_idx
    ON mesa.pending_pushes (created_at);

COMMIT;
