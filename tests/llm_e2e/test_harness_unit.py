"""Offline tests for the e2e harness itself. These run in the default suite.

A harness bug would otherwise surface only as a confusing live failure —
or worse, as a path guard that lets a model write outside the sandbox.
"""

from __future__ import annotations

import pytest

from .harness.config import E2EConfig, live_skip_reason, llm_skip_reason
from .harness.llm_agent import NEVER_EXPOSE, mcp_tool_to_openai
from .harness.oracle import Term, snake
from .harness.report import Recorder, ScenarioResult
from .harness.sandbox import PathGuardError, guard_tool_call
from .scenarios import SCENARIO_IDS, SCENARIOS

ROOT = "/iplant/home/alice/e2e/mesa-e2e-run-abc"
ALLOWED = {"mesa_avu_apply_term", "ds_add_avus", "ds_add_avu", "mesa_policy_enable"}


# --- path guard -------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"path": f"{ROOT}/x/sample.csv", "ontology_id": "envo", "value": "v"},
        {"path": ROOT},
        {"target": f"{ROOT}/x", "avus": [{"attribute": "a", "value": "/not/a/path", "unit": ""}]},
    ],
)
def test_guard_allows_paths_inside_the_sandbox(args) -> None:
    tool = "ds_add_avus" if "avus" in args else "mesa_avu_apply_term"
    guard_tool_call(tool, args, ROOT, ALLOWED)


@pytest.mark.parametrize(
    "args",
    [
        {"path": "/iplant/home/alice/other.csv"},
        {"path": f"{ROOT}/../../escape.csv"},
        {"path": f"{ROOT}-sibling/file"},
        {"path": "relative/file.csv"},
        {"target": "/iplant/home/shared/x", "avus": []},
    ],
)
def test_guard_rejects_paths_outside_the_sandbox(args) -> None:
    tool = "ds_add_avus" if "avus" in args else "mesa_avu_apply_term"
    with pytest.raises(PathGuardError):
        guard_tool_call(tool, args, ROOT, ALLOWED)


def test_guard_rejects_unexposed_tools_and_non_path_targets() -> None:
    with pytest.raises(PathGuardError):
        guard_tool_call("ds_delete_file", {"path": f"{ROOT}/x"}, ROOT, ALLOWED)
    with pytest.raises(PathGuardError):
        guard_tool_call("ds_add_avu", {"target_type": "user", "target": "alice"}, ROOT, ALLOWED)


def test_scenarios_never_expose_destructive_tools() -> None:
    for s in SCENARIOS:
        assert not (s.tools & NEVER_EXPOSE), s.id
        assert s.required_tools <= s.tools, s.id


def test_scenario_ids_are_unique() -> None:
    assert len(SCENARIO_IDS) == len(set(SCENARIO_IDS))


# --- schema conversion ------------------------------------------------------


def test_mcp_schema_is_inlined_for_openai() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"AvuItem": {"type": "object", "properties": {"attribute": {"type": "string"}}}},
        "properties": {
            "target": {"type": "string"},
            "avus": {"type": "array", "items": {"$ref": "#/$defs/AvuItem"}},
        },
        "required": ["target", "avus"],
    }
    tool = mcp_tool_to_openai("ds_add_avus", "Batch add.", schema)
    params = tool["function"]["parameters"]
    assert "$schema" not in params and "$defs" not in params
    assert params["properties"]["avus"]["items"]["properties"]["attribute"] == {"type": "string"}
    assert params["type"] == "object"
    assert tool["function"]["name"] == "ds_add_avus"


def test_empty_schema_becomes_an_empty_object() -> None:
    params = mcp_tool_to_openai("t", "", {})["function"]["parameters"]
    assert params == {"type": "object", "properties": {}}


# --- oracle -----------------------------------------------------------------


@pytest.mark.parametrize(
    "label,expected",
    [
        ("biome", "biome"),
        ("Environmental Feature", "environmental_feature"),
        ("pH value", "ph_value"),
        ("L-alanine", "lalanine"),
        ("biological_process", "biologicalprocess"),
        ("  degree   Celsius ", "degree_celsius"),
    ],
)
def test_snake_matches_the_documented_rule(label, expected) -> None:
    assert snake(label) == expected


def test_term_builds_the_canonical_triple() -> None:
    t = Term("ENVO", "http://purl.obolibrary.org/obo/ENVO_00000428", "biome", "ENVO:00000428")
    assert t.expected_avu("  tropical moist broadleaf forest ") == (
        "envo.biome",
        "tropical moist broadleaf forest",
        "ENVO:00000428",
    )


# --- config gating ----------------------------------------------------------


def test_tiers_skip_without_configuration() -> None:
    cfg = E2EConfig.from_env({})
    assert live_skip_reason(cfg) and "MESA_E2E_IRODS_ROOT" in live_skip_reason(cfg)
    assert llm_skip_reason(cfg)


def test_config_forwards_only_mesa_and_irods_env() -> None:
    cfg = E2EConfig.from_env(
        {
            "MESA_MCP_IRODS_USER": "alice",
            "IRODS_ENVIRONMENT_FILE": "/x",
            "AWS_SECRET_ACCESS_KEY": "nope",
            "MESA_E2E_IRODS_ROOT": "/z/",
        }
    )
    assert set(cfg.passthrough_env) == {"MESA_MCP_IRODS_USER", "IRODS_ENVIRONMENT_FILE"}
    assert cfg.irods_root == "/z"


# --- report -----------------------------------------------------------------


def test_recorder_tracks_outcomes_and_writes_a_report(tmp_path) -> None:
    rec = Recorder(tmp_path / "run", meta={"model": "m"})
    rec.add(ScenarioResult("scripted", "a", 0, passed=True))
    rec.add(ScenarioResult("scripted", "b", 0, passed=False, failures=["x"]))
    rec.transcript("llm", "a", 1)({"event": "prompt"})
    assert rec.outcome("scripted", "a") is True
    assert rec.outcome("scripted", "b") is False
    assert rec.outcome("llm", "a") is None
    assert rec.summary() == {"scripted": {"passed": 1, "failed": 1, "skipped": 0}}
    assert (tmp_path / "run" / "report.json").exists()
    assert (tmp_path / "run" / "llm" / "a-1.jsonl").exists()
    assert "FAIL" in rec.table()
