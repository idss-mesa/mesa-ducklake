"""The ``DuckLakeClient`` public facade.

This is the **only** public entry point of mesa-ducklake. All other
modules (``catalog``, ``lake``, ``queries``, ``time_travel``,
``schema``, ``irods_sync``, ``cache``) are internal and may be
reorganized without notice.

The client composes a catalog backend (Postgres or DuckDB, selected by
the ``catalog_dsn`` scheme) with one or more :class:`LakeStore` instances
(one DuckDB/Parquet directory per project) and a
:mod:`mesa_ducklake.irods_sync` sidecar that replicates Parquet
files into each project's iRODS ``/.mesa/ducklake/`` collection.

Write protocol — see :mod:`mesa_ducklake.irods_sync` for the full
flow rationale. Briefly: create a snapshot row in 'pending' state,
insert a WAL row, write Parquet locally, push to iRODS, flip the
catalog row to its real filename (commit point), delete the WAL row.

Read protocol: before opening a DuckDB read connection, pull any
missing Parquet files from iRODS into the local cache.

Cache: per-project Parquet files materialize under
``<cache_dir>/<project_id>/``. ``cache_dir`` defaults to
``platformdirs.user_cache_dir("mesa-ducklake")`` — honors
``XDG_CACHE_HOME`` on Linux, systemd ``CacheDirectory=`` semantics
under a service unit, and ``~/Library/Caches/`` on macOS. The cache
is bounded by ``cache_cap_bytes`` (default 1 GiB) with LRU-by-mtime
eviction at the end of every successful ``record_changes``.

Local-only mode (``irods_session=None`` *and* no per-call session
passed): the iRODS push is skipped entirely and writes follow the
pre-sync rollback shape (delete the snapshot row on lake-write
failure). Useful for local development without an iRODS server.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

from platformdirs import user_cache_dir

from mesa_ducklake import cache, irods_sync
from mesa_ducklake.catalog import open_catalog
from mesa_ducklake.catalog_base import CatalogStore
from mesa_ducklake.irods_path import ducklake_subpath
from mesa_ducklake.lake import LakeStore
from mesa_ducklake.models import PARQUET_FILE_PENDING, AvuChange, Project, Snapshot
from mesa_ducklake.time_travel import parse_as_of

logger = logging.getLogger(__name__)

# Default per-process cache cap. 1 GiB is enough for ~1k mid-sized
# snapshots without ever evicting; operators can raise or disable
# (``cache_cap_bytes=0`` means "unbounded").
DEFAULT_CACHE_CAP_BYTES = 1 << 30  # 1 GiB

# How many catalog snapshots we consult when populating the cache
# before a read. A project with more than this many committed
# snapshots will need a compaction pass (planned for a later
# milestone); until then we just cap to keep the listing cheap.
_ENSURE_CACHED_LIMIT = 1000


def _default_cache_root() -> Path:
    """Return ``platformdirs.user_cache_dir('mesa-ducklake')`` as a Path."""
    return Path(user_cache_dir("mesa-ducklake"))


class DuckLakeClient:
    """Facade over the Postgres catalog, per-project DuckLake, and iRODS sync.

    Parameters
    ----------
    catalog_dsn:
        DSN for the catalog backend. Use ``postgresql://…`` for a Postgres
        catalog or ``duckdb:///path/to/file.duckdb`` for a local DuckDB
        catalog. The backend is selected automatically by
        :func:`mesa_ducklake.catalog.open_catalog`.
    postgres_dsn:
        Deprecated alias for ``catalog_dsn``, kept for back-compat.
        When both are supplied, ``catalog_dsn`` wins. Prefer ``catalog_dsn``
        for new code.
    irods_session:
        Authenticated ``python-irodsclient`` session, or ``None`` for
        local-only mode (iRODS push/pull skipped). Typed as ``Any``
        to avoid a hard import of ``irods.session``. Callers may
        also pass per-call sessions to :meth:`record_changes` and
        the read methods — the per-call value wins.
    cache_dir:
        Override for the local Parquet cache root. Defaults to
        ``platformdirs.user_cache_dir('mesa-ducklake')``. Per-project
        subdirectories live at ``<cache_dir>/<project_id>/``.
    cache_cap_bytes:
        Soft cap on the total bytes held under ``cache_dir``. After
        every successful commit, files are evicted oldest-first until
        the total is below the cap. ``0`` disables eviction.
    lake_root_override:
        Deprecated kwarg kept for back-compat with existing tests:
        when supplied, it acts as ``cache_dir``. Prefer ``cache_dir``
        for new code.
    """

    def __init__(
        self,
        catalog_dsn: str | None = None,
        irods_session: Any = None,
        *,
        postgres_dsn: str | None = None,
        cache_dir: str | Path | None = None,
        cache_cap_bytes: int = DEFAULT_CACHE_CAP_BYTES,
        lake_root_override: str | Path | None = None,
        data_collection: str | None = None,
    ) -> None:
        dsn = catalog_dsn if catalog_dsn is not None else postgres_dsn
        if dsn is None:
            raise TypeError(
                "DuckLakeClient requires catalog_dsn (or the legacy postgres_dsn alias)"
            )
        self._catalog_dsn = dsn
        self._postgres_dsn = dsn  # back-compat attribute for any external readers
        self._irods_session = irods_session
        # Sub-collection under each project root that holds the Parquet
        # files. Applied at registration time only: an existing project
        # keeps the ducklake_path recorded in its catalog row, so changing
        # this does not orphan data already written.
        self._data_collection = data_collection

        # Back-compat: lake_root_override is the historical name; new
        # name is cache_dir. Either works; lake_root_override wins
        # when both are supplied so old tests keep behaving.
        if lake_root_override is not None:
            self._cache_root = Path(lake_root_override)
        elif cache_dir is not None:
            self._cache_root = Path(cache_dir)
        else:
            self._cache_root = _default_cache_root()
        self._cache_cap_bytes = cache_cap_bytes

        # Lazily-constructed so smoke tests that only check construction
        # don't need a live Postgres.
        self._catalog: CatalogStore | None = None
        # One LakeStore per project_id.
        self._lakes: dict[UUID, LakeStore] = {}

    # ------------------------------------------------------------------ internal helpers

    def _get_catalog(self) -> CatalogStore:
        if self._catalog is None:
            self._catalog = open_catalog(self._catalog_dsn)
        return self._catalog

    def _resolve_lake_root(self, project: Project) -> Path:
        """Return the local cache directory for this project's Parquet files.

        The directory is ``<cache_root>/<project_id>/``. The catalog's
        ``project.ducklake_path`` is the *iRODS* location and is **not**
        used as a local filesystem path — that was the v0.1 stopgap; the
        sidecar pattern in :mod:`mesa_ducklake.irods_sync` makes this
        proper.
        """
        return self._cache_root / str(project.project_id)

    def _get_lake(self, project: Project) -> LakeStore:
        lake = self._lakes.get(project.project_id)
        if lake is None:
            lake = LakeStore(self._resolve_lake_root(project))
            self._lakes[project.project_id] = lake
        return lake

    def _effective_session(self, session: Any | None) -> Any | None:
        """Per-call session wins; fall back to the constructor's session."""
        return session if session is not None else self._irods_session

    def _ensure_cached(self, project: Project, session: Any) -> None:
        """Populate the local cache with every committed snapshot's Parquet.

        Bounded by ``_ENSURE_CACHED_LIMIT`` snapshots — past that the
        project should be compacted (future work). Pending/failed
        sentinels are skipped by :func:`irods_sync.ensure_cached`.
        """
        catalog = self._get_catalog()
        # ``list_snapshots`` already filters pending by default.
        expected = [
            s.parquet_file
            for s in catalog.list_snapshots(
                project.project_id, limit=_ENSURE_CACHED_LIMIT
            )
        ]
        if not expected:
            return
        try:
            irods_sync.ensure_cached(
                self._resolve_lake_root(project),
                project.ducklake_path,
                expected,
                session=session,
            )
        except irods_sync.iRODSSyncError as exc:
            # Surface the failure but don't crash — the DuckDB read
            # below will surface a clearer "missing file" error if a
            # required snapshot didn't get pulled.
            logger.warning(
                "ensure_cached.failed project=%s error=%s",
                project.project_id,
                exc,
            )

    # ------------------------------------------------------------------ project lifecycle

    def register_project(self, irods_path: str, actor: str, zone: str) -> Project:
        """Register a new MESA-enabled iRODS project in the catalog.

        The Parquet sub-collection is ``<irods_path>/.mesa/ducklake``
        unless the client was constructed with ``data_collection``. The
        resolved path is stored on the project row, so a later change to
        the setting leaves existing projects reading their original
        location rather than silently orphaning their files.
        """
        ducklake_path = (
            ducklake_subpath(irods_path, self._data_collection)
            if self._data_collection
            else None
        )
        return self._get_catalog().register_project(
            irods_path=irods_path,
            irods_zone=zone,
            ducklake_path=ducklake_path,
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
        *,
        session: Any | None = None,
    ) -> Snapshot:
        """Append a batch of AVU changes as one new snapshot.

        With an iRODS session in scope (``session=`` or constructor's
        ``irods_session``), the write goes through the sync sidecar:

        1. Allocate snapshot row with ``parquet_file='pending'``.
        2. Insert ``mesa.pending_pushes`` WAL row.
        3. ``LakeStore.write_changes`` produces the local Parquet.
        4. :func:`irods_sync.push` replicates to iRODS.
        5. Flip catalog row to the real filename — **commit point**.
        6. Drop the WAL row.

        Without a session (local-only mode), steps 2/4/6 are skipped
        and on lake-write failure the snapshot row is deleted (pre-sync
        rollback shape).

        Raises ``ValueError`` for an empty ``changes`` list.
        """
        if not changes:
            raise ValueError("record_changes requires at least one AvuChange")

        catalog = self._get_catalog()
        project = catalog.get_project(project_id)
        if project is None:
            raise KeyError(f"project {project_id} not found")

        # Parent pointer = latest *committed* snapshot. Pending rows
        # don't count — if they crash and never commit, our pointer
        # would dangle.
        parent = catalog.latest_snapshot_id(project_id)
        snapshot = catalog.create_snapshot(
            project_id=project_id,
            actor=actor,
            parent_snapshot=parent,
            note=note,
            parquet_file=PARQUET_FILE_PENDING,
        )

        stamped: list[AvuChange] = [
            change.model_copy(
                update={
                    "project_id": project_id,
                    "snapshot_id": snapshot.snapshot_id,
                }
            )
            for change in changes
        ]

        relative_name = f"snapshot_{snapshot.snapshot_id}.parquet"
        lake_root = self._resolve_lake_root(project)
        local_path = lake_root / relative_name
        ducklake_path = project.ducklake_path.rstrip("/")
        irods_target = f"{ducklake_path}/{relative_name}"

        push_session = self._effective_session(session)
        lake = self._get_lake(project)

        if push_session is None:
            # Local-only path: keep the old "delete on failure" shape.
            try:
                relative = lake.write_changes(
                    project_id=project_id,
                    snapshot_id=snapshot.snapshot_id,
                    changes=stamped,
                )
            except Exception:
                catalog.delete_snapshot(snapshot.snapshot_id)
                raise
            committed = catalog.update_snapshot_parquet_file(
                snapshot.snapshot_id, relative
            )
        else:
            # WAL + sync path. WAL row stays put on failure; recover
            # drains it.
            catalog.insert_pending_push(
                snapshot.snapshot_id,
                str(local_path),
                irods_target,
            )
            relative = lake.write_changes(
                project_id=project_id,
                snapshot_id=snapshot.snapshot_id,
                changes=stamped,
            )
            irods_sync.push(local_path, irods_target, session=push_session)
            committed = catalog.update_snapshot_parquet_file(
                snapshot.snapshot_id, relative
            )
            catalog.delete_pending_push(snapshot.snapshot_id)

        if self._cache_cap_bytes:
            cache.evict_if_over(self._cache_root, self._cache_cap_bytes)

        return committed

    def recover_pending_pushes(
        self,
        session: Any | None = None,
        *,
        max_attempts: int = irods_sync.DEFAULT_MAX_ATTEMPTS,
        limit: int = 100,
    ) -> dict[str, int]:
        """Drain the WAL — see :func:`irods_sync.recover_pending_pushes`.

        Returns the per-status counter dict. Raises ``RuntimeError``
        when no session is available; recovery cannot run against
        local-only mode by definition.
        """
        push_session = self._effective_session(session)
        if push_session is None:
            raise RuntimeError(
                "recover_pending_pushes requires an iRODS session "
                "(pass session= or supply one at construction)"
            )
        return irods_sync.recover_pending_pushes(
            self._get_catalog(),
            push_session,
            max_attempts=max_attempts,
            limit=limit,
        )

    # ------------------------------------------------------------------ reads

    def get_avus(
        self,
        project_id: UUID,
        irods_path: str,
        *,
        session: Any | None = None,
    ) -> list[AvuChange]:
        return self.get_avus_as_of(
            project_id, irods_path, datetime.now(tz=UTC), session=session
        )

    def get_avus_as_of(
        self,
        project_id: UUID,
        irods_path: str,
        ts: str | datetime,
        *,
        session: Any | None = None,
    ) -> list[AvuChange]:
        as_of = parse_as_of(ts)
        project = self.get_project(project_id)
        sess = self._effective_session(session)
        if sess is not None:
            self._ensure_cached(project, sess)
        lake = self._get_lake(project)
        return lake.read_effective_avus(project_id, irods_path, as_of)

    def get_history(
        self,
        project_id: UUID,
        irods_path: str,
        limit: int = 100,
        *,
        session: Any | None = None,
    ) -> list[AvuChange]:
        project = self.get_project(project_id)
        sess = self._effective_session(session)
        if sess is not None:
            self._ensure_cached(project, sess)
        lake = self._get_lake(project)
        return lake.read_history(project_id, irods_path, limit=limit)

    def list_snapshots(self, project_id: UUID, limit: int = 100) -> list[Snapshot]:
        return self._get_catalog().list_snapshots(project_id, limit=limit)

    def diff(
        self,
        project_id: UUID,
        from_snapshot: int,
        to_snapshot: int,
        *,
        session: Any | None = None,
    ) -> list[AvuChange]:
        project = self.get_project(project_id)
        sess = self._effective_session(session)
        if sess is not None:
            self._ensure_cached(project, sess)
        lake = self._get_lake(project)
        return lake.diff(project_id, from_snapshot, to_snapshot)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Close any underlying connections (Postgres)."""
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None
        # LakeStore objects hold no long-lived state.
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
