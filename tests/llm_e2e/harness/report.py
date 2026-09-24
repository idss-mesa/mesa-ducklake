"""Per-run artifacts: JSONL transcripts and a ``report.json`` summary.

Layout (consumed by the ``llm-e2e-triage`` agent)::

    .llm-e2e-results/<run_id>/
        report.json                 # one entry per (tier, scenario, attempt)
        <tier>/<scenario>[-<n>].jsonl
        mesa-mcp.stderr.log
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class ScenarioResult:
    tier: str  # "scripted" | "llm"
    scenario: str
    attempt: int
    passed: bool
    failures: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    turns: int = 0
    latency_s: float = 0.0
    model: str = ""
    skipped: str = ""


class Recorder:
    def __init__(self, run_dir: Path, meta: dict[str, Any]) -> None:
        self.run_dir = run_dir
        self.meta = meta
        self.results: list[ScenarioResult] = []

    def transcript(self, tier: str, scenario: str, attempt: int = 0):
        path = (
            self.run_dir
            / tier
            / (f"{scenario}-{attempt}.jsonl" if attempt else f"{scenario}.jsonl")
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        def log(event: dict[str, Any]) -> None:
            event = {"ts": datetime.now(tz=UTC).isoformat(), **event}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")

        return log

    def add(self, result: ScenarioResult) -> None:
        self.results.append(result)
        self.write()

    def outcome(self, tier: str, scenario: str) -> bool | None:
        """Did ``scenario`` pass in ``tier`` this run? ``None`` if it didn't run."""
        runs = [
            r for r in self.results if r.tier == tier and r.scenario == scenario and not r.skipped
        ]
        return all(r.passed for r in runs) if runs else None

    def summary(self) -> dict[str, Any]:
        by_tier: dict[str, dict[str, int]] = {}
        for r in self.results:
            t = by_tier.setdefault(r.tier, {"passed": 0, "failed": 0, "skipped": 0})
            t["skipped" if r.skipped else "passed" if r.passed else "failed"] += 1
        return by_tier

    def write(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        doc = {
            "meta": self.meta,
            "summary": self.summary(),
            "results": [asdict(r) for r in self.results],
        }
        (self.run_dir / "report.json").write_text(json.dumps(doc, indent=2, default=str))

    def table(self) -> str:
        lines = [f"{'tier':<9}{'scenario':<34}{'try':>4}  {'result':<7}{'turns':>6}{'secs':>8}"]
        for r in self.results:
            status = "SKIP" if r.skipped else "PASS" if r.passed else "FAIL"
            lines.append(
                f"{r.tier:<9}{r.scenario:<34}{r.attempt:>4}  "
                f"{status:<7}{r.turns:>6}{r.latency_s:>8.1f}"
            )
        return "\n".join(lines)
