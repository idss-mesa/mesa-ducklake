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

from mesa_ducklake.irods_path import ducklake_subpath
from mesa_ducklake.models import Project, Snapshot


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


class CatalogStore:
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

    def latest_snapshot_id(self, project_id: UUID) -> int | None:
        """Return the snapshot id with the largest ``snapshot_id`` for a project."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_id
                FROM mesa.snapshots
                WHERE project_id = %s
                ORDER BY snapshot_id DESC
                LIMIT 1
                """,
                (project_id,),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def list_snapshots(self, project_id: UUID, limit: int = 100) -> list[Snapshot]:
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                       note, parquet_file
                FROM mesa.snapshots
                WHERE project_id = %s
                ORDER BY snapshot_id DESC
                LIMIT %s
                """,
                (project_id, limit),
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

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._owns_conn and not self._conn.closed:
            self._conn.close()
