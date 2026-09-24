"""Fixtures for the live e2e tiers (``live_e2e`` scripted, ``llm_e2e`` model-driven).

Both tiers skip themselves unless their environment is configured — see
``harness/config.py`` and ``docs/dev/llm-e2e-tests.md`` — so the default
``pytest -q`` run and CI are unaffected.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator

import pytest

from .harness.config import E2EConfig, live_skip_reason
from .harness.report import Recorder

_RECORDER: Recorder | None = None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Scripted runs first, so the LLM tier can tell a pipeline bug (scripted
    # fails too) from a model failure (scripted passed).
    def tier_rank(item: pytest.Item) -> int:
        if item.get_closest_marker("llm_e2e"):
            return 2
        if item.get_closest_marker("live_e2e"):
            return 1
        return 0

    items.sort(key=tier_rank)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:  # noqa: ANN001
    if _RECORDER is not None and _RECORDER.results:
        terminalreporter.write_sep("=", "mesa-mcp → DuckLake e2e")
        terminalreporter.write_line(_RECORDER.table())
        terminalreporter.write_line(f"artifacts: {_RECORDER.run_dir}")


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    return E2EConfig.from_env()


@pytest.fixture(scope="session")
def e2e_session(e2e_config: E2EConfig, tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    """A MESA-enabled sandbox collection, shared by every live scenario."""
    reason = live_skip_reason(e2e_config)
    if reason:
        pytest.skip(reason)

    from .harness.mcp_server import open_mesa_mcp
    from .harness.sandbox import Sandbox, open_irods_session
    from .runner import Session

    cfg = e2e_config
    if not cfg.catalog_dsn:
        db = tmp_path_factory.mktemp("catalog") / "e2e-catalog.duckdb"
        cfg = dataclasses.replace(cfg, catalog_dsn=f"duckdb://{db}")

    global _RECORDER
    _RECORDER = Recorder(
        cfg.run_dir,
        meta={
            "run_id": cfg.run_id,
            "model": cfg.llm_model,
            "llm_base_url": cfg.llm_base_url,
            "catalog_backend": "duckdb" if cfg.catalog_dsn.startswith("duckdb") else "postgres",
            "irods_root": cfg.irods_root,
        },
    )

    irods = open_irods_session()
    sandbox = Sandbox.create(irods, cfg.irods_root, cfg.run_id)
    try:
        ticket = None
        try:
            ticket = sandbox.write_ticket()
        except Exception as exc:  # tickets can be disabled per zone/user
            _RECORDER.meta["ticket_unavailable"] = f"{type(exc).__name__}: {exc}"

        async def enroll() -> None:
            async with open_mesa_mcp(
                cfg,
                cache_dir=cfg.run_dir / "mesa-mcp-cache" / "init",
                stderr_log=cfg.run_dir / "mesa-mcp.stderr.log",
            ) as mcp:
                out = await mcp.call("mesa_ducklake_init_project", {"irods_path": sandbox.root})
                if out.is_error:
                    raise RuntimeError(f"mesa_ducklake_init_project failed: {out.as_text(800)}")

        asyncio.run(enroll())
        yield Session(
            cfg=cfg, sandbox=sandbox, recorder=_RECORDER, catalog_dsn=cfg.catalog_dsn, ticket=ticket
        )
    finally:
        _RECORDER.write()
        sandbox.teardown(keep=cfg.keep_sandbox)
        irods.cleanup()
