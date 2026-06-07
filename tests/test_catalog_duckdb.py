"""DuckDBCatalogStore CRUD against an in-memory / tmp DuckDB file (no Postgres)."""

import duckdb
import pytest

from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore


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
