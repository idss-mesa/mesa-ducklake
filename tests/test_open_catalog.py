"""open_catalog dispatches to the right backend by DSN shape."""

import pytest

import mesa_ducklake.catalog as cat
from mesa_ducklake.catalog import PostgresCatalogStore, open_catalog
from mesa_ducklake.catalog_base import CatalogStore
from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore


def test_duckdb_uri(tmp_path):
    store = open_catalog(f"duckdb:///{tmp_path / 'c.duckdb'}")
    assert isinstance(store, DuckDBCatalogStore)
    assert isinstance(store, CatalogStore)  # Protocol conformance
    store.close()


def test_duckdb_bare_path(tmp_path):
    store = open_catalog(str(tmp_path / "c.duckdb"))
    assert isinstance(store, DuckDBCatalogStore)
    store.close()


def test_memory():
    store = open_catalog(":memory:")
    assert isinstance(store, DuckDBCatalogStore)
    store.close()


def test_postgres_scheme_dispatch(monkeypatch):
    class FakeConn:
        closed = False
        autocommit = False
        def close(self):  # pragma: no cover - not exercised here
            self.closed = True
    monkeypatch.setattr(cat.psycopg, "connect", lambda dsn: FakeConn())
    store = open_catalog("postgresql://mesa@localhost/mesa_ducklake")
    assert isinstance(store, PostgresCatalogStore)


def test_blank_raises():
    with pytest.raises(ValueError):
        open_catalog("")


def test_unrecognized_raises():
    with pytest.raises(ValueError):
        open_catalog("mysql://nope/db")


def test_duckdb_storage_version_option(tmp_path):
    store = open_catalog(f"duckdb://{tmp_path / 'c.duckdb'}?storage_version=v1.5.0")
    assert isinstance(store, DuckDBCatalogStore)
    store.close()


def test_bare_path_storage_version_option(tmp_path):
    store = open_catalog(f"{tmp_path / 'c.duckdb'}?storage_version=v1.5.0")
    assert isinstance(store, DuckDBCatalogStore)
    store.close()


def test_unknown_duckdb_option_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown catalog DSN option"):
        open_catalog(f"duckdb://{tmp_path / 'c.duckdb'}?read_only=true")


def test_postgres_query_string_is_left_alone():
    assert cat.catalog_backend("postgresql://u@h/db?sslmode=require") == "postgres"
