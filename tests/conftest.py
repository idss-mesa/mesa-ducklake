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
* ``quack_server``         — ``mesa-ducklake serve-catalog`` running in a
  subprocess for the session; yields its ``quack://`` DSN.
* ``quack_dsn``            — that DSN with the token set and the served
  catalog emptied, for one test.

A custom marker ``@pytest.mark.requires_postgres`` is registered for
tests that need a live Postgres. If the sandbox has no ``pg_ctl``
reachable, those tests are auto-skipped. Likewise
``@pytest.mark.requires_quack`` skips when DuckDB's quack extension
cannot be installed (e.g. offline).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
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


def _quack_available() -> bool:
    con = duckdb.connect()
    try:
        con.execute("INSTALL quack")
        con.execute("LOAD quack")
        return True
    except duckdb.Error:
        return False
    finally:
        con.close()


QUACK_AVAILABLE = _quack_available()
QUACK_TEST_TOKEN = "mesa-ducklake-test-token"

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
        "requires_quack: skip when DuckDB's quack extension cannot be loaded",
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
    skip_pg = pytest.mark.skip(
        reason=(
            "no Postgres executable on PATH or via pg_config; "
            "pytest-postgresql cannot start an ephemeral cluster"
        )
    )
    skip_quack = pytest.mark.skip(reason="DuckDB's quack extension cannot be installed/loaded")
    for item in items:
        if not POSTGRES_AVAILABLE and "requires_postgres" in item.keywords:
            item.add_marker(skip_pg)
        if not QUACK_AVAILABLE and "requires_quack" in item.keywords:
            item.add_marker(skip_quack)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return int(s.getsockname()[1])


def start_catalog_server(db_path: Path) -> tuple[subprocess.Popen[str], str]:
    """Start ``mesa-ducklake serve-catalog`` on ``db_path``; return the process and DSN."""
    port = _free_port()
    env = {
        **os.environ,
        "MESA_DUCKLAKE_DSN": f"duckdb://{db_path}",
        "MESA_DUCKLAKE_QUACK_TOKEN": QUACK_TEST_TOKEN,
    }
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            "from mesa_ducklake.cli import _console_main; _console_main()",
            "serve-catalog", "--port", str(port),
        ],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    if not proc.stdout.readline():
        proc.wait(timeout=30)
        pytest.fail(f"serve-catalog did not start: {proc.stderr.read()}")
    return proc, f"quack://localhost:{port}"


def stop_catalog_server(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the server and require a clean exit."""
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=30)
    assert proc.stderr is not None
    assert rc == 0, proc.stderr.read()


@pytest.fixture(scope="session")
def quack_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """One served catalog for the session; yields its ``quack://`` DSN."""
    if not QUACK_AVAILABLE:
        pytest.skip("DuckDB's quack extension cannot be installed/loaded")
    proc, dsn = start_catalog_server(tmp_path_factory.mktemp("quack") / "catalog.duckdb")
    yield dsn
    stop_catalog_server(proc)


@pytest.fixture
def quack_dsn(quack_server: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """The session catalog, emptied, with the client token in the environment."""
    from mesa_ducklake.catalog_quack import QuackCatalogStore

    monkeypatch.setenv("MESA_DUCKLAKE_QUACK_TOKEN", QUACK_TEST_TOKEN)
    store = QuackCatalogStore(quack_server[len("quack://"):])
    try:
        for table in ("pending_pushes", "snapshots", "projects"):
            store._run(f"DELETE FROM mesa.{table}", [])
    finally:
        store.close()
    return quack_server


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
