"""DuckDBCatalogStore CRUD against an in-memory / tmp DuckDB file (no Postgres)."""

import duckdb
import pytest

from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore
from mesa_ducklake.models import PARQUET_FILE_PENDING


@pytest.fixture
def store(tmp_path):
    s = DuckDBCatalogStore(str(tmp_path / "catalog.duckdb"))
    yield s
    s.close()


def test_register_and_get_project(store):
    p = store.register_project(
        irods_path="/iplant/home/u/proj", irods_zone="iplant",
        ducklake_path=None, created_by="u",
    )
    assert p.irods_path == "/iplant/home/u/proj"
    assert p.ducklake_path == "/iplant/home/u/proj/.mesa/ducklake"
    assert p.status == "active"
    got = store.get_project(p.project_id)
    assert got is not None and got.project_id == p.project_id


def test_get_project_missing_returns_none(store):
    from uuid import uuid4
    assert store.get_project(uuid4()) is None


def test_find_project_by_path(store):
    store.register_project("/iplant/home/u/proj", "iplant", None, "u")
    found = store.find_project_by_path("/iplant/home/u/proj")
    assert found is not None and found.irods_path == "/iplant/home/u/proj"
    assert store.find_project_by_path("/nope") is None


def test_duplicate_path_rejected(store):
    store.register_project("/iplant/home/u/proj", "iplant", None, "u")
    with pytest.raises(duckdb.ConstraintException):
        store.register_project("/iplant/home/u/proj", "iplant", None, "u")


# ------------------------------------------------------------------ snapshots


def _project(store):
    return store.register_project("/iplant/home/u/proj", "iplant", None, "u")


def test_snapshot_chain_and_latest(store):
    p = _project(store)
    s1 = store.create_snapshot(p.project_id, "u", None, "first", "snapshot_1.parquet")
    s2 = store.create_snapshot(p.project_id, "u", s1.snapshot_id, "second", "snapshot_2.parquet")
    assert s2.snapshot_id > s1.snapshot_id
    assert s2.parent_snapshot == s1.snapshot_id
    assert store.latest_snapshot_id(p.project_id) == s2.snapshot_id
    assert store.get_snapshot(s1.snapshot_id).note == "first"
    assert store.snapshot_ts(s2.snapshot_id) is not None


def test_latest_skips_pending(store):
    p = _project(store)
    committed = store.create_snapshot(p.project_id, "u", None, None, "snapshot_1.parquet")
    store.create_snapshot(p.project_id, "u", committed.snapshot_id, None, PARQUET_FILE_PENDING)
    # default excludes pending; include_pending sees the newer pending row
    assert store.latest_snapshot_id(p.project_id) == committed.snapshot_id
    assert store.latest_snapshot_id(p.project_id, include_pending=True) > committed.snapshot_id


def test_update_parquet_file_and_list(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    updated = store.update_snapshot_parquet_file(s.snapshot_id, "snapshot_1.parquet")
    assert updated.parquet_file == "snapshot_1.parquet"
    listed = store.list_snapshots(p.project_id)
    assert [x.snapshot_id for x in listed] == [s.snapshot_id]


def test_update_missing_snapshot_raises(store):
    with pytest.raises(KeyError):
        store.update_snapshot_parquet_file(999999, "x.parquet")


def test_delete_snapshot(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, "snapshot_1.parquet")
    store.delete_snapshot(s.snapshot_id)
    assert store.get_snapshot(s.snapshot_id) is None


# ------------------------------------------------------------------ pending pushes

def test_pending_push_insert_idempotent(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    first = store.insert_pending_push(
        s.snapshot_id, "/local/snapshot_1.parquet", "/irods/snapshot_1.parquet"
    )
    again = store.insert_pending_push(s.snapshot_id, "/DIFFERENT", "/DIFFERENT")
    assert first.snapshot_id == again.snapshot_id
    # idempotent: original row wins, not the second caller's values
    assert again.local_path == "/local/snapshot_1.parquet"


def test_pending_push_bump_and_get(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    bumped = store.bump_pending_push_attempt(s.snapshot_id, "boom")
    assert bumped.attempts == 1 and bumped.last_error == "boom"
    assert store.get_pending_push(s.snapshot_id).attempts == 1
    assert store.bump_pending_push_attempt(999999, "x") is None


def test_pending_push_list_and_delete(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    assert len(store.list_pending_pushes()) == 1
    store.delete_pending_push(s.snapshot_id)
    assert store.get_pending_push(s.snapshot_id) is None
    assert store.list_pending_pushes() == []


def test_delete_snapshot_cascades_pending_push(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    store.delete_snapshot(s.snapshot_id)
    assert store.get_snapshot(s.snapshot_id) is None
    assert store.get_pending_push(s.snapshot_id) is None


def test_bump_truncates_long_error(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    bumped = store.bump_pending_push_attempt(s.snapshot_id, "x" * 5000, error_max_chars=2000)
    assert len(bumped.last_error) == 2000


def test_list_snapshots_include_pending(store):
    p = _project(store)
    committed = store.create_snapshot(p.project_id, "u", None, None, "snapshot_1.parquet")
    pending = store.create_snapshot(
        p.project_id, "u", committed.snapshot_id, None, PARQUET_FILE_PENDING
    )
    default = store.list_snapshots(p.project_id)
    assert [x.snapshot_id for x in default] == [committed.snapshot_id]
    with_pending = store.list_snapshots(p.project_id, include_pending=True)
    assert {x.snapshot_id for x in with_pending} == {committed.snapshot_id, pending.snapshot_id}


def test_latest_snapshot_id_none_when_only_pending(store):
    p = _project(store)
    store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    assert store.latest_snapshot_id(p.project_id) is None


def test_memory_backend_constructs():
    s = DuckDBCatalogStore(":memory:")
    try:
        proj = s.register_project("/iplant/home/u/m", "iplant", None, "u")
        assert s.get_project(proj.project_id) is not None
    finally:
        s.close()
