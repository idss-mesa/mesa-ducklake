"""CatalogStore — the catalog backend interface (internal).

Both ``PostgresCatalogStore`` (catalog.py) and ``DuckDBCatalogStore``
(catalog_duckdb.py) implement this surface. ``DuckLakeClient`` and
``irods_sync`` depend on this Protocol, not on a concrete driver.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from mesa_ducklake.models import PendingPush, Project, Snapshot


@runtime_checkable
class CatalogStore(Protocol):
    """Method surface used by DuckLakeClient + irods_sync."""

    # projects
    def register_project(
        self, irods_path: str, irods_zone: str,
        ducklake_path: str | None, created_by: str,
    ) -> Project: ...
    def get_project(self, project_id: UUID) -> Project | None: ...
    def find_project_by_path(self, irods_path: str) -> Project | None: ...

    # snapshots
    def create_snapshot(
        self, project_id: UUID, actor: str, parent_snapshot: int | None,
        note: str | None, parquet_file: str,
    ) -> Snapshot: ...
    def update_snapshot_parquet_file(self, snapshot_id: int, parquet_file: str) -> Snapshot: ...
    def delete_snapshot(self, snapshot_id: int) -> None: ...
    def latest_snapshot_id(
        self, project_id: UUID, *, include_pending: bool = False
    ) -> int | None: ...
    def list_snapshots(
        self, project_id: UUID, limit: int = 100, *, include_pending: bool = False
    ) -> list[Snapshot]: ...
    def get_snapshot(self, snapshot_id: int) -> Snapshot | None: ...
    def snapshot_ts(self, snapshot_id: int) -> datetime | None: ...

    # pending pushes
    def insert_pending_push(
        self, snapshot_id: int, local_path: str, irods_target: str
    ) -> PendingPush: ...
    def delete_pending_push(self, snapshot_id: int) -> None: ...
    def bump_pending_push_attempt(
        self, snapshot_id: int, error: str, *, error_max_chars: int = 2000
    ) -> PendingPush | None: ...
    def list_pending_pushes(self, limit: int = 100) -> list[PendingPush]: ...
    def get_pending_push(self, snapshot_id: int) -> PendingPush | None: ...

    # lifecycle
    def close(self) -> None: ...
