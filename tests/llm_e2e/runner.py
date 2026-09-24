"""Run one scenario in one tier and turn the outcome into a ``ScenarioResult``."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harness.checks import LakeView, open_lake
from .harness.config import E2EConfig
from .harness.llm_agent import run_agent
from .harness.mcp_server import ToolOutcome, open_mesa_mcp
from .harness.oracle import Term
from .harness.report import Recorder, ScenarioResult
from .harness.sandbox import Sandbox
from .scenarios import Scenario, prefetch_terms


@dataclass
class Ctx:
    tier: str
    attempt: int
    cfg: E2EConfig
    sandbox: Sandbox
    paths: dict[str, str]
    terms: dict[tuple[str, str], Term]
    policy_name: str
    ticket: str | None = None
    outcomes: list[ToolOutcome] = field(default_factory=list)
    elicitations: list[dict[str, Any]] = field(default_factory=list)
    final_text: str = ""
    lake: LakeView | None = None

    def term(self, ontology: str, iri: str) -> Term:
        return self.terms[(ontology, iri)]


@dataclass
class Session:
    """Everything a scenario run needs that outlives one scenario."""

    cfg: E2EConfig
    sandbox: Sandbox
    recorder: Recorder
    catalog_dsn: str
    ticket: str | None = None

    def mcp_cache(self, tier: str, scenario: str) -> Path:
        return self.cfg.run_dir / "mesa-mcp-cache" / tier / scenario

    @property
    def stderr_log(self) -> Path:
        return self.cfg.run_dir / "mesa-mcp.stderr.log"


def _make_ctx(sess: Session, scn: Scenario, tier: str, attempt: int) -> Ctx:
    label = f"{tier}_{scn.id}_{attempt}"
    base = sess.sandbox.collection(label)
    paths = {
        "dir": base,
        "file": sess.sandbox.data_object(f"{label}/sample.csv"),
        "coll": sess.sandbox.collection(f"{label}/coll"),
    }
    return Ctx(
        tier=tier,
        attempt=attempt,
        cfg=sess.cfg,
        sandbox=sess.sandbox,
        paths=paths,
        terms=prefetch_terms(scn),
        policy_name=f"e2e_{tier}_{attempt}",
        ticket=sess.ticket,
    )


def _snapshot_count(sess: Session) -> int:
    with open_lake(sess.catalog_dsn, sess.sandbox.session, sess.sandbox.root) as lake:
        return lake.snapshot_count()


async def _scripted(sess: Session, scn: Scenario, ctx: Ctx, log) -> list[str]:
    fails: list[str] = []
    async with open_mesa_mcp(
        sess.cfg, cache_dir=sess.mcp_cache("scripted", scn.id), stderr_log=sess.stderr_log
    ) as mcp:
        if scn.scripted_choice is not None:
            choice = scn.scripted_choice

            async def chooser(message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
                return choice(message, schema)

            mcp.broker.chooser = chooser
        for i, step in enumerate(scn.steps):
            args = step.args(ctx) if callable(step.args) else dict(step.args)
            outcome = await mcp.call(step.tool, args)
            ctx.outcomes.append(outcome)
            log(
                {
                    "event": "tool",
                    "step": i,
                    "name": step.tool,
                    "arguments": args,
                    "is_error": outcome.is_error,
                    "result": outcome.payload,
                }
            )
            if step.expect_error:
                if outcome.error_code != step.expect_error:
                    fails.append(
                        f"step {i} {step.tool}: expected error {step.expect_error!r}, "
                        f"got {outcome.error_code or 'success'}"
                    )
            elif outcome.is_error:
                fails.append(f"step {i} {step.tool} failed: {outcome.as_text(600)}")
            elif (pf := outcome.payload.get("partial_failure")) is not None:
                fails.append(f"step {i} {step.tool}: partial_failure {pf}")
        ctx.elicitations = list(mcp.broker.log)
    return fails


async def _llm(sess: Session, scn: Scenario, ctx: Ctx, log) -> tuple[list[str], Any]:
    prompt = scn.prompt(ctx) if callable(scn.prompt) else scn.prompt
    log({"event": "prompt", "prompt": prompt, "tools": sorted(scn.tools)})
    async with open_mesa_mcp(
        sess.cfg,
        cache_dir=sess.mcp_cache("llm", f"{scn.id}-{ctx.attempt}"),
        stderr_log=sess.stderr_log,
    ) as mcp:
        run = await run_agent(
            sess.cfg, mcp, task=prompt, root=sess.sandbox.root, allowed_tools=scn.tools, log=log
        )
        ctx.elicitations = list(mcp.broker.log)
    ctx.final_text = run.final_text
    fails = [
        f"required tool {t} was never called"
        for t in sorted(scn.required_tools)
        if t not in run.tools_called
    ]
    if run.stopped != "completed":
        fails.append(f"agent stopped: {run.stopped} after {run.turns} turns")
    fails += [f"path guard rejected {r}" for r in run.guard_rejections]
    return fails, run


def run_scenario(sess: Session, scn: Scenario, tier: str, attempt: int = 0) -> ScenarioResult:
    log = sess.recorder.transcript(tier, scn.id, attempt)
    ctx = _make_ctx(sess, scn, tier, attempt)
    before = _snapshot_count(sess)
    started = time.monotonic()
    run = None
    if tier == "scripted":
        fails = asyncio.run(_scripted(sess, scn, ctx, log))
    else:
        fails, run = asyncio.run(_llm(sess, scn, ctx, log))

    # mesa-mcp has exited, so a DuckDB catalog's lock is free to read.
    with open_lake(sess.catalog_dsn, sess.sandbox.session, sess.sandbox.root) as lake:
        ctx.lake = lake
        delta = lake.snapshot_count() - before
        if scn.snapshot_delta is not None and delta != scn.snapshot_delta:
            # The model may legitimately take extra (read-only) steps, but
            # never extra writes: a snapshot count is a write count.
            fails.append(f"{delta} new snapshot(s); expected {scn.snapshot_delta}")
        try:
            fails += scn.verify(ctx)
        except Exception as exc:  # a verify crash is a failure, with evidence
            fails.append(f"verify raised {type(exc).__name__}: {exc}")
    log({"event": "verdict", "failures": fails})

    result = ScenarioResult(
        tier=tier,
        scenario=scn.id,
        attempt=attempt,
        passed=not fails,
        failures=fails,
        tools_called=run.tools_called if run else [o.name for o in ctx.outcomes],
        turns=run.turns if run else len(ctx.outcomes),
        latency_s=run.latency_s if run else time.monotonic() - started,
        model=sess.cfg.llm_model if tier == "llm" else "",
    )
    sess.recorder.add(result)
    return result
