# AGENTS.md

Instructions for coding agents (Codex, opencode, goose, Claude Code and
others). [`CLAUDE.md`](CLAUDE.md) has the full architecture, schema and
rationale; read it for anything non-trivial.

## What this repo is

`mesa-ducklake` is a Python library (3.11+) that records the
**history of iRODS AVU (attribute/value/unit) metadata** for MESA-enabled
projects. The data is split in two:

- A **catalog** (Postgres, or a single-writer DuckDB file) indexes
  projects and snapshots.
- Each snapshot's AVU changes are one **Parquet** file, pushed into the
  project's own iRODS collection (`<project>/.mesa/ducklake/`).

Its main consumer is [idss-mesa/mesa-mcp](https://github.com/idss-mesa/mesa-mcp).

## Commands

```bash
pip install -e ".[dev]"                          # install (dev extras)
pytest -q                                        # full suite (check the skip count)
pytest tests/test_cli.py::test_name -q           # single test
ruff check src/ tests/                           # lint (CI gate)
mypy src/                                        # types (advisory)

# Postgres-backed tests (skip silently without a server):
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16
MESA_DUCKLAKE_TEST_PG_HOST=localhost pytest -q -m requires_postgres

# Opt-in end-to-end tiers (need real iRODS, and an LLM endpoint for llm_e2e):
pip install -e ".[dev,llm-e2e]" && pip install -e "../mesa-mcp[ducklake]"
pytest -m live_e2e tests/llm_e2e -v
pytest -m llm_e2e  tests/llm_e2e -v

# CLI (needs MESA_DUCKLAKE_DSN = postgresql://… or duckdb:///abs/path.duckdb):
mesa-ducklake migrate
mesa-ducklake recover
mesa-ducklake record < change.json
```

## Hard contracts

- **The AVU triple `(attribute, value, unit)` is canonical.** Never
  split, merge or rename it, and never partition queries on less than
  the full triple.
- **Append-only.** Never rewrite Parquet files or delete snapshot rows.
  Corrections are new snapshots.
- **One `record_changes` call = one snapshot = one Parquet file.**
- **Provenance is mandatory.** Every `AvuChange` has a non-empty
  `actor` and `source`.
- **`DuckLakeClient` is the only public Python API.** The
  `mesa-ducklake` CLI is the only non-Python interface.
- **Backend parity.** Every catalog operation and schema change goes
  into both the Postgres backend (`catalog.py` + a new
  `migrations/NNNN_*.sql`) and the DuckDB backend (`catalog_duckdb.py`
  `_SCHEMA_STATEMENTS`).
- **Migrations are frozen once committed.** Changes are new numbered
  files. Update docs in the same change.

## Where things live

- `src/mesa_ducklake/`: the library (`client.py` is the facade).
  `migrations/`: Postgres DDL. `irods-rules/`: iRODS rule callbacks.
  `deploy/`: backup timer.
- `docs/user/`: API, CLI and time travel. `docs/dev/`: architecture,
  schema, migrations, contributing, e2e tests. `docs/deploy/`:
  Postgres, the DuckDB catalog, backup, iRODS rules, storage.
  `docs/design/`: design records.
- `NEXT_STEPS.md`: current status and open issues.
- `.claude/agents/`, `.claude/skills/`: Claude Code reviewers and
  checklists. The checklists in `.claude/skills/*/SKILL.md`
  (add-migration, add-catalog-op, run-llm-e2e, docs-sync) are plain
  Markdown and useful to any agent.
