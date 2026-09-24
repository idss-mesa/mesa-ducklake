# Contributing

What this page covers: how to make a clean PR against mesa-ducklake,
the hard contracts you must respect, the local test loop, and the
Claude Code agents and skills that ship with the repo.

## Before you start

Read [`../../CLAUDE.md`](../../CLAUDE.md) front-to-back. It documents
the project's architecture, the hard contracts, and the AVU shape
that mesa-ducklake mirrors from iRODS iCAT. Most PR review
discussion is about whether a change respects those contracts. If you
use a different coding agent (Codex, opencode, goose and so on), it
should read [`../../AGENTS.md`](../../AGENTS.md), a short
vendor-neutral summary of the same rules and commands.

The hard contracts, in one paragraph: the AVU triple
`(attribute, value, unit)` is canonical and never split; `avu_changes`
is append-only (corrections are new snapshots); one
`record_changes` call equals one snapshot equals one Parquet file;
per-project Parquet files live under `<project_root>/.mesa/ducklake/`
in iRODS; every `AvuChange` carries non-empty `actor` and `source`;
`DuckLakeClient` is the only public class; the Postgres and DuckDB
catalog backends stay at parity.

## Local development

```bash
git clone git@github.com:idss-mesa/mesa-ducklake.git
cd mesa-ducklake
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest -q
```

Tests that need Postgres are marked `@pytest.mark.requires_postgres`.
They **skip themselves** when no server is reachable, so check the
skip count and not just the exit status. There are two ways to run
them:

```bash
# 1. pytest-postgresql starts an ephemeral cluster (needs pg_ctl on PATH)
pytest -q -m requires_postgres

# 2. point at a server you already run (what CI does)
docker run --rm -d -p 5432:5432 \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=mesa_test postgres:16
MESA_DUCKLAKE_TEST_PG_HOST=localhost pytest -q -m requires_postgres
```

Overrides: `MESA_DUCKLAKE_TEST_PG_{PORT,USER,PASSWORD,DBNAME}`
(defaults `5432` / `postgres` / `postgres` / `mesa_test`). Each test
still gets its own database.

The DuckDB-catalog tests (`tests/test_catalog_duckdb.py`,
`tests/test_client_e2e_duckdb.py`, `tests/test_open_catalog.py`) need
no server and always run.

Run a single test with `pytest tests/test_cli.py::test_name -q`.

### Live and LLM end-to-end tests

`tests/llm_e2e/` holds two **opt-in** tiers that never run in the
default `pytest -q`:

- `live_e2e` drives mesa-mcp and mesa-ducklake against a real iRODS
  server with scripted tool calls.
- `llm_e2e` has a real LLM drive the same scenarios through
  mesa-mcp's tools.

Setup, environment variables and result triage are covered in
[`llm-e2e-tests.md`](./llm-e2e-tests.md). The `run-llm-e2e` skill and
the `llm-e2e-triage` agent automate the loop (see below).

Lint and type-check:

```bash
ruff check src/ tests/
mypy src/
```

Code style is enforced by `ruff` (line length 100, target
`py311`, rule set `E F I W B UP`). The CI gate runs both `ruff
check` and `pytest -q`; PRs that fail either are not reviewed
until they are green.

## PR conventions

- **One change per PR.** A schema migration, a new query shape,
  and a CLI tweak are three PRs, not one. This keeps review
  focused and reverts cheap.
- **Title format.** Imperative, present tense, scoped:
  `client: add diff endpoint`, `migrations: 0002 soft-delete
  projects`, `lake: stream rows for large snapshots`. No emoji.
- **Description must answer "why".** Code says what; the PR
  description says why. Link to a CLAUDE.md section if a hard
  contract is involved.
- **Touch only the files you mean to.** If a refactor is
  necessary, do it in its own PR first.
- **Update docs in the same PR.** A public API change without
  a `docs/user/` update is incomplete. A schema change without
  a `docs/dev/schema.md` update is incomplete.
- **No dependencies added casually.** Adding a runtime
  dependency requires a justification in the PR description.
  Avoid heavyweight stacks (Django, Flask, requests-heavy).
- **Migration sequencing.** If your PR adds a migration, call
  out the new number in the PR description so reviewers can
  spot conflicts with parallel work.

## Claude Code agents and skills

The repo ships project-scoped Claude Code subagents in
[`../../.claude/agents/`](../../.claude/agents/) and skills in
[`../../.claude/skills/`](../../.claude/skills/). They load
automatically when you run `claude` from the repo root.

**Agents.** To use one, ask for it by name, for example "use the
contract-reviewer agent to review my diff", or @-mention it (type
`@` and pick the agent from the list). Claude Code also delegates on its own
when a task matches an agent's description. `/agents` lists and edits
them; it does not run them.

| Agent | Use it for |
|---|---|
| `ducklake-engineer` | Non-trivial changes where hard contracts are load-bearing: new catalog or Parquet columns, new migrations, new time-travel query shapes, `DuckLakeClient` API changes, anything touching the mesa-mcp wire contract. |
| `contract-reviewer` | Read-only review of a diff or branch against the hard contracts (AVU triple, append-only, one snapshot per call, provenance, public API, backend parity, frozen migrations, docs updated). Run it before opening a PR. |
| `docs-auditor` | Finding docs that contradict the code, with file:line evidence and a suggested fix. Run it after a refactor or before a release. |
| `llm-e2e-triage` | Classifying failures in a `.llm-e2e-results/<run>/` directory as a model-behavior issue, a mesa-mcp bug, a mesa-ducklake bug, an external flake or a harness bug. |

**Skills.** Skills are checklists that Claude loads when a task
matches them. You can also invoke one directly with `/<skill-name>`.

| Skill | Use it when |
|---|---|
| `add-migration` | Changing the catalog schema. It covers the Postgres migration, the DuckDB `_SCHEMA_STATEMENTS` twin, models, docs and tests. |
| `add-catalog-op` | Adding or changing a method on the `CatalogStore` Protocol and both backends. |
| `run-llm-e2e` | Running the `live_e2e` / `llm_e2e` tiers and triaging the results. |
| `docs-sync` | After any code change, to find and update the docs that describe the touched modules. |

For routine work (typo fixes, comment edits, single-file logic
changes that do not affect contracts), the agents are overkill.

Design records (the "why" behind larger changes) live in
[`../design/`](../design/README.md). New implementation plans are
produced in Claude Code plan mode. Commit one only when it carries
lasting design value; the design README explains the convention.

## Schema change protocol

This is the same checklist the `ducklake-engineer` agent follows.
If you change a schema by hand, follow it explicitly.

1. **Write the *why* first.** Add a header comment to the new
   migration explaining the user-facing problem the change
   solves.
2. **Create a new numbered migration.** Never edit an existing
   one. See [`adding-migrations.md`](./adding-migrations.md).
3. **If you are adding a column to `avu_changes`:** answer in the
   migration comment why the existing AVU triple cannot carry
   the information. The default answer is "use the AVU itself" —
   make the case for the column explicitly.
4. **Backward compatibility.** Old Parquet files must remain
   readable. Use nullable columns and default values. Never
   reorder.
5. **Mirror catalog changes in the DuckDB backend** by appending
   idempotent statements to `_SCHEMA_STATEMENTS` in
   `src/mesa_ducklake/catalog_duckdb.py`.
6. **Update `models.py`** to reflect the new field.
7. **Update `DuckLakeClient`** only if the new field is part of
   the public contract.
8. **Tests.** Add a migration round-trip test for each backend,
   plus a regression test for every affected query.
9. **Docs.** Update [`schema.md`](./schema.md).

## Test expectations

For any new feature, the test suite must cover at minimum:

- The empty-project / empty-lake case.
- One snapshot.
- N snapshots with intermediate deletes (supersede chains).
- Time-travel reads at three boundaries: before, at, and after a
  known snapshot.
- Failure paths: invalid input rejected at the model layer; a
  failed Parquet write rolled back in the catalog (local-only mode);
  a failed push leaving a `pending` row plus WAL row for recovery
  (sync mode).
- Both catalog backends, when the change touches the catalog.

Use `tmp_path`-rooted DuckLakes for tests; never share lake
state between tests.

## Reporting back

When the `ducklake-engineer` agent (or you) finish a change,
include in the PR description:

- Files created or edited.
- New migration number (if any) and what it does.
- New columns or query shapes, with rationale.
- Test command run and result.
- Anything reviewers should pay extra attention to (especially
  migration sequencing and wire-contract changes).

## See also

- [`architecture.md`](./architecture.md) — the module map.
- [`schema.md`](./schema.md) — current schema reference.
- [`adding-migrations.md`](./adding-migrations.md) — the
  numbering rule and the bookkeeping table bootstrap.
- [`queries.md`](./queries.md) — the canonical query shape any
  new read path must mirror.
- [`llm-e2e-tests.md`](./llm-e2e-tests.md) — the opt-in live and
  LLM end-to-end tiers.
- [`architecture-review-2026-09.md`](./architecture-review-2026-09.md) —
  point-in-time architecture review.
- [`../../.claude/agents/`](../../.claude/agents/) and
  [`../../.claude/skills/`](../../.claude/skills/) — the agent and
  skill definitions.
- [`../../AGENTS.md`](../../AGENTS.md) — vendor-neutral agent
  instructions.
