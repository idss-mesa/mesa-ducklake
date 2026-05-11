# Contributing

What this page covers: how to make a clean PR against mesa-ducklake,
the hard contracts you must respect, when to use the
`ducklake-engineer` sub-agent, and the local test loop.

## Before you start

Read [`../../CLAUDE.md`](../../CLAUDE.md) front-to-back. It documents
the project's architecture, the hard contracts, and the AVU shape
that mesa-ducklake mirrors from iRODS iCAT. Most PR review
discussion is about whether a change respects those contracts.

The hard contracts, in one paragraph: the AVU triple
`(attribute, value, unit)` is canonical and never split; `avu_changes`
is append-only (corrections are new snapshots); one
`record_changes` call equals one snapshot equals one Parquet file;
per-project Parquet files live under `<project_root>/.mesa/ducklake/`
in iRODS; `DuckLakeClient` is the only public class.

## Local development

```bash
git clone git@github.com:cyverse/mesa-ducklake.git
cd mesa-ducklake
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite:

```bash
pytest -q
```

The suite uses `pytest-postgresql` for an ephemeral Postgres per
session. Tests that need a live Postgres are marked with
`@pytest.mark.requires_postgres` and auto-skip on hosts without
`pg_ctl` reachable.

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

## When to use the `ducklake-engineer` sub-agent

The repo ships a Claude Code sub-agent at
[`../../.claude/agents/ducklake-engineer.md`](../../.claude/agents/ducklake-engineer.md).
It is the right tool for **non-trivial** changes where the project's
hard contracts are load-bearing:

- Adding a Postgres or Parquet column.
- Introducing a new migration number.
- Authoring a new time-travel query shape.
- Changing the `DuckLakeClient` public API surface.
- Anything that affects the wire contract with mesa-mcp.

Invoke it (`/agents ducklake-engineer` or the IDE's sub-agent
chooser) and describe the change. The agent reads `CLAUDE.md`,
follows the [schema change protocol](#schema-change-protocol)
below automatically, and produces a PR-ready patch.

For routine work (typo fixes, comment edits, single-file logic
changes that do not affect contracts) the sub-agent is overkill —
use the main agent or work by hand.

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
5. **Update `models.py`** to reflect the new field.
6. **Update `DuckLakeClient`** only if the new field is part of
   the public contract.
7. **Tests.** Add a migration round-trip test plus a regression
   test for every affected query.

## Test expectations

For any new feature, the test suite must cover at minimum:

- The empty-project / empty-lake case.
- One snapshot.
- N snapshots with intermediate deletes (supersede chains).
- Time-travel reads at three boundaries: before, at, and after a
  known snapshot.
- Failure paths: invalid input rejected at the model layer;
  partial Parquet write rolled back in the catalog.

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
- [`../../.claude/agents/ducklake-engineer.md`](../../.claude/agents/ducklake-engineer.md) —
  the sub-agent playbook.
