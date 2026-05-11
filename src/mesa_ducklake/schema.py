"""DDL + migration runner for the Postgres ``mesa`` catalog schema.

Migrations are numbered SQL files in the ``migrations/`` directory at
the repo root:

* Files are named ``NNNN_<description>.sql`` with a numeric prefix
  (e.g. ``0001_initial.sql``).
* Files are discovered, sorted by their numeric prefix, and applied in
  ascending order.
* Each file is executed inside its own transaction.
* The runner records applied versions in a ``mesa.schema_versions``
  table so subsequent runs are idempotent.
* Schema changes are append-only: never edit a committed migration.

The migration runner is the **only** code path that mutates the live
Postgres schema. Application code does not run ad-hoc DDL.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg

# Regex matching ``NNNN_<rest>.sql``; the numeric prefix is captured.
_MIGRATION_FILENAME = re.compile(r"^(\d+)_[^/]+\.sql$")


def _find_migrations_dir() -> Path:
    """Locate the ``migrations/`` directory at the project root.

    The package is installed in editable mode from a checkout, so the
    repo root is two parents above this file (``src/mesa_ducklake/``).
    For an installed wheel, callers must supply ``MESA_MIGRATIONS_DIR``
    via the environment or rely on the in-tree layout we always use in
    tests.
    """
    here = Path(__file__).resolve()
    # src/mesa_ducklake/schema.py -> repo root is parents[2].
    candidate = here.parents[2] / "migrations"
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"could not locate migrations/ directory; looked at {candidate}"
    )


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return ``[(version, path)]`` sorted by version, ascending.

    Raises ``ValueError`` if two files share a numeric prefix — that
    would make the apply order ambiguous.
    """
    found: dict[int, Path] = {}
    for entry in sorted(migrations_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _MIGRATION_FILENAME.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(
                f"duplicate migration version {version}: "
                f"{found[version].name} and {entry.name}"
            )
        found[version] = entry
    return sorted(found.items())


_STANDALONE_BEGIN = re.compile(r"^\s*BEGIN\s*;\s*$", re.IGNORECASE)
_STANDALONE_COMMIT = re.compile(r"^\s*COMMIT\s*;\s*$", re.IGNORECASE)


def _strip_outer_transaction(sql: str) -> str:
    """Strip standalone ``BEGIN;`` and ``COMMIT;`` lines from a migration file.

    Migration files wrap their DDL in an explicit transaction for
    readability and to make them runnable directly through ``psql``.
    The runner already wraps each migration in a ``conn.transaction()``,
    so an embedded ``COMMIT;`` would prematurely end that transaction.
    We strip only **standalone** ``BEGIN;`` / ``COMMIT;`` lines (so an
    embedded SAVEPOINT-style transaction inside a migration, should
    one ever appear, is preserved).
    """
    kept = []
    for line in sql.splitlines(keepends=True):
        if _STANDALONE_BEGIN.match(line) or _STANDALONE_COMMIT.match(line):
            continue
        kept.append(line)
    return "".join(kept)


def _ensure_bookkeeping_table(conn: psycopg.Connection) -> None:
    """Create ``mesa.schema_versions`` if it doesn't exist.

    Bootstrap step; not itself a migration. The runner needs this table
    to know which migrations have already been applied. Idempotent.

    The caller is responsible for committing (or running under
    autocommit). We use ``CREATE ... IF NOT EXISTS`` so concurrent
    bootstrap is safe.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS mesa")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mesa.schema_versions (
                version    INT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                filename   TEXT NOT NULL
            )
            """
        )


def _already_applied(conn: psycopg.Connection) -> set[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM mesa.schema_versions")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(dsn: str, target: int | None = None) -> int:
    """Apply pending migrations against ``dsn``.

    Parameters
    ----------
    dsn:
        Standard libpq DSN for the target database.
    target:
        Optional inclusive upper bound on the version to apply. When
        ``None``, all pending migrations are applied.

    Returns
    -------
    int
        The count of migrations newly applied during this invocation.
        Zero when the database is already up to date.

    Notes
    -----
    * Each migration file is executed inside its own transaction; on
      failure, the runner stops and re-raises and the partially-failed
      migration is rolled back so it can be retried after a fix.
    * Idempotent: applying twice in a row applies zero migrations the
      second time.
    """
    migrations_dir = _find_migrations_dir()
    discovered = _discover_migrations(migrations_dir)

    applied_count = 0
    with psycopg.connect(dsn) as conn:
        # autocommit=True lets ``conn.transaction()`` explicitly bracket
        # each migration as one atomic unit. Otherwise psycopg's
        # implicit-transaction mode would interleave with our own
        # transaction blocks unpredictably.
        conn.autocommit = True

        # Bookkeeping table must exist before we can read which
        # versions have already been applied.
        _ensure_bookkeeping_table(conn)
        already = _already_applied(conn)

        for version, path in discovered:
            if version in already:
                continue
            if target is not None and version > target:
                break

            sql = _strip_outer_transaction(path.read_text())
            # Each migration is one atomic unit. On exception
            # ``with conn.transaction()`` rolls back; the runner
            # surfaces the error to the caller and stops.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO mesa.schema_versions (version, filename) "
                        "VALUES (%s, %s)",
                        (version, path.name),
                    )
            applied_count += 1

    return applied_count
