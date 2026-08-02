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

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_postgresql import factories

from mesa_ducklake import DuckLakeClient
from mesa_ducklake.schema import apply_migrations

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


#: Set this to point the suite at an already-running Postgres instead of
#: having ``pytest-postgresql`` start one. This is what CI uses: a
#: GitHub Actions *service container* supplies a live server, and no
#: ``pg_ctl`` exists on the runner's PATH to start a cluster with.
EXTERNAL_PG_HOST_ENV = "MESA_DUCKLAKE_TEST_PG_HOST"


def _external_postgres() -> dict[str, Any] | None:
    """Return connection settings for an externally-managed Postgres.

    ``None`` when no external server is configured, in which case the
    suite falls back to an ephemeral cluster started by
    ``pytest-postgresql``.
    """
    # ``.strip()``: a variable set to whitespace is "unset", not a host
    # named " ". Without this the suite would try to connect to nothing
    # and fail every Postgres test instead of skipping them.
    host = (os.environ.get(EXTERNAL_PG_HOST_ENV) or "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("MESA_DUCKLAKE_TEST_PG_PORT", "5432")),
        "user": os.environ.get("MESA_DUCKLAKE_TEST_PG_USER", "postgres"),
        "password": os.environ.get("MESA_DUCKLAKE_TEST_PG_PASSWORD", "postgres"),
        "dbname": os.environ.get("MESA_DUCKLAKE_TEST_PG_DBNAME", "mesa_test"),
    }


EXTERNAL_PG = _external_postgres()


def _postgres_available() -> bool:
    """Best-effort probe for a usable Postgres.

    An external server (CI service container) counts: nothing needs to
    be started, so ``pg_ctl`` is irrelevant there.
    """
    if EXTERNAL_PG is not None:
        return True
    if shutil.which("pg_ctl") is not None:
        return True
    if shutil.which("pg_config") is None:
        return False
    try:
        bindir = subprocess.check_output(
            ["pg_config", "--bindir"], text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    pg_ctl = Path(bindir) / "pg_ctl"
    return pg_ctl.exists()


POSTGRES_AVAILABLE = _postgres_available()

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
