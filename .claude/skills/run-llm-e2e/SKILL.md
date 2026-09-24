---
name: run-llm-e2e
description: Use when asked to run, rerun or debug the live end-to-end tests (pytest marker live_e2e) or the LLM-driven end-to-end tests (marker llm_e2e) under tests/llm_e2e/. These tests exercise mesa-mcp and mesa-ducklake against real iRODS and a real LLM endpoint. The skill also covers triaging the results in .llm-e2e-results/. Not for the default unit suite (plain pytest -q).
---

# Run the live / LLM end-to-end tiers

Both tiers are **opt-in**. They are skipped unless their marker is
selected and their environment is set. Full details:
`docs/dev/llm-e2e-tests.md`.

## 1. Install

From the mesa-ducklake repo root, with mesa-mcp cloned as a sibling:

```bash
pip install -e ".[dev,llm-e2e]"
pip install -e "../mesa-mcp[ducklake]"
```

## 2. Environment

| Variable | Meaning |
|---|---|
| `LLM_MODEL` | Model id for the `llm_e2e` tier |
| `LLM_BASE_URL` | OpenAI-compatible endpoint base URL |
| `LLM_API_KEY` | API key for that endpoint. Never echo it, and never write it to files. |
| `MESA_E2E_IRODS_ROOT` | Required for both tiers: a writable iRODS collection. Each run creates, and later removes, a sandbox under it. Without it, both tiers skip. |
| `MESA_E2E_CATALOG_DSN` | Optional catalog DSN; the default is a temp DuckDB file. For Postgres, run `mesa-ducklake migrate` against it first. |

The iRODS credentials come from the usual `~/.irods/irods_environment.json`
(or `IRODS_ENVIRONMENT_FILE`). Check that each variable is set before
running, and tell the user which are missing. Do not invent values.

## 3. Run: scripted tier alone, then both tiers in one process

```bash
pytest -m live_e2e tests/llm_e2e -v -rs                  # scripted only; no model needed
pytest -m "live_e2e or llm_e2e" tests/llm_e2e -v -rs     # both tiers, scripted first
```

Run the LLM tier in the **same** pytest invocation as the scripted tier.
The LLM test for a scenario skips itself when that scenario's scripted run
failed *in the same process*, so a pipeline bug is never counted as a model
failure. `pytest -m llm_e2e` on its own cannot make that distinction.

If `live_e2e` fails, stop and fix that first.

## 4. Results

Each run writes `.llm-e2e-results/<run>/`:

- `report.json`: the run metadata and per-scenario verdicts;
- `scripted/<scenario>.jsonl`: `live_e2e` transcripts;
- `llm/<scenario>-<n>.jsonl`: `llm_e2e` transcripts, one per repeat
  (`LLM_REPEATS`).

The directory is not committed.

## 5. Triage

Ask the **`llm-e2e-triage`** agent to classify the failures, and give
it the run directory:

> Use the llm-e2e-triage agent on .llm-e2e-results/<run>/

It sorts each failure into one of: model behavior, mesa-mcp bug,
mesa-ducklake bug, external flake (OLS or iRODS), or harness bug, with
evidence. Relay its table to the user. Fix only the code categories,
and file mesa-mcp bugs against `idss-mesa/mesa-mcp`.
