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
