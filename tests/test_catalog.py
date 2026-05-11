"""CatalogStore tests against an ephemeral Postgres."""

from __future__ import annotations

from uuid import uuid4

import psycopg.errors
import pytest

from mesa_ducklake.catalog import CatalogStore

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def catalog(catalog_db: str) -> CatalogStore:
    return CatalogStore(catalog_db)


def test_register_project_persists_row(catalog: CatalogStore) -> None:
    project = catalog.register_project(
        irods_path="/iplant/home/alice/myproj",
        irods_zone="iplant",
        ducklake_path=None,  # default to <irods_path>/.mesa/ducklake
        created_by="alice",
    )
    assert project.irods_path == "/iplant/home/alice/myproj"
    assert project.ducklake_path == "/iplant/home/alice/myproj/.mesa/ducklake"
    assert project.status == "active"
    assert project.created_by == "alice"
    assert project.project_id is not None


def test_get_project_round_trip(catalog: CatalogStore) -> None:
    registered = catalog.register_project(
        irods_path="/iplant/home/alice/proj1",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    fetched = catalog.get_project(registered.project_id)
    assert fetched is not None
    assert fetched.project_id == registered.project_id
    assert fetched.irods_path == registered.irods_path


def test_get_project_returns_none_for_missing(catalog: CatalogStore) -> None:
    assert catalog.get_project(uuid4()) is None


def test_find_project_by_path(catalog: CatalogStore) -> None:
    catalog.register_project(
        irods_path="/iplant/home/alice/proj2",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    found = catalog.find_project_by_path("/iplant/home/alice/proj2")
    assert found is not None
    assert found.irods_path == "/iplant/home/alice/proj2"
    assert catalog.find_project_by_path("/iplant/home/alice/never") is None


def test_register_project_rejects_duplicate_path(catalog: CatalogStore) -> None:
    """``irods_path`` is UNIQUE in the schema."""
    catalog.register_project(
        irods_path="/iplant/home/alice/dup",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        catalog.register_project(
            irods_path="/iplant/home/alice/dup",
            irods_zone="iplant",
            ducklake_path=None,
            created_by="alice",
        )


def test_create_snapshot_chain(catalog: CatalogStore) -> None:
    """Two snapshots in sequence are linked via ``parent_snapshot``."""
    project = catalog.register_project(
        irods_path="/iplant/home/alice/chain",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    s1 = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note="first",
        parquet_file="snapshot_1.parquet",
    )
    s2 = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=s1.snapshot_id,
        note="second",
        parquet_file="snapshot_2.parquet",
    )
    assert s2.parent_snapshot == s1.snapshot_id
    assert s2.snapshot_id > s1.snapshot_id

    listed = catalog.list_snapshots(project.project_id)
    assert [s.snapshot_id for s in listed] == [s2.snapshot_id, s1.snapshot_id]

    assert catalog.latest_snapshot_id(project.project_id) == s2.snapshot_id


def test_update_snapshot_parquet_file(catalog: CatalogStore) -> None:
    project = catalog.register_project(
        irods_path="/iplant/home/alice/upd",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file="pending",
    )
    updated = catalog.update_snapshot_parquet_file(
        snap.snapshot_id, "snapshot_99.parquet"
    )
    assert updated.parquet_file == "snapshot_99.parquet"

    fetched = catalog.get_snapshot(snap.snapshot_id)
    assert fetched is not None
    assert fetched.parquet_file == "snapshot_99.parquet"


def test_delete_snapshot_rolls_back_index_row(catalog: CatalogStore) -> None:
    project = catalog.register_project(
        irods_path="/iplant/home/alice/del",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file="pending",
    )
    catalog.delete_snapshot(snap.snapshot_id)
    assert catalog.get_snapshot(snap.snapshot_id) is None


# ---------------------------------------------------------------------- pending pushes


def _make_project(catalog: CatalogStore, *, slug: str):
    return catalog.register_project(
        irods_path=f"/iplant/home/alice/{slug}",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )


def _make_pending_snapshot(catalog: CatalogStore, project) -> int:
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file="pending",
    )
    return snap.snapshot_id


def test_insert_pending_push_round_trip(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="pending-rt")
    snap_id = _make_pending_snapshot(catalog, project)
    pending = catalog.insert_pending_push(
        snapshot_id=snap_id,
        local_path="/cache/snap.parquet",
        irods_target="/iplant/home/alice/pending-rt/.mesa/ducklake/snap.parquet",
    )
    assert pending.snapshot_id == snap_id
    assert pending.attempts == 0
    assert pending.last_error is None
    assert pending.local_path == "/cache/snap.parquet"

    fetched = catalog.get_pending_push(snap_id)
    assert fetched is not None
    assert fetched.snapshot_id == snap_id


def test_insert_pending_push_is_idempotent(catalog: CatalogStore) -> None:
    """ON CONFLICT DO NOTHING — re-inserting same snapshot leaves first row intact."""
    project = _make_project(catalog, slug="pending-idem")
    snap_id = _make_pending_snapshot(catalog, project)
    first = catalog.insert_pending_push(
        snapshot_id=snap_id, local_path="/cache/a.parquet", irods_target="/i/a.parquet"
    )
    second = catalog.insert_pending_push(
        snapshot_id=snap_id, local_path="/cache/B.parquet", irods_target="/i/B.parquet"
    )
    assert second.local_path == first.local_path == "/cache/a.parquet"
    assert second.attempts == 0


def test_delete_pending_push_removes_row(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="pending-del")
    snap_id = _make_pending_snapshot(catalog, project)
    catalog.insert_pending_push(
        snapshot_id=snap_id, local_path="/cache/d.parquet", irods_target="/i/d.parquet"
    )
    catalog.delete_pending_push(snap_id)
    assert catalog.get_pending_push(snap_id) is None


def test_bump_pending_push_attempt_records_error(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="pending-bump")
    snap_id = _make_pending_snapshot(catalog, project)
    catalog.insert_pending_push(
        snapshot_id=snap_id, local_path="/cache/x.parquet", irods_target="/i/x.parquet"
    )
    bumped = catalog.bump_pending_push_attempt(snap_id, "iRODS unreachable")
    assert bumped is not None
    assert bumped.attempts == 1
    assert bumped.last_error == "iRODS unreachable"
    bumped2 = catalog.bump_pending_push_attempt(snap_id, "still down")
    assert bumped2 is not None
    assert bumped2.attempts == 2


def test_bump_pending_push_truncates_long_error(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="pending-truncate")
    snap_id = _make_pending_snapshot(catalog, project)
    catalog.insert_pending_push(
        snapshot_id=snap_id, local_path="/c/t.parquet", irods_target="/i/t.parquet"
    )
    huge = "x" * 5000
    bumped = catalog.bump_pending_push_attempt(snap_id, huge, error_max_chars=100)
    assert bumped is not None
    assert bumped.last_error is not None
    assert len(bumped.last_error) == 100


def test_bump_pending_push_missing_returns_none(catalog: CatalogStore) -> None:
    """Bumping a non-existent pending row returns None, doesn't raise."""
    assert catalog.bump_pending_push_attempt(999_999, "x") is None


def test_list_pending_pushes_drain_order(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="pending-list")
    snap_a = _make_pending_snapshot(catalog, project)
    snap_b = _make_pending_snapshot(catalog, project)
    snap_c = _make_pending_snapshot(catalog, project)
    catalog.insert_pending_push(snap_a, "/c/a", "/i/a")
    catalog.insert_pending_push(snap_b, "/c/b", "/i/b")
    catalog.insert_pending_push(snap_c, "/c/c", "/i/c")
    listed = catalog.list_pending_pushes()
    # created_at ASC then snapshot_id ASC: insertion order.
    assert [p.snapshot_id for p in listed] == [snap_a, snap_b, snap_c]


def test_pending_push_dropped_when_snapshot_deleted(catalog: CatalogStore) -> None:
    """ON DELETE CASCADE — dropping the snapshot also drops its pending row."""
    project = _make_project(catalog, slug="pending-cascade")
    snap_id = _make_pending_snapshot(catalog, project)
    catalog.insert_pending_push(snap_id, "/c/x", "/i/x")
    catalog.delete_snapshot(snap_id)
    assert catalog.get_pending_push(snap_id) is None


# ---------------------------------------------------------------------- list/latest filters


def _commit_snapshot(catalog: CatalogStore, project, *, filename: str) -> int:
    snap_id = _make_pending_snapshot(catalog, project)
    catalog.update_snapshot_parquet_file(snap_id, filename)
    return snap_id


def test_list_snapshots_skips_pending_by_default(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="list-filter")
    committed_a = _commit_snapshot(catalog, project, filename="snapshot_1.parquet")
    pending_b = _make_pending_snapshot(catalog, project)  # stays pending
    committed_c = _commit_snapshot(catalog, project, filename="snapshot_3.parquet")

    visible = catalog.list_snapshots(project.project_id)
    assert [s.snapshot_id for s in visible] == [committed_c, committed_a]

    all_rows = catalog.list_snapshots(project.project_id, include_pending=True)
    assert {s.snapshot_id for s in all_rows} == {committed_a, pending_b, committed_c}


def test_latest_snapshot_id_skips_pending_by_default(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="latest-filter")
    committed_a = _commit_snapshot(catalog, project, filename="snapshot_1.parquet")
    pending_b = _make_pending_snapshot(catalog, project)

    assert catalog.latest_snapshot_id(project.project_id) == committed_a
    assert (
        catalog.latest_snapshot_id(project.project_id, include_pending=True)
        == pending_b
    )


def test_latest_snapshot_id_returns_none_when_only_pending(catalog: CatalogStore) -> None:
    project = _make_project(catalog, slug="only-pending")
    _make_pending_snapshot(catalog, project)
    assert catalog.latest_snapshot_id(project.project_id) is None
