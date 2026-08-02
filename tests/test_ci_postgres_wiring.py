"""The external-Postgres path used by CI.

``pytest-postgresql`` normally starts its own cluster via ``pg_ctl``. A
GitHub Actions *service container* instead supplies an already-running
server, and the runner has no ``pg_ctl`` to start one with — so without
an external-server path every Postgres test would skip and CI would be
green while exercising none of the catalog backend.

These tests pin the switch itself. They deliberately do not need a
database: the point is that the decision to use an external server, and
the resulting availability verdict, are correct.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).parent / "conftest.py"


def _load_conftest(monkeypatch, env: dict[str, str]):
    """Import conftest.py fresh under a given environment.

    The external-server decision is made at import time (it has to be —
    the fixtures are defined conditionally), so it can only be tested by
    reimporting under different environments.
    """
    for key in list(sys.modules):
        if key == "_mesa_conftest_probe":
            del sys.modules[key]
    monkeypatch.delenv("MESA_DUCKLAKE_TEST_PG_HOST", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("_mesa_conftest_probe", CONFTEST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mesa_conftest_probe"] = module
    spec.loader.exec_module(module)
    return module


def test_no_env_means_no_external_server(monkeypatch):
    """A developer machine keeps the ephemeral-cluster behaviour."""
    mod = _load_conftest(monkeypatch, {})
    assert mod.EXTERNAL_PG is None


def test_host_env_selects_the_external_server(monkeypatch):
    mod = _load_conftest(monkeypatch, {"MESA_DUCKLAKE_TEST_PG_HOST": "localhost"})
    assert mod.EXTERNAL_PG is not None
    assert mod.EXTERNAL_PG["host"] == "localhost"


def test_external_server_counts_as_available(monkeypatch):
    """Availability must not depend on pg_ctl when nothing needs starting.

    This is the crux: the CI runner has no pg_ctl, so a probe that only
    looked for one would mark Postgres unavailable and skip all 40
    tests despite a healthy service container.
    """
    mod = _load_conftest(monkeypatch, {"MESA_DUCKLAKE_TEST_PG_HOST": "db"})
    assert mod.POSTGRES_AVAILABLE is True


def test_connection_settings_come_from_the_environment(monkeypatch):
    mod = _load_conftest(
        monkeypatch,
        {
            "MESA_DUCKLAKE_TEST_PG_HOST": "db.internal",
            "MESA_DUCKLAKE_TEST_PG_PORT": "6543",
            "MESA_DUCKLAKE_TEST_PG_USER": "mesa",
            "MESA_DUCKLAKE_TEST_PG_PASSWORD": "s3cret",
            "MESA_DUCKLAKE_TEST_PG_DBNAME": "otherdb",
        },
    )
    assert mod.EXTERNAL_PG == {
        "host": "db.internal",
        "port": 6543,
        "user": "mesa",
        "password": "s3cret",
        "dbname": "otherdb",
    }


def test_port_defaults_to_5432(monkeypatch):
    mod = _load_conftest(monkeypatch, {"MESA_DUCKLAKE_TEST_PG_HOST": "db"})
    assert mod.EXTERNAL_PG["port"] == 5432


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_host_is_not_an_external_server(monkeypatch, blank):
    """An empty variable is 'unset', not 'connect to the empty host'."""
    mod = _load_conftest(monkeypatch, {"MESA_DUCKLAKE_TEST_PG_HOST": blank})
    assert mod.EXTERNAL_PG is None
