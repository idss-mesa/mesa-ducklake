"""Shared pytest fixtures for mesa-ducklake.

Provides:

* ``dummy_irods_session``  — stand-in iRODS session for tests that
  don't need real iRODS behavior.
* ``client_fixture``       — bare client construction, used by smoke
  tests.
* ``postgres_dsn``         — DSN of an ephemeral Postgres started by
  ``pytest-postgresql``. Tests that depend on this fixture skip
  cleanly when no Postgres executable is on the host (see the
  ``requires_postgres`` marker).
* ``catalog_db``           — migrations applied against
  ``postgres_dsn``; yields the DSN.
* ``tmp_lake_root``        — ``tmp_path``-rooted lake directory.

A custom marker ``@pytest.mark.requires_postgres`` is registered for
tests that need a live Postgres. If the sandbox has no ``pg_ctl``
reachable, those tests are auto-skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_postgresql import factories

from mesa_ducklake import DuckLakeClient
from mesa_ducklake.schema import apply_migrations

from ._pg_env import external_postgres, postgres_available

_DUMMY_DSN = "postgresql://mesa:mesa@localhost:5432/mesa_test"


class _DummyIrodsSession:
    """Stand-in for a python-irodsclient session."""


@pytest.fixture
def dummy_irods_session() -> Any:
    return _DummyIrodsSession()


@pytest.fixture
def client_fixture(dummy_irods_session: Any) -> DuckLakeClient:
    """Bare client: no live Postgres, no lake_root. Used by smoke tests only."""
    return DuckLakeClient(postgres_dsn=_DUMMY_DSN, irods_session=dummy_irods_session)


# ---------------------------------------------------------------------------
# Postgres detection
#
# ``pytest-postgresql`` shells out to either a pinned ``pg_ctl`` (default
# ``/usr/lib/postgresql/13/bin/pg_ctl``) or, if that doesn't exist, to
# ``pg_config --bindir``. If neither path resolves to a real executable
# in the sandbox we run in, every Postgres-dependent test gets skipped
# rather than failing in fixture setup.
# ---------------------------------------------------------------------------


# The decision of *where* Postgres comes from lives in ``_pg_env`` so it
# can be unit-tested without reimporting this module — a reimport re-runs
# the fixture factories below, which does not survive being interleaved
# with the real Postgres tests.
EXTERNAL_PG = external_postgres()
POSTGRES_AVAILABLE = postgres_available()

# When an external server is configured, replace pytest-postgresql's
# process-starting fixture with a no-op one pointed at it, and rebind
# ``postgresql`` to connect through that. Each test still gets its own
# freshly-created database on the shared server, so isolation is
# unchanged from the ephemeral-cluster path.
if EXTERNAL_PG is not None:
    postgresql_external_proc = factories.postgresql_noproc(
        host=EXTERNAL_PG["host"],
        port=EXTERNAL_PG["port"],
        user=EXTERNAL_PG["user"],
        password=EXTERNAL_PG["password"],
        dbname=EXTERNAL_PG["dbname"],
    )
    postgresql = factories.postgresql(
        "postgresql_external_proc",
        dbname=EXTERNAL_PG["dbname"],
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_postgres: skip when no Postgres executable is reachable",
    )
    config.addinivalue_line(
        "markers",
        "live_e2e: scripted mesa-mcp e2e against live iRODS/OLS; skips unless "
        "MESA_E2E_IRODS_ROOT is set (see docs/dev/llm-e2e-tests.md)",
    )
    config.addinivalue_line(
        "markers",
        "llm_e2e: LLM-driven mesa-mcp e2e; additionally needs LLM_MODEL "
        "(see docs/dev/llm-e2e-tests.md)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if POSTGRES_AVAILABLE:
        return
    skip = pytest.mark.skip(
        reason=(
            "no Postgres executable on PATH or via pg_config; "
            "pytest-postgresql cannot start an ephemeral cluster"
        )
    )
    for item in items:
        if "requires_postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def postgres_dsn(postgresql: Any) -> str:
    """Return a libpq DSN string for the per-test ephemeral Postgres.

    ``postgresql`` is supplied by ``pytest-postgresql``. We translate
    its connection info into a DSN string so the migration runner
    (which opens its own connection via ``psycopg.connect``) can be
    exercised end-to-end.
    """
    info = postgresql.info
    parts = [
        f"host={info.host}",
        f"port={info.port}",
        f"dbname={info.dbname}",
        f"user={info.user}",
    ]
    pw = info.password
    if pw:
        parts.append(f"password={pw}")
    return " ".join(parts)


@pytest.fixture
def catalog_db(postgres_dsn: str) -> str:
    """Apply all migrations against the ephemeral Postgres; yield the DSN."""
    apply_migrations(postgres_dsn)
    return postgres_dsn


@pytest.fixture
def tmp_lake_root(tmp_path: Path) -> Path:
    """Return a fresh per-test directory to host snapshot Parquet files."""
    root = tmp_path / "lake"
    root.mkdir()
    return root
