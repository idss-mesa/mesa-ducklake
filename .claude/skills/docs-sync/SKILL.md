---
name: docs-sync
description: Use after changing code, CLI behavior, schema, migrations, config or deployment files in mesa-ducklake, and before committing. It maps each changed module to the docs that describe it, so those docs can be checked and updated in the same change. Also use it when asked "which docs need updating?". For a whole-repo audit that is not tied to a diff, use the docs-auditor agent instead.
---

# Keep docs in sync with a code change

1. List what changed:

   ```bash
   git status --short
   git diff --name-only main...HEAD
   ```

2. For each changed path, open **every** doc mapped below. Check each
   statement the change could have invalidated: signatures, defaults,
   exit codes, JSON shapes, file names, migration numbers, and
   examples.

| Changed path | Docs to check |
|---|---|
| `src/mesa_ducklake/client.py` | `docs/user/usage.md`, `docs/user/time-travel.md`, `docs/dev/architecture.md` (control flow), `README.md` (Public API), `CLAUDE.md` (API sketch) |
| `src/mesa_ducklake/__init__.py` | `docs/user/usage.md` (public surface), `docs/dev/architecture.md` (module map) |
| `src/mesa_ducklake/models.py` | `docs/dev/schema.md`, `docs/user/usage.md` (AvuChange rules), `docs/user/cli.md` (stdin fields) |
| `src/mesa_ducklake/catalog_base.py`, `catalog.py`, `catalog_duckdb.py` | `docs/dev/architecture.md`, `docs/dev/schema.md` (DuckDB differences), `docs/deploy/duckdb-catalog.md`, `docs/deploy/postgres.md`, `docs/user/usage.md` (DSN table) |
| `src/mesa_ducklake/schema.py`, `migrations/*.sql` | `docs/dev/schema.md`, `docs/dev/adding-migrations.md` (next number), `docs/deploy/postgres.md` (expected `applied` count) |
| `src/mesa_ducklake/lake.py` | `docs/dev/schema.md` (Parquet columns), `docs/dev/architecture.md`, `docs/deploy/per-project-storage.md` |
| `src/mesa_ducklake/queries.py`, `time_travel.py` | `docs/dev/queries.md`, `docs/user/time-travel.md` |
| `src/mesa_ducklake/irods_sync.py` | `docs/dev/architecture.md` (write protocol, crash recovery), `docs/dev/schema.md` (sentinels), `docs/deploy/backup.md` |
| `src/mesa_ducklake/cache.py` | `docs/dev/architecture.md` (local cache), `docs/user/usage.md` (`cache_dir`, `cache_cap_bytes`) |
| `src/mesa_ducklake/irods_path.py` | `docs/deploy/per-project-storage.md` (`data_collection`) |
| `src/mesa_ducklake/cli.py` | `docs/user/cli.md` (verbs, stdin, exit-code table), `docs/deploy/irods-rules.md`, `README.md` (CLI), `CLAUDE.md` (Commands) |
| `irods-rules/*` | `docs/deploy/irods-rules.md`, `irods-rules/README.md`, `docs/user/cli.md` |
| `deploy/*` | `docs/deploy/backup.md` |
| `pyproject.toml` (deps, extras, scripts) | `docs/user/usage.md` (Installation), `docs/dev/contributing.md`, `AGENTS.md`, `CLAUDE.md` (Dependencies) |
| `tests/conftest.py`, `tests/_pg_env.py`, `.github/workflows/*` | `README.md` (Postgres-backed tests), `docs/dev/contributing.md`, `AGENTS.md` |
| `tests/llm_e2e/*`, `deploy/llm-e2e/*` | `docs/dev/llm-e2e-tests.md`, `.claude/skills/run-llm-e2e/SKILL.md` |
| `.claude/agents/*`, `.claude/skills/*` | `docs/dev/contributing.md` (agents and skills tables), `CLAUDE.md` (Working with this repo), `docs/design/README.md` |

3. Also update `NEXT_STEPS.md` if the change closes or opens an item.
4. Keep each doc's voice and structure, and edit rather than rewrite.
   Do not document behavior the code doesn't have. If code and doc
   disagree and you are not changing the code, fix the doc.
5. Check that links still resolve. For example:

   ```bash
   grep -rn "](\./\|](\.\./" docs README.md | head
   ```
