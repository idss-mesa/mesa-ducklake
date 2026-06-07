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
