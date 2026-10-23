"""Quack catalog backend — internal.

A DuckDB-file catalog served by ``mesa-ducklake serve-catalog`` (DuckDB
2.0's Quack client-server protocol), so several writer processes, e.g.
CyVerse DE VMs each processing different sites, can share one catalog
without Postgres. The server owns the file and serializes writes through
DuckDB's MVCC; clients attach it with ``ATTACH 'quack:host:port'``.

The SQL surface is :class:`~mesa_ducklake.catalog_duckdb.DuckDBCatalogStore`'s;
only connecting and statement execution differ:

* The token comes from ``MESA_DUCKLAKE_QUACK_TOKEN``, never the DSN.
* Clients never run the schema bootstrap — concurrent ``CREATE … IF NOT
  EXISTS`` statements race. The server bootstraps; clients only check.
* DuckDB 2.0.0 does not forward prepared-statement parameters through
  Quack ("Values were not provided for the following parameters"), so
  each ``?`` is bound as a session variable and read back with
  ``getvariable()``. Variables are per cursor, and every statement gets
  its own cursor, so concurrent threads cannot see each other's values.
* Cursors of one client DuckDB instance share its Quack session, and a
  statement sent while another is in flight supersedes it ("INTERRUPT
  Error: superseded by a new query"). Statements are therefore serialized
  per store with a lock; separate processes (or stores) run in parallel.
* A write-write conflict (``TransactionException: Conflict on …``) aborts
  the statement without applying it, so it is retried with backoff.

Quack is beta in DuckDB 2.0; see ``docs/deploy/quack-catalog.md``.
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any

import duckdb

from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore

QUACK_TOKEN_ENV = "MESA_DUCKLAKE_QUACK_TOKEN"

_ALIAS = "mesa_catalog"
_VAR_PREFIX = "_mesa_p"
_REQUIRED_TABLES = ("projects", "snapshots", "pending_pushes")


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def bind_as_variables(sql: str, n_params: int) -> str:
    """Replace each ``?`` placeholder outside string literals with ``getvariable('_mesa_pN')``.

    Raises :class:`ValueError` when the placeholder count differs from
    ``n_params``, the same mistake DuckDB itself would reject.
    """
    out: list[str] = []
    n = 0
    in_string = False
    for ch in sql:
        if ch == "'":
            in_string = not in_string
        if ch == "?" and not in_string:
            out.append(f"getvariable('{_VAR_PREFIX}{n}')")
            n += 1
        else:
            out.append(ch)
    if n != n_params:
        raise ValueError(f"SQL has {n} placeholders but {n_params} parameters were given")
    return "".join(out)


class QuackCatalogStore(DuckDBCatalogStore):
    """Catalog backend on a Quack-served DuckDB file. See module docstring."""

    def __init__(
        self,
        address: str,
        *,
        token: str | None = None,
        conflict_retries: int = 3,
    ) -> None:
        token = token if token is not None else os.environ.get(QUACK_TOKEN_ENV, "")
        if not token:
            raise ValueError(
                f"a quack:// catalog needs a token: set {QUACK_TOKEN_ENV} to the token "
                "the catalog server was started with"
            )
        self._path = f"quack:{address}"
        self._conflict_retries = conflict_retries
        self._lock = threading.Lock()
        self._conn = duckdb.connect(":memory:")
        self._closed = False
        try:
            self._conn.execute("INSTALL quack")
            self._conn.execute("LOAD quack")
            self._conn.execute(
                f"ATTACH {_sql_literal(self._path)} AS {_ALIAS} (TOKEN {_sql_literal(token)})"
            )
            self._bootstrap()
        except BaseException:
            self.close()
            raise

    def _bootstrap(self) -> None:
        _, rows = self._run(
            "SELECT table_name FROM duckdb_tables() "
            f"WHERE database_name = '{_ALIAS}' AND schema_name = 'mesa'",
            [],
        )
        missing = set(_REQUIRED_TABLES) - {r[0] for r in rows}
        if missing:
            raise RuntimeError(
                f"catalog at {self._path} has no mesa.{', mesa.'.join(sorted(missing))}; "
                "start it with `mesa-ducklake serve-catalog`, which creates the schema"
            )

    def _run(self, sql: str, params: list[Any]) -> tuple[Any, list[tuple]]:
        bound = bind_as_variables(sql, len(params))
        for attempt in range(self._conflict_retries + 1):
            with self._lock:
                try:
                    return self._run_once(bound, params)
                except duckdb.TransactionException as exc:
                    if "Conflict" not in str(exc) or attempt == self._conflict_retries:
                        raise
            time.sleep(random.uniform(0.01, 0.05) * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    def _run_once(self, bound: str, params: list[Any]) -> tuple[Any, list[tuple]]:
        cur = self._conn.cursor()
        try:
            cur.execute(f"USE {_ALIAS}")
            for i, value in enumerate(params):
                cur.execute(f"SET VARIABLE {_VAR_PREFIX}{i} = ?", [value])
            cur.execute(bound)
            desc = cur.description
            return desc, cur.fetchall() if desc else []
        finally:
            cur.close()
