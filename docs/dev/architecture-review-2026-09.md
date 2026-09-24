# Architecture review — 2026-09

This is a dated record of a review of mesa-ducklake, and of how mesa-mcp uses
it, as of `main@75893d5` and mesa-mcp `main@5816120`. It lists every finding
with its severity and what happened to it: fixed in this branch, or deferred
with a reason. Once written, it isn't updated. A later review supersedes it
with a new file.

Severity: **high** means wrong or lost history is possible. **Medium** means a
contract gap or a misleading operator surface. **Low** means drift or a
papercut.

## Summary

The core design holds up. The design is:

- a catalog/lake split;
- push-before-commit writes with a write-ahead log (WAL) in the catalog;
- Parquet co-located with the data it describes;
- a single public class, `DuckLakeClient`.

The DuckDB-file catalog backend fits behind the `CatalogStore` Protocol
without leaking into callers.

The main problems were at the edges:

- documentation that still described the Postgres-only v0.1;
- a provenance rule that the model layer never enforced;
- the full LLM → mesa-mcp → DuckLake path had never been tested end to end;
- three mesa-mcp bugs that silently dropped or misplaced history metadata.

## Findings — mesa-ducklake

| # | Sev | Finding | Outcome |
|---|---|---|---|
| D1 | high | `AvuChange` accepted `actor=""` / `source=""`, although CLAUDE.md says provenance is mandatory and must be rejected at the model layer. A blank actor would be written permanently to append-only Parquet. | **Fixed.** `field_validator` rejects empty or whitespace `actor`/`source` on input (`models.py`). Rows read back from Parquet are exempt through a validation context (`lake._stored_change`), because legacy history can't be rewritten and must stay readable. Tested in `test_models.py` and `test_lake.py`. |
| D2 | medium | `mesa-ducklake migrate` failed with exit 2 on a DuckDB DSN, so operators had no single command to prepare either backend. | **Fixed.** `migrate` now bootstraps a DuckDB catalog and reports `{"applied": 0, "backend": "duckdb"}`. Backend selection was pulled out into `catalog.catalog_backend()`, which `open_catalog` reuses. Tests in `test_cli_migrate.py`. |
| D3 | low | The CLI docstring said "two verbs", and the `missing_dsn` message named only libpq DSNs. The CLI constructed its client with the deprecated `postgres_dsn=`. | **Fixed.** |
| D4 | medium | `irods_sync._checksum_matches` counts an **empty** server checksum, or an **unknown algorithm**, as a match. A zone that never computes checksums therefore skips verification without any error. | **Deferred.** The fix is to call `obj.chksum()` to have the server compute one when it is missing. But every mocked session in both repos' tests returns a `MagicMock` there, so the change needs its own PR with test updates. Until then, operators should enable server-side checksums on the zone. |
| D5 | medium | A delete matches on the exact `(attribute, value, unit)` triple. A `ds_delete_avu` whose unit differs from the add's (such as an empty unit) records a delete row that supersedes nothing, so the add stays effective. iCAT, however, has removed its own AVU. The two histories disagree. | **Deferred** (it's a design question). The e2e `delete_and_time_travel` scenario pins down the matching-triple case. |
| D6 | medium | File moves and renames don't carry AVU history. The history stays keyed to the old path, and the new path starts empty. | **Deferred.** Needs either a `move` event type (a schema change, which needs `ducklake-engineer`) or path aliasing in reads. |
| D7 | low | `list_snapshots`/`_ensure_cached` cap at 1000 snapshots per project, and no compaction exists yet. | **Deferred** (already on the roadmap). |
| D8 | medium | The two `.re` rule findings from `SECURITY_REVIEW_2026-05-11.md` are still open: `mesa_avu_change.re` builds JSON by string concatenation without escaping, and `mesa_enroll_policy.re` interpolates `*parent` into GenQuery. | **Deferred.** They have to be fixed and tested on a real iRODS server. They block deploying the rules to production. |
| D9 | medium | Code added since the May security review hasn't been reviewed: `catalog_duckdb.py`, `irods_sync.py`, `open_catalog` DSN parsing, and CLI `recover`/`migrate`. | **Deferred.** Schedule a follow-up security review. |
| D10 | low | The docs described `IrodsLakeStorage (planned)`, a Postgres-only catalog, a pre-sync write flow, a `record` stdin format the CLI no longer accepts, and `0002` as the next free migration number. `NEXT_STEPS.md` had machine-specific secrets paths. | **Fixed.** docs/ refreshed. `docs/deploy/duckdb-catalog.md` added. `NEXT_STEPS.md` rewritten. |
| D11 | low | Agent tooling relied on the deprecated "superpowers" skill convention and a subagent with stale paths. | **Fixed.** Design records moved to `docs/design/`. Project skills added in `.claude/skills/`, subagents refreshed and added in `.claude/agents/`, plus `AGENTS.md`. |
| D13 | high | `mesa-ducklake record`, the rule-callback path, builds its client with `irods_session=None` (`cli.py`). Snapshots captured by rules are committed to the catalog, but their Parquet stays only in the rule host's local cache. There cache eviction can delete it, and no other host can pull it. This breaks "metadata travels with the data" for every non-mesa-mcp write. | **Deferred.** It is documented in `docs/user/cli.md` and `docs/deploy/irods-rules.md`. The fix is to open a session from the rule host's iRODS environment, as `recover` already does. It must land before the rules are deployed to production (with D8). |
| D14 | medium | `list_snapshots`/`latest_snapshot_id` filter only the `'pending'` marker on both backends. Rows marked `'failed'` by recovery still appear, and one can become the next snapshot's `parent_snapshot`. AVU reads hide them only because no Parquet file exists for them. | **Deferred.** It needs a behaviour change on both backends (use the `add-catalog-op` skill). |
| D15 | low | The catalog opens lazily. A Postgres outage, or a DuckDB lock held by another writer, during `record`'s project lookup therefore raises a traceback instead of the documented `catalog_unreachable` JSON envelope. It still exits 1. | **Deferred.** It is documented in `docs/user/cli.md`. |
| D12 | medium | No test covered the path a real AI client takes: model → mesa-mcp → iRODS → DuckLake. mesa-mcp's tests mock both OLS and DuckLake. | **Fixed.** `tests/llm_e2e/` adds a live scripted tier and an LLM tier; see [`llm-e2e-tests.md`](./llm-e2e-tests.md). |

## Findings — mesa-mcp (fixed on `idss-mesa/mesa-mcp` branch `fix/ducklake-provenance-and-ols-config`)

| # | Sev | Finding | Outcome |
|---|---|---|---|
| M1 | high | `via_ticket` was never recorded. `ds_use_ticket` set a per-call `ContextVar`, which no later MCP call can see. | **Fixed** (`d8bdd17`). The ticket is bound to the caller's pooled session (`session.attributes["mesa.via_ticket"]`), and the mirror reads it from there. Tests prove there's no leak between identities. |
| M2 | medium | `mesa_ducklake_init_project` hard-coded `.mesa/ducklake` and ignored `ducklake.data_collection`. The collection it created and the place Parquet was written could therefore differ. | **Fixed** (`017ae25`). A residual gap is noted in that commit: an installed mesa-ducklake too old to accept `data_collection` still diverges. |
| M3 | medium | `config.yaml.example` and `.env.example` shipped an OLS base URL without `/v2`, which breaks every OLS call. | **Fixed** (`ac3eae8`), with a test that pins the examples to the code's default. |
| M4 | low | The descendant search ignored the configured OLS `base_url` because the URL was hard-coded. | **Fixed** (`ed9657e`). |
| M5 | low | `docs/superpowers/` used the same deprecated convention as this repo. | **Fixed** (`f6d59ea`). |
| M9 | medium | `OLSClient.get_term` only queried `/classes/`. Vocabularies whose terms are OWL individuals, confirmed live for ROR, returned `not_found`, so a ROR organisation could not be applied as an AVU. The e2e oracle found this. | **Fixed** (`bdcf3e7`): falls back to `/individuals/` on a 404. |
| M6 | low | `mesa_avu_apply_term` with a `curie` but no `label` fails, because there's no lookup by CURIE. | **Deferred** (feature request). The e2e scenario `apply_term_curie_without_label` pins down the current behaviour. |
| M7 | low | Attribute names drop every character outside ASCII letters, digits and spaces, so "α-helix" becomes `helix`. | **Deferred.** It is documented, and the e2e oracle checks for it. Changing it would change AVU names already in iCAT. |
| M8 | low | A mirror failure in `mesa_policy_enable`/`disable` is silently ignored, where the AVU tools report `partial_failure`. | **Deferred.** |

## What was deliberately not changed

- **No schema change.** Each deferred item that needs one (D6, and the
  alternative for D5) goes through `ducklake-engineer` and the
  `add-migration` skill in its own PR.
- **mesa-mcp CI still installs without the `ducklake` extra.** So only the new
  e2e suite exercises a real `DuckLakeClient` behind mesa-mcp. Adding the extra
  to mesa-mcp CI is a cheap follow-up.
