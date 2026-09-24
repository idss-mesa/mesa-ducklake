"""Environment-driven configuration for the live e2e tiers.

Everything here is read from the environment so the same suite runs
against a local vLLM, the CARC LiteLLM gateway, or AI Verde without code
changes. Nothing is defaulted to a real endpoint: an unset variable means
"skip", never "guess".
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


def _get(env: Mapping[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


@dataclass(frozen=True)
class E2EConfig:
    # --- live tier (iRODS + OLS + catalog) --------------------------------
    irods_root: str
    catalog_dsn: str | None
    mesa_mcp_cmd: str
    keep_sandbox: bool
    results_dir: Path
    run_id: str
    # --- llm tier -----------------------------------------------------------
    llm_model: str
    llm_base_url: str | None
    llm_api_key: str | None
    llm_temperature: float
    llm_max_turns: int
    llm_repeats: int
    llm_timeout: float
    # Extra env forwarded to the mesa-mcp subprocess (credentials, config).
    passthrough_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> E2EConfig:
        src = env if env is not None else os.environ
        run_id = _get(src, "MESA_E2E_RUN_ID") or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        passthrough = {
            k: v
            for k, v in src.items()
            if k.startswith(("MESA_MCP_", "IRODS_")) or k in {"HOME", "USER", "LANG"}
        }
        return cls(
            irods_root=_get(src, "MESA_E2E_IRODS_ROOT").rstrip("/"),
            catalog_dsn=_get(src, "MESA_E2E_CATALOG_DSN") or None,
            mesa_mcp_cmd=_get(src, "MESA_E2E_MESA_MCP_CMD", "mesa-mcp"),
            keep_sandbox=_get(src, "MESA_E2E_KEEP") in {"1", "true", "yes"},
            results_dir=Path(_get(src, "MESA_E2E_RESULTS_DIR", ".llm-e2e-results")),
            run_id=run_id,
            llm_model=_get(src, "LLM_MODEL"),
            llm_base_url=_get(src, "LLM_BASE_URL") or None,
            llm_api_key=_get(src, "LLM_API_KEY") or None,
            llm_temperature=float(_get(src, "LLM_TEMPERATURE", "0")),
            llm_max_turns=int(_get(src, "LLM_MAX_TURNS", "12")),
            llm_repeats=max(1, int(_get(src, "LLM_REPEATS", "1"))),
            llm_timeout=float(_get(src, "LLM_TIMEOUT", "300")),
            passthrough_env=passthrough,
        )

    @property
    def run_dir(self) -> Path:
        return self.results_dir / self.run_id


def _missing_modules(*names: str) -> list[str]:
    return [n for n in names if importlib.util.find_spec(n) is None]


def live_skip_reason(cfg: E2EConfig) -> str | None:
    """Why the live (scripted) tier cannot run here, or ``None`` if it can."""
    if not cfg.irods_root:
        return "MESA_E2E_IRODS_ROOT is unset (a writable iRODS collection for the sandbox)"
    missing = _missing_modules("mcp", "mesa_mcp", "irods")
    if missing:
        return (
            f"missing modules {missing}; install with "
            "`pip install -e '.[llm-e2e]' -e '../mesa-mcp[ducklake]'`"
        )
    if shutil.which(cfg.mesa_mcp_cmd) is None:
        return f"{cfg.mesa_mcp_cmd!r} is not on PATH (set MESA_E2E_MESA_MCP_CMD)"
    return None


def llm_skip_reason(cfg: E2EConfig) -> str | None:
    """Why the LLM tier cannot run here, or ``None`` if it can."""
    reason = live_skip_reason(cfg)
    if reason:
        return reason
    if not cfg.llm_model:
        return "LLM_MODEL is unset (e.g. hosted_vllm/Qwen/Qwen3-32B with LLM_BASE_URL)"
    if _missing_modules("litellm"):
        return "litellm is not installed; `pip install -e '.[llm-e2e]'`"
    return None
