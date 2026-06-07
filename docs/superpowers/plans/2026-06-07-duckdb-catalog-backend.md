# DuckDB Catalog Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let mesa-ducklake's catalog run on a self-contained local DuckDB file instead of Postgres, selected automatically from the existing `catalog_dsn`, so single-user/local installs get AVU history with zero external database.

**Architecture:** Extract a `CatalogStore` Protocol; keep the existing Postgres class (renamed `PostgresCatalogStore`); add a `DuckDBCatalogStore` over a single `.duckdb` file; an `open_catalog(dsn)` factory dispatches by DSN scheme. The Parquet data plane (`lake.py` → iRODS `.mesa/ducklake/`) and the `DuckLakeClient` public API are unchanged.

**Tech Stack:** Python 3.11, DuckDB 1.5.3 (sequences + `RETURNING` + `now()`/`nextval` defaults, `UNIQUE` → `duckdb.ConstraintException`), Pydantic v2, pytest, uv-managed venv at `/Users/tswetnam/Desktop/mesa-ai-test/.venv`.

**Spec:** `mesa-ducklake/docs/superpowers/specs/2026-06-07-duckdb-catalog-backend-design.md`

**Conventions for every command below:**
- `PY=/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python`
- Run all `pytest` from the `mesa-ducklake/` repo root: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-ducklake`
- Commit trailer (append to every commit message):
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `src/mesa_ducklake/catalog_base.py` | `CatalogStore` Protocol (the backend interface) | Create |
| `src/mesa_ducklake/catalog.py` | `PostgresCatalogStore` (renamed) + `open_catalog` factory | Modify |
| `src/mesa_ducklake/catalog_duckdb.py` | `DuckDBCatalogStore` over a DuckDB file | Create |
| `src/mesa_ducklake/client.py` | Facade: `catalog_dsn` param + `open_catalog` dispatch | Modify |
| `tests/test_catalog_base.py` | Protocol conformance | Create |
| `tests/test_catalog_duckdb.py` | DuckDB catalog CRUD (no Postgres) | Create |
| `tests/test_open_catalog.py` | Factory DSN dispatch | Create |
| `tests/test_client_e2e_duckdb.py` | Client end-to-end over DuckDB (local-only) | Create |
| `tests/test_catalog.py` | Existing Postgres tests | Modify (import rename) |
| `mesa-mcp/src/mesa_mcp/ducklake/client.py` | Pass `catalog_dsn=` kwarg | Modify (1 line) |

---

## Task 0: Prerequisites — dev deps + feature branch

**Files:** none (environment + git)

- [ ] **Step 1: Install test/lint tooling into the venv (uv-managed; no pip in venv)**

```bash
uv pip install --python /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python \
  pytest pytest-asyncio pytest-postgresql ruff mypy
```

- [ ] **Step 2: Verify the runner works**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest --version`
Expected: prints a `pytest 8.x` version line.

- [ ] **Step 3: Baseline the existing suite (sanity)**

Run: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-ducklake && /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest -q`
Expected: PASS (Postgres-marked tests may SKIP when no Postgres binary is present — skips are fine, failures are not).

- [ ] **Step 4: Create the feature branch (repo is on `main`)**

```bash
cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-ducklake
git checkout -b feat/duckdb-catalog-backend
```

---

## Task 1: Rename `CatalogStore` → `PostgresCatalogStore`

**Files:**
- Modify: `src/mesa_ducklake/catalog.py` (class name only)
- Modify: `tests/test_catalog.py` (import + fixture annotation)

- [ ] **Step 1: Update the existing test to the new name (this is the failing test)**

In `tests/test_catalog.py`, change the import and every `CatalogStore` annotation to `PostgresCatalogStore`:

```python
from mesa_ducklake.catalog import PostgresCatalogStore


@pytest.fixture
def catalog(catalog_db: str) -> PostgresCatalogStore:
    return PostgresCatalogStore(catalog_db)
```

Replace `CatalogStore` with `PostgresCatalogStore` in the remaining type hints in that file (e.g. `def test_register_project_persists_row(catalog: PostgresCatalogStore) -> None:`).

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog.py -q`
Expected: collection/import error — `ImportError: cannot import name 'PostgresCatalogStore'`.

- [ ] **Step 3: Rename the class in `catalog.py`**

In `src/mesa_ducklake/catalog.py`, change the class declaration:

```python
class PostgresCatalogStore:
    """Internal CRUD layer over the Postgres ``mesa`` schema.
```

(Leave the entire body unchanged — only the class name changes. Update the module docstring's first line to "Postgres catalog operations — internal." if desired.)

- [ ] **Step 4: Run to verify it passes (or skips cleanly)**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog.py -q`
Expected: PASS, or SKIP if no Postgres binary (no import errors).

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/catalog.py tests/test_catalog.py
git commit -m "refactor(catalog): rename CatalogStore to PostgresCatalogStore

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `CatalogStore` Protocol (`catalog_base.py`)

**Files:**
- Create: `src/mesa_ducklake/catalog_base.py`
- Test: `tests/test_catalog_base.py`

- [ ] **Step 1: Write the failing test**

`tests/test_catalog_base.py`:

```python
"""The CatalogStore Protocol recognizes a complete backend and rejects a partial one."""

from mesa_ducklake.catalog_base import CatalogStore


class _Complete:
    def register_project(self, *a, **k): ...
    def get_project(self, *a, **k): ...
    def find_project_by_path(self, *a, **k): ...
    def create_snapshot(self, *a, **k): ...
    def update_snapshot_parquet_file(self, *a, **k): ...
    def delete_snapshot(self, *a, **k): ...
    def latest_snapshot_id(self, *a, **k): ...
    def list_snapshots(self, *a, **k): ...
    def get_snapshot(self, *a, **k): ...
    def snapshot_ts(self, *a, **k): ...
    def insert_pending_push(self, *a, **k): ...
    def delete_pending_push(self, *a, **k): ...
    def bump_pending_push_attempt(self, *a, **k): ...
    def list_pending_pushes(self, *a, **k): ...
    def get_pending_push(self, *a, **k): ...
    def close(self): ...


class _Partial:
    def register_project(self, *a, **k): ...


def test_complete_backend_satisfies_protocol():
    assert isinstance(_Complete(), CatalogStore)


def test_partial_backend_does_not_satisfy_protocol():
    assert not isinstance(_Partial(), CatalogStore)
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_base.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mesa_ducklake.catalog_base'`.

- [ ] **Step 3: Create the Protocol**

`src/mesa_ducklake/catalog_base.py`:

```python
"""CatalogStore — the catalog backend interface (internal).

Both ``PostgresCatalogStore`` (catalog.py) and ``DuckDBCatalogStore``
(catalog_duckdb.py) implement this surface. ``DuckLakeClient`` and
``irods_sync`` depend on this Protocol, not on a concrete driver.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from mesa_ducklake.models import PendingPush, Project, Snapshot


@runtime_checkable
class CatalogStore(Protocol):
    """Method surface used by DuckLakeClient + irods_sync."""

    # projects
    def register_project(
        self, irods_path: str, irods_zone: str,
        ducklake_path: str | None, created_by: str,
    ) -> Project: ...
    def get_project(self, project_id: UUID) -> Project | None: ...
    def find_project_by_path(self, irods_path: str) -> Project | None: ...

    # snapshots
    def create_snapshot(
        self, project_id: UUID, actor: str, parent_snapshot: int | None,
        note: str | None, parquet_file: str,
    ) -> Snapshot: ...
    def update_snapshot_parquet_file(self, snapshot_id: int, parquet_file: str) -> Snapshot: ...
    def delete_snapshot(self, snapshot_id: int) -> None: ...
    def latest_snapshot_id(self, project_id: UUID, *, include_pending: bool = False) -> int | None: ...
    def list_snapshots(self, project_id: UUID, limit: int = 100, *, include_pending: bool = False) -> list[Snapshot]: ...
    def get_snapshot(self, snapshot_id: int) -> Snapshot | None: ...
    def snapshot_ts(self, snapshot_id: int) -> datetime | None: ...

    # pending pushes
    def insert_pending_push(self, snapshot_id: int, local_path: str, irods_target: str) -> PendingPush: ...
    def delete_pending_push(self, snapshot_id: int) -> None: ...
    def bump_pending_push_attempt(self, snapshot_id: int, error: str, *, error_max_chars: int = 2000) -> PendingPush | None: ...
    def list_pending_pushes(self, limit: int = 100) -> list[PendingPush]: ...
    def get_pending_push(self, snapshot_id: int) -> PendingPush | None: ...

    # lifecycle
    def close(self) -> None: ...
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_base.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/catalog_base.py tests/test_catalog_base.py
git commit -m "feat(catalog): add CatalogStore Protocol interface

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `DuckDBCatalogStore` — construction + projects

**Files:**
- Create: `src/mesa_ducklake/catalog_duckdb.py`
- Test: `tests/test_catalog_duckdb.py`

- [ ] **Step 1: Write the failing tests (projects)**

`tests/test_catalog_duckdb.py`:

```python
"""DuckDBCatalogStore CRUD against an in-memory / tmp DuckDB file (no Postgres)."""

import duckdb
import pytest

from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore


@pytest.fixture
def store(tmp_path):
    s = DuckDBCatalogStore(str(tmp_path / "catalog.duckdb"))
    yield s
    s.close()


def test_register_and_get_project(store):
    p = store.register_project(
        irods_path="/iplant/home/u/proj", irods_zone="iplant",
        ducklake_path=None, created_by="u",
    )
    assert p.irods_path == "/iplant/home/u/proj"
    assert p.ducklake_path == "/iplant/home/u/proj/.mesa/ducklake"
    assert p.status == "active"
    got = store.get_project(p.project_id)
    assert got is not None and got.project_id == p.project_id


def test_get_project_missing_returns_none(store):
    from uuid import uuid4
    assert store.get_project(uuid4()) is None


def test_find_project_by_path(store):
    store.register_project("/iplant/home/u/proj", "iplant", None, "u")
    found = store.find_project_by_path("/iplant/home/u/proj")
    assert found is not None and found.irods_path == "/iplant/home/u/proj"
    assert store.find_project_by_path("/nope") is None


def test_duplicate_path_rejected(store):
    store.register_project("/iplant/home/u/proj", "iplant", None, "u")
    with pytest.raises(duckdb.ConstraintException):
        store.register_project("/iplant/home/u/proj", "iplant", None, "u")
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_duckdb.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mesa_ducklake.catalog_duckdb'`.

- [ ] **Step 3: Create `catalog_duckdb.py` (schema bootstrap + helpers + project methods)**

`src/mesa_ducklake/catalog_duckdb.py`:

```python
"""DuckDB-file catalog backend — internal.

A single local DuckDB database file standing in for the Postgres catalog,
for single-user / local-install deployments that don't want to run Postgres.
Implements the same surface as PostgresCatalogStore (see
:class:`mesa_ducklake.catalog_base.CatalogStore`).

Single-writer: one OS process holds the DuckDB file lock at a time. Use the
Postgres backend for concurrent/hosted multi-writer deployments.

``project_id`` is stored as TEXT (UUID string form) — matching how the
Parquet data plane stores it — to avoid UUID parameter-binding edge cases;
Pydantic coerces the string back to ``uuid.UUID`` on the way out.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

import duckdb

from mesa_ducklake.irods_path import ducklake_subpath
from mesa_ducklake.models import PARQUET_FILE_PENDING, PendingPush, Project, Snapshot

# Idempotent schema bootstrap. FK constraints are intentionally omitted
# (DuckDB self-referencing FKs are limited); integrity is enforced by the
# DuckLakeClient write protocol exactly as it is for Postgres.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS mesa",
    "CREATE SEQUENCE IF NOT EXISTS mesa.snapshots_seq START 1",
    """
    CREATE TABLE IF NOT EXISTS mesa.projects (
        project_id    TEXT PRIMARY KEY,
        irods_path    TEXT NOT NULL UNIQUE,
        irods_zone    TEXT NOT NULL,
        ducklake_path TEXT NOT NULL,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by    TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'active'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mesa.snapshots (
        snapshot_id     BIGINT PRIMARY KEY DEFAULT nextval('mesa.snapshots_seq'),
        project_id      TEXT NOT NULL,
        ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor           TEXT NOT NULL,
        parent_snapshot BIGINT,
        note            TEXT,
        parquet_file    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mesa.pending_pushes (
        snapshot_id  BIGINT PRIMARY KEY,
        local_path   TEXT NOT NULL,
        irods_target TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        last_error   TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


def _to_dict(description: Sequence[Any], row: tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {col[0]: val for col, val in zip(description, row)}


def _row_to_project(d: dict[str, Any]) -> Project:
    return Project(
        project_id=d["project_id"],
        irods_path=d["irods_path"],
        irods_zone=d["irods_zone"],
        ducklake_path=d["ducklake_path"],
        created_at=d["created_at"],
        created_by=d["created_by"],
        status=d["status"],
    )


def _row_to_snapshot(d: dict[str, Any]) -> Snapshot:
    return Snapshot(
        snapshot_id=d["snapshot_id"],
        project_id=d["project_id"],
        ts=d["ts"],
        actor=d["actor"],
        parent_snapshot=d["parent_snapshot"],
        note=d["note"],
        parquet_file=d["parquet_file"],
    )


def _row_to_pending_push(d: dict[str, Any]) -> PendingPush:
    return PendingPush(
        snapshot_id=d["snapshot_id"],
        local_path=d["local_path"],
        irods_target=d["irods_target"],
        attempts=d["attempts"],
        last_error=d["last_error"],
        created_at=d["created_at"],
    )


class DuckDBCatalogStore:
    """Catalog backend over a single DuckDB file. See module docstring."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            resolved = Path(self._path).expanduser()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self._path = str(resolved)
        self._conn = duckdb.connect(self._path)
        for stmt in _SCHEMA_STATEMENTS:
            self._conn.execute(stmt)

    # ------------------------------------------------------------------ helpers
    def _one(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        cur = self._conn.execute(sql, params)
        desc = cur.description
        return _to_dict(desc, cur.fetchone())

    def _all(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, params)
        desc = cur.description
        out = []
        for r in cur.fetchall():
            d = _to_dict(desc, r)
            if d is not None:
                out.append(d)
        return out

    # ------------------------------------------------------------------ projects
    def register_project(self, irods_path, irods_zone, ducklake_path, created_by) -> Project:
        path = ducklake_path or ducklake_subpath(irods_path)
        d = self._one(
            """
            INSERT INTO mesa.projects
                (project_id, irods_path, irods_zone, ducklake_path, created_by)
            VALUES (?, ?, ?, ?, ?)
            RETURNING project_id, irods_path, irods_zone, ducklake_path,
                      created_at, created_by, status
            """,
            [str(uuid4()), irods_path, irods_zone, path, created_by],
        )
        assert d is not None
        return _row_to_project(d)

    def get_project(self, project_id) -> Project | None:
        d = self._one(
            """SELECT project_id, irods_path, irods_zone, ducklake_path,
                      created_at, created_by, status
               FROM mesa.projects WHERE project_id = ?""",
            [str(project_id)],
        )
        return _row_to_project(d) if d else None

    def find_project_by_path(self, irods_path) -> Project | None:
        d = self._one(
            """SELECT project_id, irods_path, irods_zone, ducklake_path,
                      created_at, created_by, status
               FROM mesa.projects WHERE irods_path = ?""",
            [irods_path],
        )
        return _row_to_project(d) if d else None

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_duckdb.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/catalog_duckdb.py tests/test_catalog_duckdb.py
git commit -m "feat(catalog): DuckDBCatalogStore construction + project CRUD

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `DuckDBCatalogStore` — snapshots

**Files:**
- Modify: `src/mesa_ducklake/catalog_duckdb.py` (add snapshot methods)
- Modify: `tests/test_catalog_duckdb.py` (add snapshot tests)

- [ ] **Step 1: Append the failing tests**

Add to `tests/test_catalog_duckdb.py`:

```python
from mesa_ducklake.models import PARQUET_FILE_PENDING


def _project(store):
    return store.register_project("/iplant/home/u/proj", "iplant", None, "u")


def test_snapshot_chain_and_latest(store):
    p = _project(store)
    s1 = store.create_snapshot(p.project_id, "u", None, "first", "snapshot_1.parquet")
    s2 = store.create_snapshot(p.project_id, "u", s1.snapshot_id, "second", "snapshot_2.parquet")
    assert s2.snapshot_id > s1.snapshot_id
    assert s2.parent_snapshot == s1.snapshot_id
    assert store.latest_snapshot_id(p.project_id) == s2.snapshot_id
    assert store.get_snapshot(s1.snapshot_id).note == "first"
    assert store.snapshot_ts(s2.snapshot_id) is not None


def test_latest_skips_pending(store):
    p = _project(store)
    committed = store.create_snapshot(p.project_id, "u", None, None, "snapshot_1.parquet")
    store.create_snapshot(p.project_id, "u", committed.snapshot_id, None, PARQUET_FILE_PENDING)
    # default excludes pending; include_pending sees the newer pending row
    assert store.latest_snapshot_id(p.project_id) == committed.snapshot_id
    assert store.latest_snapshot_id(p.project_id, include_pending=True) > committed.snapshot_id


def test_update_parquet_file_and_list(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    updated = store.update_snapshot_parquet_file(s.snapshot_id, "snapshot_1.parquet")
    assert updated.parquet_file == "snapshot_1.parquet"
    listed = store.list_snapshots(p.project_id)
    assert [x.snapshot_id for x in listed] == [s.snapshot_id]


def test_update_missing_snapshot_raises(store):
    with pytest.raises(KeyError):
        store.update_snapshot_parquet_file(999999, "x.parquet")


def test_delete_snapshot(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, "snapshot_1.parquet")
    store.delete_snapshot(s.snapshot_id)
    assert store.get_snapshot(s.snapshot_id) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_duckdb.py -q`
Expected: FAIL — `AttributeError: 'DuckDBCatalogStore' object has no attribute 'create_snapshot'`.

- [ ] **Step 3: Add snapshot methods to the class**

Insert these methods into `DuckDBCatalogStore` (after `find_project_by_path`, before `close`):

```python
    # ------------------------------------------------------------------ snapshots
    def create_snapshot(self, project_id, actor, parent_snapshot, note, parquet_file) -> Snapshot:
        d = self._one(
            """
            INSERT INTO mesa.snapshots
                (project_id, actor, parent_snapshot, note, parquet_file)
            VALUES (?, ?, ?, ?, ?)
            RETURNING snapshot_id, project_id, ts, actor, parent_snapshot,
                      note, parquet_file
            """,
            [str(project_id), actor, parent_snapshot, note, parquet_file],
        )
        assert d is not None
        return _row_to_snapshot(d)

    def update_snapshot_parquet_file(self, snapshot_id, parquet_file) -> Snapshot:
        d = self._one(
            """UPDATE mesa.snapshots SET parquet_file = ? WHERE snapshot_id = ?
               RETURNING snapshot_id, project_id, ts, actor, parent_snapshot,
                         note, parquet_file""",
            [parquet_file, snapshot_id],
        )
        if d is None:
            raise KeyError(f"snapshot {snapshot_id} not found")
        return _row_to_snapshot(d)

    def delete_snapshot(self, snapshot_id) -> None:
        self._conn.execute("DELETE FROM mesa.snapshots WHERE snapshot_id = ?", [snapshot_id])

    def latest_snapshot_id(self, project_id, *, include_pending=False) -> int | None:
        if include_pending:
            d = self._one(
                "SELECT snapshot_id FROM mesa.snapshots WHERE project_id = ? "
                "ORDER BY snapshot_id DESC LIMIT 1",
                [str(project_id)],
            )
        else:
            d = self._one(
                "SELECT snapshot_id FROM mesa.snapshots WHERE project_id = ? "
                "AND parquet_file <> ? ORDER BY snapshot_id DESC LIMIT 1",
                [str(project_id), PARQUET_FILE_PENDING],
            )
        return d["snapshot_id"] if d else None

    def list_snapshots(self, project_id, limit=100, *, include_pending=False) -> list[Snapshot]:
        if include_pending:
            rows = self._all(
                """SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                          note, parquet_file
                   FROM mesa.snapshots WHERE project_id = ?
                   ORDER BY snapshot_id DESC LIMIT ?""",
                [str(project_id), limit],
            )
        else:
            rows = self._all(
                """SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                          note, parquet_file
                   FROM mesa.snapshots WHERE project_id = ? AND parquet_file <> ?
                   ORDER BY snapshot_id DESC LIMIT ?""",
                [str(project_id), PARQUET_FILE_PENDING, limit],
            )
        return [_row_to_snapshot(d) for d in rows]

    def get_snapshot(self, snapshot_id) -> Snapshot | None:
        d = self._one(
            """SELECT snapshot_id, project_id, ts, actor, parent_snapshot,
                      note, parquet_file
               FROM mesa.snapshots WHERE snapshot_id = ?""",
            [snapshot_id],
        )
        return _row_to_snapshot(d) if d else None

    def snapshot_ts(self, snapshot_id) -> datetime | None:
        d = self._one("SELECT ts FROM mesa.snapshots WHERE snapshot_id = ?", [snapshot_id])
        return d["ts"] if d else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_duckdb.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/catalog_duckdb.py tests/test_catalog_duckdb.py
git commit -m "feat(catalog): DuckDBCatalogStore snapshot operations

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `DuckDBCatalogStore` — pending pushes

**Files:**
- Modify: `src/mesa_ducklake/catalog_duckdb.py` (add pending-push methods)
- Modify: `tests/test_catalog_duckdb.py` (add pending-push tests)

- [ ] **Step 1: Append the failing tests**

Add to `tests/test_catalog_duckdb.py`:

```python
def test_pending_push_insert_idempotent(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    first = store.insert_pending_push(s.snapshot_id, "/local/snapshot_1.parquet", "/irods/snapshot_1.parquet")
    again = store.insert_pending_push(s.snapshot_id, "/DIFFERENT", "/DIFFERENT")
    assert first.snapshot_id == again.snapshot_id
    # idempotent: original row wins, not the second caller's values
    assert again.local_path == "/local/snapshot_1.parquet"


def test_pending_push_bump_and_get(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    bumped = store.bump_pending_push_attempt(s.snapshot_id, "boom")
    assert bumped.attempts == 1 and bumped.last_error == "boom"
    assert store.get_pending_push(s.snapshot_id).attempts == 1
    assert store.bump_pending_push_attempt(999999, "x") is None


def test_pending_push_list_and_delete(store):
    p = _project(store)
    s = store.create_snapshot(p.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    assert len(store.list_pending_pushes()) == 1
    store.delete_pending_push(s.snapshot_id)
    assert store.get_pending_push(s.snapshot_id) is None
    assert store.list_pending_pushes() == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_duckdb.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'insert_pending_push'`.

- [ ] **Step 3: Add pending-push methods to the class**

Insert into `DuckDBCatalogStore` (after `snapshot_ts`, before `close`):

```python
    # ------------------------------------------------------------------ pending pushes
    def insert_pending_push(self, snapshot_id, local_path, irods_target) -> PendingPush:
        d: dict[str, Any] | None = None
        try:
            d = self._one(
                """INSERT INTO mesa.pending_pushes (snapshot_id, local_path, irods_target)
                   VALUES (?, ?, ?)
                   RETURNING snapshot_id, local_path, irods_target,
                             attempts, last_error, created_at""",
                [snapshot_id, local_path, irods_target],
            )
        except duckdb.ConstraintException:
            d = None
        if d is None:
            d = self._one(
                """SELECT snapshot_id, local_path, irods_target,
                          attempts, last_error, created_at
                   FROM mesa.pending_pushes WHERE snapshot_id = ?""",
                [snapshot_id],
            )
        assert d is not None
        return _row_to_pending_push(d)

    def delete_pending_push(self, snapshot_id) -> None:
        self._conn.execute("DELETE FROM mesa.pending_pushes WHERE snapshot_id = ?", [snapshot_id])

    def bump_pending_push_attempt(self, snapshot_id, error, *, error_max_chars=2000) -> PendingPush | None:
        truncated = error[:error_max_chars] if error else error
        d = self._one(
            """UPDATE mesa.pending_pushes
               SET attempts = attempts + 1, last_error = ?
               WHERE snapshot_id = ?
               RETURNING snapshot_id, local_path, irods_target,
                         attempts, last_error, created_at""",
            [truncated, snapshot_id],
        )
        return _row_to_pending_push(d) if d else None

    def list_pending_pushes(self, limit=100) -> list[PendingPush]:
        rows = self._all(
            """SELECT snapshot_id, local_path, irods_target,
                      attempts, last_error, created_at
               FROM mesa.pending_pushes
               ORDER BY created_at ASC, snapshot_id ASC LIMIT ?""",
            [limit],
        )
        return [_row_to_pending_push(d) for d in rows]

    def get_pending_push(self, snapshot_id) -> PendingPush | None:
        d = self._one(
            """SELECT snapshot_id, local_path, irods_target,
                      attempts, last_error, created_at
               FROM mesa.pending_pushes WHERE snapshot_id = ?""",
            [snapshot_id],
        )
        return _row_to_pending_push(d) if d else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_catalog_duckdb.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/catalog_duckdb.py tests/test_catalog_duckdb.py
git commit -m "feat(catalog): DuckDBCatalogStore pending-push WAL operations

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: `open_catalog` factory

**Files:**
- Modify: `src/mesa_ducklake/catalog.py` (add factory + helper at module end)
- Test: `tests/test_open_catalog.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_open_catalog.py`:

```python
"""open_catalog dispatches to the right backend by DSN shape."""

import pytest

import mesa_ducklake.catalog as cat
from mesa_ducklake.catalog import PostgresCatalogStore, open_catalog
from mesa_ducklake.catalog_base import CatalogStore
from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore


def test_duckdb_uri(tmp_path):
    store = open_catalog(f"duckdb:///{tmp_path / 'c.duckdb'}")
    assert isinstance(store, DuckDBCatalogStore)
    assert isinstance(store, CatalogStore)  # Protocol conformance
    store.close()


def test_duckdb_bare_path(tmp_path):
    store = open_catalog(str(tmp_path / "c.duckdb"))
    assert isinstance(store, DuckDBCatalogStore)
    store.close()


def test_memory():
    store = open_catalog(":memory:")
    assert isinstance(store, DuckDBCatalogStore)
    store.close()


def test_postgres_scheme_dispatch(monkeypatch):
    class FakeConn:
        closed = False
        autocommit = False
        def close(self):  # pragma: no cover - not exercised here
            self.closed = True
    monkeypatch.setattr(cat.psycopg, "connect", lambda dsn: FakeConn())
    store = open_catalog("postgresql://mesa@localhost/mesa_ducklake")
    assert isinstance(store, PostgresCatalogStore)


def test_blank_raises():
    with pytest.raises(ValueError):
        open_catalog("")


def test_unrecognized_raises():
    with pytest.raises(ValueError):
        open_catalog("mysql://nope/db")
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_open_catalog.py -q`
Expected: FAIL — `ImportError: cannot import name 'open_catalog'`.

- [ ] **Step 3: Add the factory to `catalog.py`**

Append to the end of `src/mesa_ducklake/catalog.py` (the `import` for the two backends goes at the **top** of the file with the other imports):

```python
# --- at top of file, with the other imports ---
from mesa_ducklake.catalog_base import CatalogStore
from mesa_ducklake.catalog_duckdb import DuckDBCatalogStore


# --- at the end of the file ---
def _duckdb_uri_to_path(uri: str) -> str:
    """Map a ``duckdb://`` URI to a filesystem path (or ``:memory:``)."""
    rest = uri[len("duckdb://"):]
    if rest in (":memory:", "/:memory:"):
        return ":memory:"
    return rest  # 'duckdb:///abs/p.duckdb' -> '/abs/p.duckdb'


def open_catalog(dsn: str) -> CatalogStore:
    """Construct the catalog backend implied by ``dsn``.

    * ``postgresql://`` / ``postgres://`` (or a libpq keyword DSN) ->
      :class:`PostgresCatalogStore`
    * ``duckdb://…`` URI, a path ending ``.duckdb``, or ``:memory:`` ->
      :class:`DuckDBCatalogStore`
    * blank / unrecognized -> ``ValueError``

    Callers that treat a blank DSN as "DuckLake disabled" must gate on that
    before calling — this factory raises on blank.
    """
    if dsn is None or not str(dsn).strip():
        raise ValueError("open_catalog requires a non-empty catalog DSN")
    s = str(dsn).strip()
    if s.startswith(("postgresql://", "postgres://")) or (
        "://" not in s and ("dbname=" in s or "host=" in s)
    ):
        return PostgresCatalogStore(s)
    if s.startswith("duckdb://"):
        return DuckDBCatalogStore(_duckdb_uri_to_path(s))
    if s == ":memory:" or s.endswith(".duckdb"):
        return DuckDBCatalogStore(s)
    raise ValueError(
        f"unrecognized catalog DSN (expected postgresql:// or duckdb://…/*.duckdb): {dsn!r}"
    )
```

Note: `catalog_duckdb.py` imports only from `models` + `irods_path`, so importing it from `catalog.py` creates no import cycle.

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_open_catalog.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/catalog.py tests/test_open_catalog.py
git commit -m "feat(catalog): open_catalog factory dispatches by DSN scheme

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Wire `DuckLakeClient` to the factory

**Files:**
- Modify: `src/mesa_ducklake/client.py` (imports, `__init__`, `_get_catalog`)
- Test: `tests/test_client_catalog_dsn.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_client_catalog_dsn.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_client_catalog_dsn.py -q`
Expected: FAIL — `TypeError: __init__() missing 1 required positional argument: 'postgres_dsn'` (current signature requires it positionally).

- [ ] **Step 3: Update `client.py`**

(a) Replace the catalog import near the top:

```python
# remove:  from mesa_ducklake.catalog import CatalogStore
from mesa_ducklake.catalog import open_catalog
from mesa_ducklake.catalog_base import CatalogStore
```

(b) Replace the `__init__` signature head and DSN resolution (the first lines of `__init__`):

```python
    def __init__(
        self,
        catalog_dsn: str | None = None,
        irods_session: Any = None,
        *,
        postgres_dsn: str | None = None,
        cache_dir: str | Path | None = None,
        cache_cap_bytes: int = DEFAULT_CACHE_CAP_BYTES,
        lake_root_override: str | Path | None = None,
    ) -> None:
        dsn = catalog_dsn if catalog_dsn is not None else postgres_dsn
        if dsn is None:
            raise TypeError(
                "DuckLakeClient requires catalog_dsn (or the legacy postgres_dsn alias)"
            )
        self._catalog_dsn = dsn
        self._postgres_dsn = dsn  # back-compat attribute for any external readers
        self._irods_session = irods_session
```

(Keep the rest of `__init__` — the `lake_root_override`/`cache_dir`/`cache_cap_bytes` handling and the `self._catalog`/`self._lakes` initialization — exactly as it is.)

(c) Replace the catalog construction in `_get_catalog`:

```python
    def _get_catalog(self) -> CatalogStore:
        if self._catalog is None:
            self._catalog = open_catalog(self._catalog_dsn)
        return self._catalog
```

- [ ] **Step 4: Run to verify it passes (new + existing client smoke)**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_client_catalog_dsn.py tests/test_smoke.py -q`
Expected: PASS. (`test_smoke.py`'s `client_fixture` uses `postgres_dsn=` keyword — still valid via the alias.)

- [ ] **Step 5: Commit**

```bash
git add src/mesa_ducklake/client.py tests/test_client_catalog_dsn.py
git commit -m "feat(client): select catalog backend via open_catalog(catalog_dsn)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: End-to-end over DuckDB (local-only)

**Files:**
- Test: `tests/test_client_e2e_duckdb.py`

- [ ] **Step 1: Write the failing test**

`tests/test_client_e2e_duckdb.py`:

```python
"""DuckLakeClient register -> record -> history/effective/diff over a DuckDB catalog.

Local-only mode (no iRODS session): Parquet lands in the local cache dir; the
catalog lives in a DuckDB file. Proves catalog + lake compose end-to-end.
"""

from datetime import UTC, datetime, timedelta

from mesa_ducklake import AvuChange, DuckLakeClient

PATH = "/iplant/home/u/proj/f.jpg"


def _change(op: str, ts: datetime) -> AvuChange:
    return AvuChange(
        irods_path=PATH, target_type="data_object",
        attribute="envo.biome", value="forest", unit="ENVO:01000228",
        op=op, ts=ts,
    )


def test_duckdb_catalog_end_to_end(tmp_path):
    client = DuckLakeClient(
        catalog_dsn=f"duckdb:///{tmp_path / 'cat.duckdb'}",
        cache_dir=tmp_path / "cache",
    )
    project = client.register_project(irods_path="/iplant/home/u/proj", actor="u", zone="iplant")

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    s1 = client.record_changes(project.project_id, "u", [_change("add", t0)], note="add biome")
    s2 = client.record_changes(
        project.project_id, "u", [_change("delete", t0 + timedelta(hours=1))], note="remove biome"
    )

    history = client.get_history(project.project_id, PATH)
    assert len(history) == 2

    effective = client.get_avus(project.project_id, PATH)
    assert effective == []  # added then deleted -> no effective AVU

    d = client.diff(project.project_id, s1.snapshot_id, s2.snapshot_id)
    assert len(d) == 1 and d[0].op == "delete"

    client.close()
```

- [ ] **Step 2: Run to verify it fails, then passes**

Run: `/Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests/test_client_e2e_duckdb.py -q`
Expected: PASS. (If it fails, the failure is the signal; do NOT modify library code to fit — the e2e exercises already-built pieces. A real failure here means a bug in Tasks 3–7 to fix at its source.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_client_e2e_duckdb.py
git commit -m "test(client): end-to-end AVU history over a DuckDB catalog

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: mesa-mcp — pass `catalog_dsn` kwarg

**Files:**
- Modify: `mesa-mcp/src/mesa_mcp/ducklake/client.py:90`

- [ ] **Step 1: Check for a test asserting the old kwarg**

Run: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-mcp && grep -rn "postgres_dsn" src tests`
Expected: identifies the single construction site in `src/mesa_mcp/ducklake/client.py` (the `client_kwargs` dict). If any test asserts `postgres_dsn`, update that assertion to `catalog_dsn` in the same step below.

- [ ] **Step 2: Change the kwarg**

In `mesa-mcp/src/mesa_mcp/ducklake/client.py`, in `get_default_client`, update the kwargs dict:

```python
    client_kwargs: dict[str, Any] = {
        "catalog_dsn": dsn,
        "irods_session": None,
        "cache_cap_bytes": config.ducklake.cache_cap_bytes,
    }
```

(Only `"postgres_dsn"` → `"catalog_dsn"` changes. The DuckLakeClient alias means even leaving it would work, but the rename keeps naming honest now that the DSN may be DuckDB.)

- [ ] **Step 3: Run mesa-mcp's DuckLake tests**

Run: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-mcp && /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest tests -k "ducklake or duck" -q`
Expected: PASS (or no tests collected → then run the broader `pytest tests -q` to confirm no regression).

- [ ] **Step 4: Commit**

```bash
cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-mcp
git add src/mesa_mcp/ducklake/client.py
git commit -m "feat(ducklake): pass catalog_dsn to DuckLakeClient (DuckDB-or-Postgres)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Full suite + lint + type-check

**Files:** none (verification)

- [ ] **Step 1: Full mesa-ducklake suite**

Run: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-ducklake && /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python -m pytest -q`
Expected: PASS (Postgres-marked tests may SKIP; everything else green).

- [ ] **Step 2: Lint changed files**

Run: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-ducklake && /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/ruff check src tests`
Expected: "All checks passed!" (fix any warnings in the files this plan created/modified).

- [ ] **Step 3: Type-check the new modules**

Run: `cd /Users/tswetnam/Desktop/mesa-ai-test/mesa-ducklake && /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/mypy src/mesa_ducklake/catalog_base.py src/mesa_ducklake/catalog_duckdb.py src/mesa_ducklake/catalog.py src/mesa_ducklake/client.py`
Expected: "Success: no issues found" (resolve any reported issues in those files).

- [ ] **Step 4: Final commit if lint/type fixes were needed**

```bash
git add -A
git commit -m "chore(catalog): lint + type-check fixes for DuckDB backend

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Post-implementation: enable + demo (not part of the TDD loop)

After the branch is green and merged (or while on the branch), to make DuckLake live for the Alcock dataset:

1. Edit `mesa-mcp/config.yaml`:
   ```yaml
   ducklake:
     catalog_dsn: duckdb:///Users/tswetnam/Desktop/mesa-ai-test/.mesa/catalog.duckdb
     data_collection: .mesa/ducklake
   ```
2. **Restart the mesa-mcp MCP server** (it reads config at startup → reconnect the server in the client). This briefly interrupts mesa-mcp tools.
3. `mesa_ducklake_init_project` on `/iplant/home/tswetnam/Alcock_leafPhenotypingImages_2016`.
4. Resume the paused Haiku metadata workflow; every `mesa_avu_apply_term` / `ds_add_avu` now mirrors into the DuckDB catalog + Parquet-in-iRODS history.
5. Verify history directly:
   ```bash
   /Users/tswetnam/Desktop/mesa-ai-test/.venv/bin/python - <<'PY'
   from mesa_ducklake import DuckLakeClient
   c = DuckLakeClient(catalog_dsn="duckdb:///Users/tswetnam/Desktop/mesa-ai-test/.mesa/catalog.duckdb")
   p = c.find_project_by_path("/iplant/home/tswetnam/Alcock_leafPhenotypingImages_2016")
   print("project:", p and p.project_id)
   print("snapshots:", len(c.list_snapshots(p.project_id)) if p else 0)
   c.close()
   PY
   ```

---

## Self-Review Notes (author check)

- **Spec coverage:** Protocol (Task 2), Postgres rename (Task 1), DuckDBCatalogStore projects/snapshots/pending (Tasks 3–5), factory + DSN rules incl. `:memory:`/triple-slash/bare-path (Task 6), client `catalog_dsn` + alias (Task 7), e2e local-only (Task 8), mesa-mcp one-line (Task 9), tests/lint/types + rollout (Task 10 + Post-impl). All spec sections mapped.
- **Single-writer tradeoff:** documented in `catalog_duckdb.py` module docstring (Task 3).
- **No placeholders:** every code step shows full content; every run step has an exact command + expected result.
- **Type consistency:** method names/signatures in `catalog_base.py` (Task 2) match `DuckDBCatalogStore` (Tasks 3–5) and the calls in `client.py` (unchanged) and the factory return type (Task 6).
