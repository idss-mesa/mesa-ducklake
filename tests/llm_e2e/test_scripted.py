"""Tier 1 (``live_e2e``): every scenario's tool calls, sent without an LLM.

Proves each mesa-mcp ontology endpoint works against live OLS4 and iRODS,
and that the resulting AVU history lands in DuckLake intact.
"""

from __future__ import annotations

import pytest

from .harness.oracle import OracleUnavailable
from .runner import run_scenario
from .scenarios import SCENARIO_IDS, SCENARIOS

pytestmark = pytest.mark.live_e2e


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_scripted(scenario, e2e_session) -> None:
    if scenario.needs_ticket and e2e_session.ticket is None:
        pytest.skip("could not issue an iRODS write ticket on the sandbox")
    try:
        result = run_scenario(e2e_session, scenario, "scripted")
    except OracleUnavailable as exc:
        pytest.skip(str(exc))
    assert result.passed, "\n".join(result.failures)
