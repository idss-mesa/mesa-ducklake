# Live LLM e2e tests

This page covers the opt-in test suite in `tests/llm_e2e/`. It drives the real
[mesa-mcp](https://github.com/idss-mesa/mesa-mcp) MCP server, using a
self-hosted model served by vLLM or LiteLLM. The suite exercises every ontology
endpoint mesa-mcp exposes and checks how the resulting AVUs are recorded in
mesa-ducklake. The page explains what the suite proves, how to run it, and how
to add a scenario.

## What it tests

```
self-hosted LLM ──tool calls──▶ mesa-mcp (stdio) ──▶ EBI OLS4
   (litellm)                        │
                                    ├──▶ iRODS AVU write (live Data Store)
                                    └──▶ DuckLakeClient.record_changes
                                             ├─ catalog (Postgres or DuckDB)
                                             └─ Parquet ─▶ <project>/.mesa/ducklake/
                                                              │
      verify ◀── fresh DuckLakeClient, empty cache ◀──────────┘ (pulled from iRODS)
             ◀── python-irodsclient (iCAT AVUs)
```

Each scenario runs in two tiers:

| Tier | Marker | Needs | Proves |
|---|---|---|---|
| scripted | `live_e2e` | iRODS, OLS, a catalog | The endpoint and the DuckLake pipeline work, with no model involved. |
| llm | `llm_e2e` | the above + a model | A model given only a natural-language task and a small set of tools leaves the same end state. |

Both tiers use the same `verify`. It checks only the **end state**, never the
model's wording:

- **The AVU triple.** The attribute, value and unit match an independent
  *oracle*: OLS4 queried directly, with the documented rule
  `attribute = <ontology_id>.<snake(label)>` and `unit = CURIE` applied.
- **Provenance.** `op`, `source` (`mesa-mcp:<tool>`), `actor`, `target_type`,
  and `via_ticket` for ticket-mediated changes.
- **Snapshot count.** Each scenario states how many snapshots it should
  create. A read-only tool must create **zero**, and a batch must create
  exactly one.
- **iRODS and DuckLake agree.** The live iCAT AVU set on every path the
  scenario touched equals `DuckLakeClient.get_avus` on the same path.
- **The history is really in iRODS.** The checker opens its own
  `DuckLakeClient` with an empty cache, so every Parquet file it reads is
  pulled from `<project>/.mesa/ducklake/` in iRODS.

The LLM tier skips a scenario when the scripted tier failed that scenario in
the same run. A pipeline bug is therefore never counted as a model failure.

## Scenario coverage

| Scenario | Endpoint(s) | Key assertion |
|---|---|---|
| `ols_ontologies` | `mesa_ols_list_ontologies`, `mesa_ols_get_ontology` | no snapshot |
| `ols_search` | `mesa_ols_search_terms` (plain, per-ontology, `descendants_of`) | result has the expected term IRI; no snapshot |
| `ols_term_details` | `mesa_ols_get_term`, `…_get_term_hierarchy`, `…_generate_template` | label and CURIE equal the oracle's |
| `avu_from_term` | `mesa_avu_from_term` | computed AVU equals the oracle's; no snapshot |
| `apply_term_<ont>` | `mesa_avu_apply_term` on ENVO, GO, ChEBI, UBERON, PATO, NCBITaxon, UO, OBI, ROR | triple, provenance, 1 snapshot, iRODS equals DuckLake |
| `apply_term_envo_collection` | same, on a collection | `target_type=collection` |
| `apply_term_choice` | `mesa_avu_apply_term` with no IRI: the multi-round-trip (MRTR) `term_choice` elicitation | the chosen term's triple is recorded |
| `apply_term_curie_without_label` (scripted only) | `mesa_avu_apply_term` | `invalid_argument`, nothing written |
| `apply_term_unicode_value` | `mesa_avu_apply_term` | a non-ASCII value (`°`, `±`) is stored byte-for-byte |
| `batch_add_avus` | `ds_add_avus` | 3 rows, **one** snapshot |
| `delete_and_time_travel` | `mesa_avu_apply_term`, `ds_delete_avu` | history is add then delete; `get_avus_as_of` shows the AVU at the add's timestamp; `diff` contains the delete |
| `datacite_apply` | `mesa_datacite_template`, `mesa_avu_apply_datacite` | one snapshot, empty unit, DataCite source |
| `policy_toggle` | `mesa_policy_enable`/`_disable` | mirrored as add then delete |
| `ticket_provenance` | `ds_use_ticket`, `ds_add_avu` | `via_ticket` is populated |

If OLS cannot resolve a term the oracle needs, that scenario is *skipped*
with the reason; it is not failed. The ROR entry depends on OLS carrying ROR.

## Safety

The LLM tier gives a model write access to a live Data Store. Three guards
contain it:

1. **Sandbox.** Every run creates `<MESA_E2E_IRODS_ROOT>/mesa-e2e-<run>-<rand>/`,
   enables MESA on it through `mesa_ducklake_init_project`, and deletes it at
   the end. Set `MESA_E2E_KEEP=1` to keep it for inspection.
2. **Tool allow-list.** A scenario exposes only the tools it lists. The
   tools in `llm_agent.NEVER_EXPOSE` (file delete, move and write,
   ACL changes, rule execution, ticket admin) are never exposed.
3. **Path guard.** Every model tool call is checked before it reaches mesa-mcp.
   Any path argument outside the sandbox, including `..` escapes, is rejected
   and returned to the model as an error. Resource and user AVUs are rejected
   too. `test_harness_unit.py` covers the guard in the default suite.

Use a dedicated catalog. The default is a throwaway DuckDB file. If you use
Postgres, create a database just for e2e runs; it must never be a production
catalog, because rows there are append-only.

## Running it

### 1. Install

```bash
pip install -e ".[dev,llm-e2e]"
pip install -e "../mesa-mcp[ducklake]"   # mesa-mcp is not on PyPI
```

The mesa-ducklake from this checkout must be the one mesa-mcp imports:
install mesa-mcp *after* it, into the same environment.

### 2. Configure

| Variable | Required | Meaning |
|---|---|---|
| `MESA_E2E_IRODS_ROOT` | yes | A writable collection the sandbox is created under, e.g. `/iplant/home/<you>/e2e`. |
| iRODS credentials | yes | `MESA_MCP_IRODS_USER` + `MESA_MCP_IRODS_PASSWORD` (and `MESA_MCP_IRODS__HOST`/`__ZONE` if not CyVerse), **or** an `iinit`-ed `~/.irods/irods_environment.json`. The harness and mesa-mcp use the same source. |
| `MESA_E2E_CATALOG_DSN` | no | `postgresql://…` (run `MESA_DUCKLAKE_DSN=… mesa-ducklake migrate` first) or `duckdb:///abs/path.duckdb`. Default: a temp DuckDB file. |
| `LLM_MODEL` | llm tier | A litellm model string, e.g. `hosted_vllm/qwen3-8b`, `openai/e2e-tools`, `openai/js2/gpt-oss-120b`, `openai/carc-tools`. |
| `LLM_BASE_URL` | llm tier | The OpenAI-compatible base URL (`…/v1` for vLLM; the proxy root for LiteLLM). |
| `LLM_API_KEY` | depends | The LiteLLM master key or the AI Verde key. |
| `LLM_TEMPERATURE`, `LLM_MAX_TURNS`, `LLM_TIMEOUT` | no | Defaults `0`, `12`, `300`. |
| `LLM_REPEATS` | no | Runs each LLM scenario N times; all N must pass (pass@k). |
| `MESA_E2E_TOOL_TIMEOUT` | no | Seconds allowed for one MCP tool call (default `300`). A call can include a Parquet upload to the Data Store, which occasionally stalls. |
| `MESA_E2E_MESA_MCP_CMD` | no | The mesa-mcp executable (default `mesa-mcp` on `PATH`). |
| `MESA_E2E_KEEP`, `MESA_E2E_RESULTS_DIR`, `MESA_E2E_RUN_ID` | no | Keep the sandbox; artifact root (default `.llm-e2e-results/`); run label. |

### Model endpoints

- **Local vLLM + LiteLLM.** Start the stack with `cd deploy/llm-e2e && cp .env.example .env && docker compose up -d`,
  then set `LLM_MODEL=openai/e2e-tools`, `LLM_BASE_URL=http://localhost:4000`
  and `LLM_API_KEY=<master key>`. vLLM must run with
  `--enable-auto-tool-choice` and a `--tool-call-parser` for the model family,
  or tool calls come back as plain text and every LLM scenario fails with
  "required tool … was never called".
- **CARC LiteLLM (sparky-2).** This is reachable only from inside the Jetstream2
  k3s cluster (`http://litellm.carc-external.svc.cluster.local:8000/v1`) or
  through a tunnel. Use a tool-capable alias such as `openai/carc-tools`.
- **AI Verde** (`https://llm-api.cyverse.ai/v1`). It supports Chat Completions
  only, which is all the harness uses. Use a per-user key, and a model such as
  `openai/js2/gpt-oss-120b`.

### 3. Run

```bash
# Scripted only (no model), then both tiers in ONE process:
pytest -m live_e2e tests/llm_e2e -v -rs
pytest -m "live_e2e or llm_e2e" tests/llm_e2e -v -rs

# One scenario, both tiers:
pytest -m "live_e2e or llm_e2e" tests/llm_e2e -k apply_term_envo -v
```

Run both tiers in the **same** pytest invocation. The LLM tier only
avoids blaming the model for pipeline bugs if it can see that run's scripted
results.

Without `MESA_E2E_IRODS_ROOT`, both tiers skip. That is what happens in
`pytest -q` and in the normal CI. The harness's own unit tests
(`test_harness_unit.py`) always run.

`.github/workflows/llm-e2e.yml` runs the same suite manually
(`workflow_dispatch`) on a self-hosted runner labelled `llm-e2e`.

## Reading the results

```
.llm-e2e-results/<run_id>/
  report.json               # meta (model, backend, sandbox) + per-scenario verdicts
  scripted/<scenario>.jsonl # every tool call + result, then a "verdict" event
  llm/<scenario>-<n>.jsonl  # prompt, every assistant turn, tool calls, elicitations, verdict
  mesa-mcp.stderr.log       # server logs from every spawned mesa-mcp
```

pytest also prints a PASS/FAIL table at the end. For failures, ask Claude Code to
use the `llm-e2e-triage` agent on the run directory. It sorts each failure into
one of five kinds, with evidence:

- model behaviour
- a mesa-mcp bug
- a mesa-ducklake bug
- external flakiness (OLS or iRODS)
- a harness bug

## Adding a scenario

Scenarios are data in `tests/llm_e2e/scenarios.py`:

```python
Scenario(
    id="my_case",
    prompt=lambda ctx: f"… {ctx.paths['file']} …",   # what the model is asked
    tools={...},                  # exposed to the model (never NEVER_EXPOSE ones)
    required_tools={...},         # the model must call at least these
    steps=[Step("tool", lambda ctx: {...})],          # the scripted equivalent
    verify=my_verify,             # end-state checks -> list of failure strings
    snapshot_delta=1,             # exact number of new snapshots
    terms=[("envo", IRI)],        # oracle lookups to pre-fetch (ctx.term(...))
)
```

`ctx.paths` gives each (tier, scenario, attempt) its own `file`, `coll` and
`dir` inside the sandbox, so scenarios never see each other's AVUs. Reuse
the helpers `_has_triple`, `_last_row_provenance` and `_mirrored`. Put a
new helper in `harness/checks.py` only if more than one scenario needs it.

## Known limitations

- **One mesa-mcp process per scenario.** This is deliberate: a DuckDB catalog is
  single-writer and mesa-mcp holds the lock for its lifetime. Reads happen only
  after the process exits, which also makes the harness work with Postgres.
- **ASCII-only attribute names.** mesa-mcp's attribute rule drops every
  character outside ASCII letters, digits and spaces. A label such as
  "α-helix" loses letters. The oracle mirrors the documented rule, so a change
  to it shows up as a failure.
- **Moves and renames.** File moves and renames through mesa-mcp don't carry
  AVU history to the new path. This isn't covered; see
  [`architecture-review-2026-09.md`](./architecture-review-2026-09.md).
