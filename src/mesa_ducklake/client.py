"""The ``DuckLakeClient`` public facade.

This is the **only** public entry point of mesa-ducklake. All other
modules (``catalog``, ``lake``, ``queries``, ``time_travel``,
``schema``) are internal and may be reorganized without notice.

The client composes a :class:`CatalogStore` (Postgres index of
projects + snapshots) with one or more :class:`LakeStore` instances
(one DuckDB/Parquet directory per project) to satisfy the methods
called by the sibling project ``mesa-mcp``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

from mesa_ducklake.catalog import CatalogStore
from mesa_ducklake.lake import LakeStore
from mesa_ducklake.models import AvuChange, Project, Snapshot
from mesa_ducklake.time_travel import parse_as_of


class DuckLakeClient:
    """Facade over the Postgres catalog + per-project DuckLake Parquet files.

    Parameters
    ----------
    postgres_dsn:
        Standard libpq DSN for the catalog database.
    irods_session:
        Authenticated ``python-irodsclient`` session. Currently used
        only as an opaque reference — the per-project lake roots
        resolve to local filesystem paths via ``lake_root_override``
        in tests, and to iRODS-mounted directories in production.
        Typed as ``Any`` to avoid a hard import of ``irods.session``.
    lake_root_override:
        Optional override for the lake root directory. When set, every
        project's lake lives at
        ``<lake_root_override>/<project_id>/``. This is what tests use
        with ``tmp_path``. In production, leave ``None`` and the
        client resolves to each project's ``ducklake_path`` (the
        in-iRODS ``/.mesa/ducklake/`` collection — wiring lands in a
        later PR that adds iRODS-backed ``LakeStorage``).
    """

    def __init__(
        self,
        postgres_dsn: str,
        irods_session: Any,
        lake_root_override: str | Path | None = None,
    ) -> None:
        self._postgres_dsn = postgres_dsn
        self._irods_session = irods_session
        self._lake_root_override = (
            Path(lake_root_override) if lake_root_override is not None else None
        )
        # Lazily-constructed so smoke tests that only check construction
        # don't need a live Postgres.
        self._catalog: CatalogStore | None = None
        # One LakeStore per project_id (one DuckDB/Parquet directory per
        # project, mirroring the "per-project DuckLake" architectural
        # decision in CLAUDE.md).
        self._lakes: dict[UUID, LakeStore] = {}

    # ------------------------------------------------------------------ internal helpers

    def _get_catalog(self) -> CatalogStore:
        if self._catalog is None:
            self._catalog = CatalogStore(self._postgres_dsn)
        return self._catalog

    def _resolve_lake_root(self, project: Project) -> Path:
        """Return the on-disk directory where this project's Parquet lives."""
        if self._lake_root_override is not None:
            return self._lake_root_override / str(project.project_id)
        # In production this would be the iRODS-mounted ducklake_path.
        # We don't have an iRODS LakeStorage backend yet (out of scope for
        # this PR — see lake.py), so we return the literal ducklake_path
        # and let the caller arrange for it to be a real filesystem path.
        return Path(project.ducklake_path)

    def _get_lake(self, project: Project) -> LakeStore:
        lake = self._lakes.get(project.project_id)
        if lake is None:
            lake = LakeStore(self._resolve_lake_root(project))
            self._lakes[project.project_id] = lake
        return lake

    # ------------------------------------------------------------------ project lifecycle

    def register_project(self, irods_path: str, actor: str, zone: str) -> Project:
        """Register a new MESA-enabled iRODS project in the catalog."""
        return self._get_catalog().register_project(
            irods_path=irods_path,
            irods_zone=zone,
            ducklake_path=None,  # default to <irods_path>/.mesa/ducklake
            created_by=actor,
        )

    def get_project(self, project_id: UUID) -> Project:
        """Look up a project by its UUID.

        Raises
        ------
        KeyError
            If no project with that id exists.
        """
        project = self._get_catalog().get_project(project_id)
        if project is None:
            raise KeyError(f"project {project_id} not found")
        return project

    def find_project_by_path(self, irods_path: str) -> Project | None:
        return self._get_catalog().find_project_by_path(irods_path)

    # ------------------------------------------------------------------ writes

    def record_changes(
        self,
        project_id: UUID,
        actor: str,
        changes: list[AvuChange],
        note: str | None = None,
    ) -> Snapshot:
        """Append a batch of AVU changes as one new snapshot.

        Steps:

        1. Allocate a snapshot row in ``mesa.snapshots`` with the most
           recent snapshot of this project as the parent. The
           ``parquet_file`` column is initially a placeholder (we don't
           know the real filename until we have the snapshot id).
        2. Stamp every ``AvuChange`` with ``project_id`` and
           ``snapshot_id``.
        3. Write all rows to one Parquet file via :class:`LakeStore`.
        4. Update the snapshot row with the real ``parquet_file``.
        5. On any lake-write failure, delete the snapshot row so the
           catalog doesn't dangle.

        Raises ``ValueError`` for an empty ``changes`` list (empty
        snapshots are not allowed — one snapshot is one user action).
        """
        if not changes:
            raise ValueError("record_changes requires at least one AvuChange")

        catalog = self._get_catalog()
        project = catalog.get_project(project_id)
        if project is None:
            raise KeyError(f"project {project_id} not found")

        parent = catalog.latest_snapshot_id(project_id)
        # Pre-allocate the snapshot row with a placeholder parquet_file
        # so the rows we write into the file carry the real snapshot_id.
        snapshot = catalog.create_snapshot(
            project_id=project_id,
            actor=actor,
            parent_snapshot=parent,
            note=note,
            parquet_file="pending",
        )

        # Stamp every change with the snapshot+project ids.
        stamped: list[AvuChange] = []
        for change in changes:
            # Pydantic v2: model_copy(update=...) returns a new instance.
            stamped.append(
                change.model_copy(
                    update={
                        "project_id": project_id,
                        "snapshot_id": snapshot.snapshot_id,
                    }
                )
            )

        lake = self._get_lake(project)
        try:
            relative = lake.write_changes(
                project_id=project_id,
                snapshot_id=snapshot.snapshot_id,
                changes=stamped,
            )
        except Exception:
            # Roll back the catalog row so we don't dangle.
            catalog.delete_snapshot(snapshot.snapshot_id)
            raise

        return catalog.update_snapshot_parquet_file(snapshot.snapshot_id, relative)

    # ------------------------------------------------------------------ reads

    def get_avus(self, project_id: UUID, irods_path: str) -> list[AvuChange]:
        return self.get_avus_as_of(project_id, irods_path, datetime.now(tz=UTC))

    def get_avus_as_of(
        self,
        project_id: UUID,
        irods_path: str,
        ts: str | datetime,
    ) -> list[AvuChange]:
        as_of = parse_as_of(ts)
        project = self.get_project(project_id)
        lake = self._get_lake(project)
        return lake.read_effective_avus(project_id, irods_path, as_of)

    def get_history(
        self,
        project_id: UUID,
        irods_path: str,
        limit: int = 100,
    ) -> list[AvuChange]:
        project = self.get_project(project_id)
        lake = self._get_lake(project)
        return lake.read_history(project_id, irods_path, limit=limit)

    def list_snapshots(self, project_id: UUID, limit: int = 100) -> list[Snapshot]:
        return self._get_catalog().list_snapshots(project_id, limit=limit)

    def diff(
        self,
        project_id: UUID,
        from_snapshot: int,
        to_snapshot: int,
    ) -> list[AvuChange]:
        project = self.get_project(project_id)
        lake = self._get_lake(project)
        return lake.diff(project_id, from_snapshot, to_snapshot)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close any underlying connections (Postgres)."""
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None
        # LakeStore objects hold no long-lived state — each DuckDB
        # connection is opened per operation and closed before return.
        self._lakes.clear()

    # ------------------------------------------------------------------ context manager

    def __enter__(self) -> DuckLakeClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
