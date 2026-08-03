"""Where the test suite's Postgres comes from.

Two sources:

* an **external** server — a CI service container, or any instance the
  developer already has — selected by ``MESA_DUCKLAKE_TEST_PG_HOST``;
* otherwise an **ephemeral cluster** started by ``pytest-postgresql``,
  which needs ``pg_ctl`` on the host.

This lives in its own module rather than in ``conftest.py`` so it can be
unit-tested by *calling* it with an explicit environment. Testing it by
reimporting ``conftest.py`` would re-run ``pytest_postgresql``'s fixture
factories on every call, and that interleaves badly with the real
Postgres tests in the same session.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

HOST_ENV = "MESA_DUCKLAKE_TEST_PG_HOST"

_DEFAULTS = {
    "MESA_DUCKLAKE_TEST_PG_PORT": "5432",
    "MESA_DUCKLAKE_TEST_PG_USER": "postgres",
    "MESA_DUCKLAKE_TEST_PG_PASSWORD": "postgres",
    "MESA_DUCKLAKE_TEST_PG_DBNAME": "mesa_test",
}


def external_postgres(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Return connection settings for an externally-managed Postgres.

    ``None`` when no external server is configured. A host set to
    whitespace counts as unset — otherwise the suite would try to connect
    to nothing and fail every Postgres test rather than skipping them.
    """
    source = env if env is not None else os.environ
    host = (source.get(HOST_ENV) or "").strip()
    if not host:
        return None

    def _get(key: str) -> str:
        value = (source.get(key) or "").strip()
        return value or _DEFAULTS[key]

    return {
        "host": host,
        "port": int(_get("MESA_DUCKLAKE_TEST_PG_PORT")),
        "user": _get("MESA_DUCKLAKE_TEST_PG_USER"),
        "password": _get("MESA_DUCKLAKE_TEST_PG_PASSWORD"),
        "dbname": _get("MESA_DUCKLAKE_TEST_PG_DBNAME"),
    }


def pg_ctl_available() -> bool:
    """Best-effort probe for a ``pg_ctl`` that could start a cluster."""
    if shutil.which("pg_ctl") is not None:
        return True
    if shutil.which("pg_config") is None:
        return False
    try:
        bindir = subprocess.check_output(["pg_config", "--bindir"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return (Path(bindir) / "pg_ctl").exists()


def postgres_available(env: Mapping[str, str] | None = None) -> bool:
    """True when the suite can reach a Postgres, by either route.

    An external server counts even with no ``pg_ctl`` on the host: a CI
    runner has none, and a probe that required one would skip every
    Postgres test against a perfectly healthy service container.
    """
    if external_postgres(env) is not None:
        return True
    return pg_ctl_available()
