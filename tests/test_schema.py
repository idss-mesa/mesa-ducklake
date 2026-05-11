"""Migration runner tests.

Most cases exercise pure parsing / discovery logic and run without a
live Postgres. The handful of cases that touch a database carry the
``requires_postgres`` marker and skip cleanly when no PG executable
is reachable in the sandbox.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import mesa_ducklake.schema as schema_mod
from mesa_ducklake.schema import (
    _discover_migrations,
    _find_migrations_dir,
    apply_migrations,
)


def test_find_migrations_dir_returns_repo_root_migrations() -> None:
    """In editable-install mode, the runner finds ``./migrations`` at repo root."""
    path = _find_migrations_dir()
    assert path.is_dir()
    assert (path / "0001_initial.sql").exists()


def test_discover_migrations_returns_sorted_by_numeric_prefix(tmp_path: Path) -> None:
    """File parsing tolerates zero-padded and non-padded prefixes."""
    (tmp_path / "0001_initial.sql").write_text("-- noop\n")
    (tmp_path / "0010_later.sql").write_text("-- noop\n")
    (tmp_path / "0002_middle.sql").write_text("-- noop\n")
    (tmp_path / "README.md").write_text("not a migration\n")

    discovered = _discover_migrations(tmp_path)
    versions = [v for v, _ in discovered]
    assert versions == [1, 2, 10]


def test_discover_migrations_rejects_duplicate_versions(tmp_path: Path) -> None:
    """Two files claiming the same numeric prefix is a hard error."""
    (tmp_path / "0001_first.sql").write_text("-- noop\n")
    (tmp_path / "0001_second.sql").write_text("-- noop\n")

    with pytest.raises(ValueError, match="duplicate migration version"):
        _discover_migrations(tmp_path)


# ---------------------------------------------------------------------------
# Postgres-dependent cases
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
def test_apply_migrations_creates_mesa_schema(postgres_dsn: str) -> None:
    """A fresh DB should end up with the ``mesa`` schema and core tables."""
    count = apply_migrations(postgres_dsn)
    assert count >= 1

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'mesa'
            ORDER BY table_name
            """
        )
        tables = {row[0] for row in cur.fetchall()}
    assert {"projects", "snapshots", "schema_versions"} <= tables


@pytest.mark.requires_postgres
def test_apply_migrations_is_idempotent(postgres_dsn: str) -> None:
    """Running twice in a row applies zero new migrations the second time."""
    first = apply_migrations(postgres_dsn)
    assert first >= 1
    second = apply_migrations(postgres_dsn)
    assert second == 0


@pytest.mark.requires_postgres
def test_apply_migrations_records_schema_versions(postgres_dsn: str) -> None:
    apply_migrations(postgres_dsn)
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT version, filename FROM mesa.schema_versions ORDER BY version"
        )
        rows = cur.fetchall()
    assert rows[0] == (1, "0001_initial.sql")


@pytest.mark.requires_postgres
def test_apply_migrations_respects_target(
    postgres_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``target=N`` stops the runner after applying version N (inclusive)."""
    fake_migrations = tmp_path / "migrations"
    fake_migrations.mkdir()
    real_initial = (
        Path(schema_mod.__file__).resolve().parents[2]
        / "migrations"
        / "0001_initial.sql"
    )
    (fake_migrations / "0001_initial.sql").write_text(real_initial.read_text())
    (fake_migrations / "0002_marker.sql").write_text(
        "CREATE TABLE mesa.target_marker (id INT);\n"
    )

    monkeypatch.setattr(schema_mod, "_find_migrations_dir", lambda: fake_migrations)

    applied = apply_migrations(postgres_dsn, target=1)
    assert applied == 1

    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'mesa' AND table_name = 'target_marker'
            """
        )
        assert cur.fetchone() is None

    # Now apply without a target: the marker migration is picked up.
    applied_more = apply_migrations(postgres_dsn)
    assert applied_more == 1
    with psycopg.connect(postgres_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'mesa' AND table_name = 'target_marker'
            """
        )
        assert cur.fetchone() == (1,)
