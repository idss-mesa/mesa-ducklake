"""Read back what landed in DuckLake, and compare it with iRODS.

The checker opens its **own** ``DuckLakeClient`` with a fresh, empty cache
directory and the live iRODS session. Every Parquet file it reads is
therefore pulled from ``<project>/.mesa/ducklake/`` in iRODS — so a
passing check also proves the push-before-commit sidecar put the history
next to the data, not just in mesa-mcp's local cache.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mesa_ducklake import AvuChange, DuckLakeClient


@dataclass
class LakeView:
    client: DuckLakeClient
    session: Any
    project_id: UUID | None

    def snapshot_count(self) -> int:
        if self.project_id is None:
            return 0
        return len(self.client.list_snapshots(self.project_id, limit=10_000))

    def latest_snapshot_id(self) -> int | None:
        if self.project_id is None:
            return None
        snaps = self.client.list_snapshots(self.project_id, limit=10_000)
        return max((s.snapshot_id for s in snaps), default=None)

    def avus(self, path: str) -> set[tuple[str, str, str]]:
        if self.project_id is None:
            return set()
        rows = self.client.get_avus(self.project_id, path, session=self.session)
        return {(r.attribute, r.value, r.unit) for r in rows}

    def avus_as_of(self, path: str, ts: Any) -> set[tuple[str, str, str]]:
        if self.project_id is None:
            return set()
        rows = self.client.get_avus_as_of(self.project_id, path, ts, session=self.session)
        return {(r.attribute, r.value, r.unit) for r in rows}

    def history(self, path: str) -> list[AvuChange]:
        if self.project_id is None:
            return []
        return self.client.get_history(self.project_id, path, limit=10_000, session=self.session)

    def diff(self, from_snapshot: int, to_snapshot: int) -> list[AvuChange]:
        assert self.project_id is not None
        return self.client.diff(self.project_id, from_snapshot, to_snapshot, session=self.session)


@contextmanager
def open_lake(catalog_dsn: str, session: Any, project_root: str) -> Iterator[LakeView]:
    with tempfile.TemporaryDirectory(prefix="mesa-e2e-check-") as cache:
        client = DuckLakeClient(catalog_dsn=catalog_dsn, irods_session=session, cache_dir=cache)
        try:
            project = client.find_project_by_path(project_root)
            yield LakeView(client, session, project.project_id if project else None)
        finally:
            client.close()


def provenance_problems(
    rows: list[AvuChange], *, actor: str, source: str, via_ticket: str | None = None
) -> list[str]:
    """Human-readable provenance mismatches (empty list means all good)."""
    problems = []
    for r in rows:
        if r.actor != actor:
            problems.append(f"{r.attribute}: actor={r.actor!r}, expected {actor!r}")
        if r.source != source:
            problems.append(f"{r.attribute}: source={r.source!r}, expected {source!r}")
        if via_ticket is not None and r.via_ticket != via_ticket:
            problems.append(f"{r.attribute}: via_ticket={r.via_ticket!r}, expected {via_ticket!r}")
    return problems
