"""Spawn the real mesa-mcp over MCP stdio and call its tools.

One mesa-mcp process per scenario, on purpose: a DuckDB-file catalog is
single-writer, and mesa-mcp holds its lock for the life of the process.
Running each scenario in its own process — and reading DuckLake only
after it exits — lets the same harness verify both catalog backends.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import E2EConfig

# (message, requestedSchema) -> chosen content dict, or None to decline.
Chooser = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


@dataclass
class ToolOutcome:
    name: str
    arguments: dict[str, Any]
    payload: dict[str, Any]
    is_error: bool

    @property
    def error_code(self) -> str | None:
        err = self.payload.get("error") if self.is_error else None
        return err.get("code") if isinstance(err, dict) else None

    def as_text(self, limit: int = 12000) -> str:
        text = json.dumps(self.payload, default=str)
        return text if len(text) <= limit else text[:limit] + "…(truncated)"


class ElicitationBroker:
    """Answers mesa-mcp's MRTR ``elicitation/create`` requests.

    The chooser is swappable per scenario: scripted runs pick
    deterministically, LLM runs ask the model.
    """

    def __init__(self) -> None:
        self.chooser: Chooser | None = None
        self.log: list[dict[str, Any]] = []

    async def __call__(self, context: Any, params: Any) -> Any:
        from mcp import types

        message = getattr(params, "message", "")
        schema = getattr(params, "requested_schema", None) or getattr(params, "requestedSchema", {})
        content = await self.chooser(message, schema) if self.chooser else None
        self.log.append({"message": message, "schema": schema, "content": content})
        if content is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=content)


def subprocess_env(cfg: E2EConfig, cache_dir: Path) -> dict[str, str]:
    env = dict(cfg.passthrough_env)
    if cfg.catalog_dsn:
        env["MESA_MCP_DUCKLAKE__CATALOG_DSN"] = cfg.catalog_dsn
    env["MESA_MCP_DUCKLAKE__CACHE_DIR"] = str(cache_dir)
    return env


class MesaMcp:
    """Thin wrapper over an ``mcp.client.Client`` bound to one mesa-mcp process."""

    def __init__(self, client: Any, broker: ElicitationBroker) -> None:
        self._client = client
        self.broker = broker

    async def list_tools(self) -> list[Any]:
        result = await self._client.list_tools()
        return list(result.tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        result = await self._client.call_tool(name, arguments)
        payload = getattr(result, "structured_content", None)
        if payload is None:
            texts = [getattr(c, "text", "") for c in (result.content or [])]
            try:
                payload = json.loads("".join(texts)) if texts else {}
            except json.JSONDecodeError:
                payload = {"text": "".join(texts)}
        return ToolOutcome(name, arguments, payload, bool(getattr(result, "is_error", False)))


@asynccontextmanager
async def open_mesa_mcp(
    cfg: E2EConfig, *, cache_dir: Path, stderr_log: Path
) -> AsyncIterator[MesaMcp]:
    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=cfg.mesa_mcp_cmd,
        args=["--transport", "stdio"],
        env=subprocess_env(cfg, cache_dir),
    )
    broker = ElicitationBroker()
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    with stderr_log.open("a", encoding="utf-8") as errlog:
        async with Client(
            stdio_client(params, errlog=errlog),
            elicitation_callback=broker,
            read_timeout_seconds=120,
        ) as client:
            yield MesaMcp(client, broker)
