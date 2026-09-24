# Design records

What this directory holds: design records for mesa-ducklake. These are
decisions with lasting value, such as a new catalog backend, a schema
direction, or a change to the write protocol. Each record keeps the
context, the options that were weighed, and the reason for the choice.
Treat them as history. The code and the docs under `docs/dev/` and
`docs/deploy/` describe the current state. When a record and the code
disagree, the code wins, and the record should carry a short drift
note.

| Record | Status |
|---|---|
| [`2026-06-07-duckdb-catalog-backend-design.md`](./2026-06-07-duckdb-catalog-backend-design.md) | Implemented (drift noted: `project_id` stored as `TEXT`) |
| [`plans/2026-06-07-duckdb-catalog-backend.md`](./plans/2026-06-07-duckdb-catalog-backend.md) | Implementation plan for the above; historical |

See also the point-in-time review
[`../dev/architecture-review-2026-09.md`](../dev/architecture-review-2026-09.md).

## Where plans come from now

These files used to live under `docs/superpowers/`. That layout came
from a "superpowers" skill convention. Plans carried a
`REQUIRED SUB-SKILL` header and were executed with the
`superpowers:writing-plans`, `superpowers:executing-plans` and
`superpowers:subagent-driven-development` skills. **That convention is
deprecated in this repo.** Those skills are not installed here, and
new documents should not reference them.

New implementation plans come from Claude Code **plan mode**, together
with the project subagents and skills in [`../../.claude/`](../../.claude/):

- Agents: `ducklake-engineer`, `contract-reviewer`, `docs-auditor` and
  `llm-e2e-triage`.
- Skills: `add-migration`, `add-catalog-op`, `run-llm-e2e` and
  `docs-sync`.

Most plans are working material and should not be committed. Commit a
plan or spec here only when it has **lasting design value**, meaning a
future contributor would need it to understand why the code is shaped
the way it is. If you commit one, write it as a design record (template
below) rather than as a task checklist.

## Design-record template

File name: `YYYY-MM-DD-<short-slug>.md`. Put any accompanying
implementation plan in `plans/` under the same date and slug.

```markdown
# <Title>

- **Status:** Proposed | Accepted | Implemented | Superseded by <link>
- **Date:** YYYY-MM-DD
- **Author:** <name> (with Claude Code, if applicable)

## Context

What problem prompted this, and which constraints apply? Cite the
CLAUDE.md contracts involved: canonical AVU triple, append-only, one
snapshot per record_changes, mandatory provenance, DuckLakeClient as
the only public API, and Postgres/DuckDB backend parity.

## Decision

What we are doing, stated concretely: the modules, tables and public
API touched.

## Alternatives

The other options considered, and why each was rejected.

## Consequences

What becomes easier or harder. Cover migrations, docs to update, test
coverage, and operator impact.

## Status

The current state. Append drift notes here when the implementation
diverges from the decision.
```
