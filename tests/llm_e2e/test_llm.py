"""Tier 2 (``llm_e2e``): a self-hosted model drives mesa-mcp from a prompt.

The model sees only the scenario's tools, every call passes the sandbox
path guard, and the verdict comes from the end state in DuckLake + iRODS
— the same ``verify`` the scripted tier uses. ``LLM_REPEATS=N`` runs each
scenario N times and requires all N to pass.
"""

from __future__ import annotations

import pytest

from .harness.config import llm_skip_reason
from .harness.oracle import OracleUnavailable
from .runner import run_scenario
from .scenarios import SCENARIOS

pytestmark = pytest.mark.llm_e2e

LLM_SCENARIOS = [s for s in SCENARIOS if not s.scripted_only]


@pytest.mark.parametrize("scenario", LLM_SCENARIOS, ids=[s.id for s in LLM_SCENARIOS])
def test_llm(scenario, e2e_config, e2e_session) -> None:
    reason = llm_skip_reason(e2e_config)
    if reason:
        pytest.skip(reason)
    if scenario.needs_ticket and e2e_session.ticket is None:
        pytest.skip("could not issue an iRODS write ticket on the sandbox")
    if e2e_session.recorder.outcome("scripted", scenario.id) is False:
        pytest.skip("scripted tier failed this scenario — a pipeline bug, not a model failure")

    failures = []
    for attempt in range(1, e2e_config.llm_repeats + 1):
        try:
            result = run_scenario(e2e_session, scenario, "llm", attempt)
        except OracleUnavailable as exc:
            pytest.skip(str(exc))
        failures += [f"[attempt {attempt}] {f}" for f in result.failures]
    assert not failures, "\n".join(failures)
