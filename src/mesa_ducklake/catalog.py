"""Postgres catalog operations — internal.

This module owns reads/writes against the ``mesa.projects`` and
``mesa.snapshots`` tables. It is **internal** and not part of the
public API; consumers should go through
:class:`mesa_ducklake.DuckLakeClient`.

All operations use parameterized SQL — user input is **never**
interpolated into a query string. Returns Pydantic models from
:mod:`mesa_ducklake.models`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from mesa_ducklake.catalog_base import CatalogStore
from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore
from mesa_ducklake.irods_path import ducklake_subpath
from mesa_ducklake.models import PARQUET_FILE_PENDING, PendingPush, Project, Snapshot


def _row_to_project(row: dict[str, Any]) -> Project:
    return Project(
        project_id=row["project_id"],
        irods_path=row["irods_path"],
        irods_zone=row["irods_zone"],
        ducklake_path=row["ducklake_path"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        status=row["status"],
    )


def _row_to_snapshot(row: dict[str, Any]) -> Snapshot:
    return Snapshot(
        snapshot_id=row["snapshot_id"],
        project_id=row["project_id"],
        ts=row["ts"],
        actor=row["actor"],
        parent_snapshot=row["parent_snapshot"],
        note=row["note"],
        parquet_file=row["parquet_file"],
    )


def _row_to_pending_push(row: dict[str, Any]) -> PendingPush:
    return PendingPush(
        snapshot_id=row["snapshot_id"],
        local_path=row["local_path"],
        irods_target=row["irods_target"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


class PostgresCatalogStore:
    """Internal CRUD layer over the Postgres ``mesa`` schema.

    Parameters
    ----------
    conn:
        Either a libpq DSN string or an existing
        :class:`psycopg.Connection`. When a DSN is supplied the store
        owns the connection and will close it via :meth:`close`. When
        an existing connection is passed in, the store does **not**
        close it.
    """

    def __init__(self, conn: str | psycopg.Connection) -> None:
        if isinstance(conn, str):
            self._conn = psycopg.connect(conn)
            self._owns_conn = True
            # autocommit=True means each ``cur.execute`` commits on
            # success and rolls back on error. Without it, a single
            # failing query (e.g. a UNIQUE violation on duplicate
            # project paths) leaves psycopg in InFailedSqlTransaction
            # state until the caller explicitly rolls back. We never
            # run multi-statement units of work in this class, so
            # autocommit is the right default — but only when we own
            # the connection (don't mutate a caller-supplied one).
            self._conn.autocommit = True
        else:
            self._conn = conn
            self._owns_conn = False

    # ------------------------------------------------------------------ projects

    def register_project(
        self,
        irods_path: str,
        irods_zone: str,
        ducklake_path: str | None,
        created_by: str,
    ) -> Project:
        """Insert a new row in ``mesa.projects`` and return it.

        ``ducklake_path`` defaults to ``<irods_path>/.mesa/ducklake``
        when ``None``.
        """
        path = ducklake_path or ducklake_subpath(irods_path)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO mesa.projects (irods_path, irods_zone, ducklake_path, created_by)
                VALUES (%s, %s, %s, %s)
                RETURNING project_id, irods_path, irods_zone, ducklake_path,
                          created_at, created_by, status
                """,
                (irods_path, irods_zone, path, created_by),
            )
            row = cur.fetchone()
        assert row is not None  # RETURNING always emits a row on success
        return _row_to_project(row)

    def get_project(self, project_id: UUID) -> Project | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT project_id, irods_path, irods_zone, ducklake_path,
                       created_at, created_by, status
                FROM mesa.projects
                WHERE project_id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
        return _row_to_project(row) if row else None

    def find_project_by_path(self, irods_path: str) -> Project | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT project_id, irods_path, irods_zone, ducklake_path,
                       created_at, created_by, status
                FROM mesa.projects
                WHERE irods_path = %s
                """,
                (irods_path,),
            )
            row = cur.fetchone()
        return _row_to_project(row) if row else None

    # ------------------------------------------------------------------ snapshots

    def create_snapshot(
        self,
        project_id: UUID,
        actor: str,
        parent_snapshot: int | None,
        note: str | None,
        parquet_file: str,
    ) -> Snapshot:
        """Insert a row in ``mesa.snapshots`` and return it.

        The ``parquet_file`` column is required by the schema; pass a
        placeholder if you intend to overwrite it via
        :meth:`update_snapshot_parquet_file` after the Parquet write
        succeeds.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO mesa.snapshots
                    (project_id, actor, parent_snapshot, note, parquet_file)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING snapshot_id, project_id, ts, actor, parent_snapshot,
                          note, parquet_file
                """,
                (project_id, actor, parent_snapshot, note, parquet_file),
            )
            row = cur.fetchone()
        assert row is not None
        return _row_to_snapshot(row)

    def update_snapshot_parquet_file(self, snapshot_id: int, parquet_file: str) -> Snapshot:
        """Update the ``parquet_file`` column on an existing snapshot row.

        This is the **only** sanctioned UPDATE against ``mesa.snapshots``
        — it exists because ``record_changes`` first allocates the
        snapshot row (so it has a stable id to embed in the Parquet
        rows), then writes the Parquet file, then stamps the final
        filename here. Everything else about the snapshot row is
        immutable; the append-only contract refers to AVU rows and
        snapshot existence, not to this single placeholder field.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE mesa.snapshots
                SET parquet_file = %s
                WHERE snapshot_id = %s
                RETURNING snapshot_id, project_id, ts, actor, parent_snapshot,
                          note, parquet_file
                """,
                (parquet_file, snapshot_id),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError(f"snapshot {snapshot_id} not found")
        return _row_to_snapshot(row)

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Remove a snapshot row.

        Used only when the lake write fails after a snapshot row was
        allocated — the catalog index would otherwise dangle. Callers
        in normal flow never call this; the append-only contract for
        AVU rows is preserved because the Parquet file the deleted
        snapshot would have referenced never came into existence.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mesa.snapshots WHERE snapshot_id = %s",
                (snapshot_id,),
            )

    def latest_snapshot_id(
        self,
        project_id: UUID,
        *,
        include_pending: bool = False,
    ) -> int | None:
        """Return the largest ``snapshot_id`` for a project.

        By default skips rows whose ``parquet_file`` is the
        ``PARQUET_FILE_PENDING`` sentinel — those snapshots are
        in-flight and not yet durable. Callers building a parent
        pointer for a new snapshot want the latest *committed* one,
        which is the default. Set ``include_pending=True`` for
        recovery / debug queries that need to see uncommitted rows.
        """
        clause = "" if include_pending else "AND parquet_file <> %s"
        params: tuple[Any, ...] = (project_id,)
        if not include_pending:
            params = (project_id, PARQUET_FILE_PENDING)
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT snapshot_id
                FROM mesa.snapshots
                WHERE project_id = %s {clause}
                ORDER BY snapshot_id DESC
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
        return row[0] if row else None

    def list_snapshots(
        self,
        project_id: UUID,
        limit: int = 100,
        *,
        include_pending: bool = False,
    ) -> list[Snapshot]:
        """List a project's snapshots, newest first.

        By default omits pending (in-flight) snapshots — see
        :data:`mesa_ducklake.models.PARQUET_FILE_PENDING`. Pass
        ``include_pending=True`` for recovery scans.
        """
        clause = "" if include_pending else "AND parquet_file <> %s"
        params: tuple[Any, ...] = (project_id, limit)
        if not include_pending:
            params = (project_id, PARQUET_FILE_PENDING, limit)
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                       note, parquet_file
                FROM mesa.snapshots
                WHERE project_id = %s {clause}
                ORDER BY snapshot_id DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [_row_to_snapshot(r) for r in rows]

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                       note, parquet_file
                FROM mesa.snapshots
                WHERE snapshot_id = %s
                """,
                (snapshot_id,),
            )
            row = cur.fetchone()
        return _row_to_snapshot(row) if row else None

    def snapshot_ts(self, snapshot_id: int) -> datetime | None:
        """Return a snapshot's ``ts`` column or ``None`` if not found."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT ts FROM mesa.snapshots WHERE snapshot_id = %s",
                (snapshot_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ pending pushes

    def insert_pending_push(
        self,
        snapshot_id: int,
        local_path: str,
        irods_target: str,
    ) -> PendingPush:
        """Insert a write-ahead row tracking an in-flight Parquet push.

        Called by :class:`DuckLakeClient` just before it asks
        :mod:`irods_sync` to push the Parquet file. The row survives
        process crashes and gets drained by the recovery task on the
        next start.

        Idempotent on conflict: an existing row for the same
        ``snapshot_id`` is left intact (we trust the queue, not the
        new caller) and the existing row is returned. This makes the
        write path safe to retry.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO mesa.pending_pushes
                    (snapshot_id, local_path, irods_target)
                VALUES (%s, %s, %s)
                ON CONFLICT (snapshot_id) DO NOTHING
                RETURNING snapshot_id, local_path, irods_target,
                          attempts, last_error, created_at
                """,
                (snapshot_id, local_path, irods_target),
            )
            row = cur.fetchone()
            if row is None:
                # Pre-existing row; return it as-is.
                cur.execute(
                    """
                    SELECT snapshot_id, local_path, irods_target,
                           attempts, last_error, created_at
                    FROM mesa.pending_pushes
                    WHERE snapshot_id = %s
                    """,
                    (snapshot_id,),
                )
                row = cur.fetchone()
        assert row is not None  # snapshot_id must exist after INSERT-or-SELECT
        return _row_to_pending_push(row)

    def delete_pending_push(self, snapshot_id: int) -> None:
        """Drop a pending-push row. Called after the catalog commit succeeds."""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mesa.pending_pushes WHERE snapshot_id = %s",
                (snapshot_id,),
            )

    def bump_pending_push_attempt(
        self,
        snapshot_id: int,
        error: str,
        *,
        error_max_chars: int = 2000,
    ) -> PendingPush | None:
        """Record one more failed push attempt against a pending row.

        Returns the updated :class:`PendingPush`, or ``None`` if the
        row no longer exists (e.g. another process drained the queue
        between our last read and this update).
        """
        truncated = error[:error_max_chars] if error else error
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE mesa.pending_pushes
                SET attempts   = attempts + 1,
                    last_error = %s
                WHERE snapshot_id = %s
                RETURNING snapshot_id, local_path, irods_target,
                          attempts, last_error, created_at
                """,
                (truncated, snapshot_id),
            )
            row = cur.fetchone()
        return _row_to_pending_push(row) if row else None

    def list_pending_pushes(self, limit: int = 100) -> list[PendingPush]:
        """List pending pushes, oldest first (drain order)."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT snapshot_id, local_path, irods_target,
                       attempts, last_error, created_at
                FROM mesa.pending_pushes
                ORDER BY created_at ASC, snapshot_id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [_row_to_pending_push(r) for r in rows]

    def get_pending_push(self, snapshot_id: int) -> PendingPush | None:
        """Return a single pending-push row by snapshot id, or ``None``."""
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT snapshot_id, local_path, irods_target,
                       attempts, last_error, created_at
                FROM mesa.pending_pushes
                WHERE snapshot_id = %s
                """,
                (snapshot_id,),
            )
            row = cur.fetchone()
        return _row_to_pending_push(row) if row else None

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._owns_conn and not self._conn.closed:
            self._conn.close()


def _duckdb_uri_to_path(uri: str) -> str:
    """Map a ``duckdb://`` URI to a filesystem path (or ``:memory:``)."""
    rest = uri[len("duckdb://"):]
    if rest in (":memory:", "/:memory:"):
        return ":memory:"
    return rest  # 'duckdb:///abs/p.duckdb' -> '/abs/p.duckdb'


def open_catalog(dsn: str) -> CatalogStore:
    """Construct the catalog backend implied by ``dsn``.

    * ``postgresql://`` / ``postgres://`` (or a libpq keyword DSN) ->
      :class:`PostgresCatalogStore`
    * ``duckdb://…`` URI, a path ending ``.duckdb``, or ``:memory:`` ->
      :class:`DuckDBCatalogStore`
    * blank / unrecognized -> ``ValueError``

    Callers that treat a blank DSN as "DuckLake disabled" must gate on that
    before calling — this factory raises on blank.
    """
    if dsn is None or not str(dsn).strip():
        raise ValueError("open_catalog requires a non-empty catalog DSN")
    s = str(dsn).strip()
    if s.startswith(("postgresql://", "postgres://")) or (
        "://" not in s and ("dbname=" in s or "host=" in s)
    ):
        return PostgresCatalogStore(s)
    if s.startswith("duckdb://"):
        return DuckDBCatalogStore(_duckdb_uri_to_path(s))
    if s == ":memory:" or s.endswith(".duckdb"):
        return DuckDBCatalogStore(s)
    raise ValueError(
        f"unrecognized catalog DSN (expected postgresql:// or duckdb://…/*.duckdb): {dsn!r}"
    )
