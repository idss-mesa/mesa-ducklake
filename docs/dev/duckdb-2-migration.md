# DuckDB 2.0 migration (mesa-ducklake 0.2.0)

What this page covers: which DuckDB 2.0 changes mattered to
mesa-ducklake and how each was handled. DuckDB 2.0.0 LTS was released
2026-10-21; mesa-ducklake 0.2.0 requires `duckdb>=2.0,<3`.

mesa-ducklake does not use the `ducklake` extension. It implements the
pattern itself (a catalog plus one Parquet file per snapshot), so the
upgrade only touched plain `duckdb` Python calls. The existing suite
passed unchanged on 2.0.0. The changes below remove hazards and adopt
what 2.0 adds.

| DuckDB 2.0 change | Effect here | Handling |
|---|---|---|
| A connection used from two threads, or while an earlier result is still open, raises `ResourceInUseException`. | `DuckDBCatalogStore` shared one connection, and mesa-mcp calls it from concurrent tool threads. `_one` left `fetchone()` results open. | Every statement runs on its own cursor and fully drains its result (`_run`). A concurrency test covers it. |
| A failed `execute()` aborts the transaction. | `insert_pending_push` relied on catching `ConstraintException` from a duplicate insert. | Uses `INSERT … ON CONFLICT (snapshot_id) DO NOTHING RETURNING`, then re-selects. |
| New files use storage version 69, which DuckDB 1.x cannot open. | A catalog file created by 2.0 is unreadable by hosts still on 1.x (e.g. Python 3.10 iRODS rule hosts, where pip resolves duckdb 1.x). | `duckdb:///…?storage_version=v1.5.0` sets `storage_compatibility_version` for new files. Existing files keep their version. |
| Quack client-server protocol (beta). | Lets several processes write one DuckDB catalog. | New `quack://` backend (`catalog_quack.py`) and `mesa-ducklake serve-catalog`. See [`../deploy/quack-catalog.md`](../deploy/quack-catalog.md). |
| Python client requires Python ≥ 3.11. | Already required. | None. |
| PEG parser, lambda syntax, `//`/`%` by zero, untyped `NULL`, S3 URL parameters. | No affected SQL in this repo. | None. |

## Not adopted yet: the native DuckLake 1.1 extension

DuckLake 1.1 (the `ducklake` extension in 2.0) would replace the
hand-rolled catalog with `ATTACH 'ducklake:…'`. That would bring
built-in snapshots and time travel, data inlining, and
`ducklake_merge_adjacent_files` for the deferred per-project
compaction. The obstacle is storage: DuckLake writes data files through
DuckDB file systems, and the iRODS Data Store has none, so the
push-before-commit protocol to `<project>/.mesa/ducklake/` would need a
redesign. Treat it as a separate design task.
