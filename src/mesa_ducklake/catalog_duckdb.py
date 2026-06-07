"""DuckDB-file catalog backend — internal.

A single local DuckDB database file standing in for the Postgres catalog,
for single-user / local-install deployments that don't want to run Postgres.
Implements the same surface as PostgresCatalogStore (see
:class:`mesa_ducklake.catalog_base.CatalogStore`).

Single-writer: one OS process holds the DuckDB file lock at a time. Use the
Postgres backend for concurrent/hosted multi-writer deployments.

``project_id`` is stored as TEXT (UUID string form) — matching how the
Parquet data plane stores it — to avoid UUID parameter-binding edge cases;
Pydantic coerces the string back to ``uuid.UUID`` on the way out.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import duckdb

from mesa_ducklake.irods_path import ducklake_subpath
from mesa_ducklake.models import PARQUET_FILE_PENDING, PendingPush, Project, Snapshot

# Idempotent schema bootstrap. FK constraints are intentionally omitted
# (DuckDB self-referencing FKs are limited); integrity is enforced by the
# DuckLakeClient write protocol exactly as it is for Postgres.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS mesa",
    "CREATE SEQUENCE IF NOT EXISTS mesa.snapshots_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS mesa.projects (
        project_id    TEXT PRIMARY KEY,
        irods_path    TEXT NOT NULL UNIQUE,
        irods_zone    TEXT NOT NULL,
        ducklake_path TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by    TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mesa.snapshots (
        snapshot_id     BIGINT PRIMARY KEY DEFAULT nextval('mesa.snapshots_seq'),
        project_id      TEXT NOT NULL,
        ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor           TEXT NOT NULL,
        parent_snapshot BIGINT,
        note            TEXT,
        parquet_file    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mesa.pending_pushes (
        snapshot_id  BIGINT PRIMARY KEY,
        local_path   TEXT NOT NULL,
        irods_target TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


def _to_dict(description: Sequence[Any], row: tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {col[0]: val for col, val in zip(description, row)}


def _row_to_project(d: dict[str, Any]) -> Project:
    return Project(
        project_id=d["project_id"],
        irods_path=d["irods_path"],
        irods_zone=d["irods_zone"],
        ducklake_path=d["ducklake_path"],
        created_at=d["created_at"],
        created_by=d["created_by"],
        status=d["status"],
    )


def _row_to_snapshot(d: dict[str, Any]) -> Snapshot:
    return Snapshot(
        snapshot_id=d["snapshot_id"],
        project_id=d["project_id"],
        ts=d["ts"],
        actor=d["actor"],
        parent_snapshot=d["parent_snapshot"],
        note=d["note"],
        parquet_file=d["parquet_file"],
    )


def _row_to_pending_push(d: dict[str, Any]) -> PendingPush:
    return PendingPush(
        snapshot_id=d["snapshot_id"],
        local_path=d["local_path"],
        irods_target=d["irods_target"],
        attempts=d["attempts"],
        last_error=d["last_error"],
        created_at=d["created_at"],
    )


class DuckDBCatalogStore:
    """Catalog backend over a single DuckDB file. See module docstring."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            resolved = Path(self._path).expanduser()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(resolved)
        self._conn = duckdb.connect(self._path)
        for stmt in _SCHEMA_STATEMENTS:
            self._conn.execute(stmt)

    # ------------------------------------------------------------------ helpers
    def _one(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        cur = self._conn.execute(sql, params)
        desc = cur.description
        return _to_dict(desc, cur.fetchone())

    def _all(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, params)
        desc = cur.description
        out = []
        for r in cur.fetchall():
            d = _to_dict(desc, r)
            if d is not None:
                out.append(d)
        return out

    # ------------------------------------------------------------------ projects
    def register_project(self, irods_path, irods_zone, ducklake_path, created_by) -> Project:
        path = ducklake_path or ducklake_subpath(irods_path)
        d = self._one(
            """
            INSERT INTO mesa.projects
                (project_id, irods_path, irods_zone, ducklake_path, created_by)
            VALUES (?, ?, ?, ?, ?)
            RETURNING project_id, irods_path, irods_zone, ducklake_path,
                      created_at, created_by, status
            """,
            [str(uuid4()), irods_path, irods_zone, path, created_by],
        )
        assert d is not None
        return _row_to_project(d)

    def get_project(self, project_id) -> Project | None:
        d = self._one(
            """SELECT project_id, irods_path, irods_zone, ducklake_path,
                      created_at, created_by, status
               FROM mesa.projects WHERE project_id = ?""",
            [str(project_id)],
        )
        return _row_to_project(d) if d else None

    def find_project_by_path(self, irods_path) -> Project | None:
        d = self._one(
            """SELECT project_id, irods_path, irods_zone, ducklake_path,
                      created_at, created_by, status
               FROM mesa.projects WHERE irods_path = ?""",
            [irods_path],
        )
        return _row_to_project(d) if d else None

    # ------------------------------------------------------------------ snapshots
    def create_snapshot(self, project_id, actor, parent_snapshot, note, parquet_file) -> Snapshot:
        d = self._one(
            """
            INSERT INTO mesa.snapshots
                (project_id, actor, parent_snapshot, note, parquet_file)
            VALUES (?, ?, ?, ?, ?)
            RETURNING snapshot_id, project_id, ts, actor, parent_snapshot,
                      note, parquet_file
            """,
            [str(project_id), actor, parent_snapshot, note, parquet_file],
        )
        assert d is not None
        return _row_to_snapshot(d)

    def update_snapshot_parquet_file(self, snapshot_id, parquet_file) -> Snapshot:
        d = self._one(
            """UPDATE mesa.snapshots SET parquet_file = ? WHERE snapshot_id = ?
               RETURNING snapshot_id, project_id, ts, actor, parent_snapshot,
                         note, parquet_file""",
            [parquet_file, snapshot_id],
        )
        if d is None:
            raise KeyError(f"snapshot {snapshot_id} not found")
        return _row_to_snapshot(d)

    def delete_snapshot(self, snapshot_id) -> None:
        self._conn.execute("DELETE FROM mesa.snapshots WHERE snapshot_id = ?", [snapshot_id])

    def latest_snapshot_id(self, project_id, *, include_pending=False) -> int | None:
        if include_pending:
            d = self._one(
                "SELECT snapshot_id FROM mesa.snapshots WHERE project_id = ? "
                "ORDER BY snapshot_id DESC LIMIT 1",
                [str(project_id)],
            )
        else:
            d = self._one(
                "SELECT snapshot_id FROM mesa.snapshots WHERE project_id = ? "
                "AND parquet_file <> ? ORDER BY snapshot_id DESC LIMIT 1",
                [str(project_id), PARQUET_FILE_PENDING],
            )
        return d["snapshot_id"] if d else None

    def list_snapshots(self, project_id, limit=100, *, include_pending=False) -> list[Snapshot]:
        if include_pending:
            rows = self._all(
                """SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                          note, parquet_file
                   FROM mesa.snapshots WHERE project_id = ?
                   ORDER BY snapshot_id DESC LIMIT ?""",
                [str(project_id), limit],
            )
        else:
            rows = self._all(
                """SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                          note, parquet_file
                   FROM mesa.snapshots WHERE project_id = ? AND parquet_file <> ?
                   ORDER BY snapshot_id DESC LIMIT ?""",
                [str(project_id), PARQUET_FILE_PENDING, limit],
            )
        return [_row_to_snapshot(d) for d in rows]

    def get_snapshot(self, snapshot_id) -> Snapshot | None:
        d = self._one(
            """SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                      note, parquet_file
               FROM mesa.snapshots WHERE snapshot_id = ?""",
            [snapshot_id],
        )
        return _row_to_snapshot(d) if d else None

    def snapshot_ts(self, snapshot_id) -> datetime | None:
        d = self._one("SELECT ts FROM mesa.snapshots WHERE snapshot_id = ?", [snapshot_id])
        return d["ts"] if d else None

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._conn.close()
