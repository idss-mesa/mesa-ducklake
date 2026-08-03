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

import pytest

from ._pg_env import external_postgres, postgres_available

# Each case passes its environment explicitly. An earlier version of
# these tests reimported conftest.py to observe its import-time
# decision, which re-ran pytest-postgresql's fixture factories on every
# call and broke the full-suite run while both halves passed alone.


def test_no_env_means_no_external_server():
    """A developer machine keeps the ephemeral-cluster behaviour."""
    assert external_postgres({}) is None


def test_host_env_selects_the_external_server():
    settings = external_postgres({"MESA_DUCKLAKE_TEST_PG_HOST": "localhost"})
    assert settings is not None
    assert settings["host"] == "localhost"


def test_external_server_counts_as_available():
    """Availability must not depend on pg_ctl when nothing needs starting.

    This is the crux: the CI runner has no pg_ctl, so a probe that only
    looked for one would mark Postgres unavailable and skip all 40
    Postgres tests despite a healthy service container.
    """
    assert postgres_available({"MESA_DUCKLAKE_TEST_PG_HOST": "db"}) is True


def test_connection_settings_come_from_the_environment():
    assert external_postgres(
        {
            "MESA_DUCKLAKE_TEST_PG_HOST": "db.internal",
            "MESA_DUCKLAKE_TEST_PG_PORT": "6543",
            "MESA_DUCKLAKE_TEST_PG_USER": "mesa",
            "MESA_DUCKLAKE_TEST_PG_PASSWORD": "s3cret",
            "MESA_DUCKLAKE_TEST_PG_DBNAME": "otherdb",
        }
    ) == {
        "host": "db.internal",
        "port": 6543,
        "user": "mesa",
        "password": "s3cret",
        "dbname": "otherdb",
    }


def test_defaults_fill_in_the_unset_settings():
    settings = external_postgres({"MESA_DUCKLAKE_TEST_PG_HOST": "db"})
    assert settings == {
        "host": "db",
        "port": 5432,
        "user": "postgres",
        "password": "postgres",
        "dbname": "mesa_test",
    }


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_host_is_not_an_external_server(blank):
    """An empty variable is 'unset', not 'connect to the empty host'."""
    assert external_postgres({"MESA_DUCKLAKE_TEST_PG_HOST": blank}) is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_overrides_fall_back_to_defaults(blank):
    """A set-but-empty override must not become an empty username."""
    settings = external_postgres(
        {
            "MESA_DUCKLAKE_TEST_PG_HOST": "db",
            "MESA_DUCKLAKE_TEST_PG_USER": blank,
            "MESA_DUCKLAKE_TEST_PG_PORT": blank,
        }
    )
    assert settings["user"] == "postgres"
    assert settings["port"] == 5432


def test_conftest_uses_the_same_decision():
    """The conftest must not reimplement this logic."""
    from . import conftest

    assert conftest.EXTERNAL_PG == external_postgres()
    assert conftest.POSTGRES_AVAILABLE == postgres_available()
