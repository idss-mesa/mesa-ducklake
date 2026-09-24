---
name: add-catalog-op
description: Use when adding, renaming or changing the signature or behavior of a catalog method. These are the methods DuckLakeClient or irods_sync call on the catalog, such as register_project, list_snapshots or the pending-push operations. The change must land in the CatalogStore Protocol and in BOTH the Postgres and DuckDB backends, with parity tests. Do not use it for pure schema changes; use add-migration for those, alongside this skill if both apply.
---

# Add or change a catalog operation

The catalog is a Protocol with two implementations, and they must stay
at parity. `DuckLakeClient` and `irods_sync` depend only on the
Protocol. See `docs/dev/architecture.md`.

| File | Role |
|---|---|
| `src/mesa_ducklake/catalog_base.py` | `CatalogStore` Protocol (`@runtime_checkable`) |
| `src/mesa_ducklake/catalog.py` | `PostgresCatalogStore` (psycopg, `%s` placeholders, `dict_row`), `open_catalog` |
| `src/mesa_ducklake/catalog_duckdb.py` | `DuckDBCatalogStore` (`?` placeholders, `_one` / `_all` helpers) |

## Checklist

1. **Protocol first.** Add or change the method signature in
   `CatalogStore` (`catalog_base.py`). Keep keyword-only flags (for
   example `*, include_pending: bool = False`) identical across the
   Protocol and both backends.
2. **Postgres.** Implement the method in `PostgresCatalogStore`.
   - Queries must be parameterized. Never use f-strings for values.
     An f-string is acceptable only for fixed SQL fragments chosen
     in code.
   - Return Pydantic models from `models.py`, not rows.
   - Missing-row semantics must match the other methods: `get_*`
     returns `None`, and update-style methods raise `KeyError`.
3. **DuckDB.** Implement the same method in `DuckDBCatalogStore`.
   - Bind `project_id` as `str(project_id)`; it is `TEXT` there.
   - Preserve the same filtering (for example `parquet_file <> 'pending'`)
     and the same ordering and `LIMIT` semantics as Postgres.
   - Remember that the DuckDB backend has no foreign keys. If
     Postgres relies on `ON DELETE CASCADE`, reproduce the effect
     explicitly or document the difference.
4. **Update the Protocol conformance test.** Add the method name to
   `_Complete` in `tests/test_catalog_base.py`. That test asserts that
   a class with every Protocol method satisfies `isinstance(..., CatalogStore)`
   and that a partial class does not.
5. **Behavior tests on both backends.**
   - `tests/test_catalog.py`, marked `@pytest.mark.requires_postgres`.
   - `tests/test_catalog_duckdb.py`, which needs no server.
   - Cover the same cases in both: happy path, missing row, and any
     sentinel handling (`'pending'`, `'failed'`).
6. **If a new call site uses it:** update `client.py` or `irods_sync.py`,
   and add an end-to-end test in `tests/test_client_e2e_duckdb.py`
   (always runs) and `tests/test_client_e2e.py` (Postgres).
7. **Docs.** Update `docs/dev/architecture.md` if the write or recover
   flow changes. Update `docs/user/usage.md` only if the public
   `DuckLakeClient` surface changes.
8. **Verify:**

   ```bash
   ruff check src/ tests/ && mypy src/
   pytest -q tests/test_catalog_base.py tests/test_catalog_duckdb.py tests/test_open_catalog.py
   MESA_DUCKLAKE_TEST_PG_HOST=localhost pytest -q -m requires_postgres tests/test_catalog.py
   ```

   Confirm that the Postgres tests actually ran, meaning they did not
   all skip.
