"""DuckLakeClient accepts catalog_dsn and the legacy postgres_dsn alias."""

from mesa_ducklake import DuckLakeClient
from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore


def test_catalog_dsn_kwarg_selects_duckdb(tmp_path):
    c = DuckLakeClient(catalog_dsn=f"duckdb:///{tmp_path / 'c.duckdb'}")
    assert isinstance(c._get_catalog(), DuckDBCatalogStore)
    c.close()


def test_postgres_dsn_alias_still_accepted(tmp_path):
    # The alias accepts any DSN the factory understands (here a duckdb one),
    # proving back-compat callers keep working.
    c = DuckLakeClient(postgres_dsn=f"duckdb:///{tmp_path / 'c2.duckdb'}")
    assert isinstance(c._get_catalog(), DuckDBCatalogStore)
    c.close()


def test_missing_dsn_raises():
    import pytest
    with pytest.raises(TypeError):
        DuckLakeClient()
