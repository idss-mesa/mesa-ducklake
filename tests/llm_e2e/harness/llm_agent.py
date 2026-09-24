"""A minimal tool-calling agent over litellm, for self-hosted models.

litellm speaks the OpenAI Chat Completions shape to vLLM
(``hosted_vllm/<model>``), a LiteLLM proxy (``openai/<alias>`` +
``LLM_BASE_URL``), or AI Verde. vLLM must be started with
``--enable-auto-tool-choice`` and a ``--tool-call-parser`` matching the
model, or the model's tool calls arrive as plain text.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import E2EConfig
from .mcp_server import MesaMcp
from .sandbox import PathGuardError, guard_tool_call

# Never handed to a model, whatever a scenario asks for.
NEVER_EXPOSE = frozenset(
    {
        "ds_delete_file",
        "ds_move_file",
        "ds_modify_access",
        "ds_modify_access_inheritance",
        "ds_execute_rule",
        "ds_create_ticket",
        "ds_delete_ticket",
        "ds_modify_ticket",
        "ds_write_file",
        "ds_upload_file",
    }
)

SYSTEM_PROMPT = """\
You are a research-data curator using the mesa-mcp tools on the CyVerse Data Store.
Work only inside the sandbox collection {root}. Use the tools to complete the task;
do not ask the user questions. When a tool needs an ontology term, look it up with the
OLS tools rather than guessing identifiers. When you are finished, reply with one short
sentence summarising what you did, and include any ontology CURIEs you used.
"""


def _resolve_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1], {})
            merged = {**_resolve_refs(copy.deepcopy(target), defs)}
            merged.update({k: v for k, v in node.items() if k != "$ref"})
            return merged
        return {k: _resolve_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, defs) for v in node]
    return node


def mcp_tool_to_openai(name: str, description: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP tool definition to an OpenAI ``tools`` entry.

    Inlines ``$defs`` references and drops the ``$schema`` dialect marker:
    several OpenAI-compatible servers (vLLM's guided decoding among them)
    reject or ignore schemas that rely on either.
    """
    schema = copy.deepcopy(input_schema or {"type": "object", "properties": {}})
    defs = schema.pop("$defs", {})
    schema.pop("$schema", None)
    schema = _resolve_refs(schema, defs)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return {
        "type": "function",
        "function": {"name": name, "description": description or "", "parameters": schema},
    }


@dataclass
class AgentRun:
    final_text: str = ""
    turns: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    guard_rejections: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    stopped: str = "completed"  # completed | max_turns | error

    @property
    def tools_called(self) -> list[str]:
        return [c["name"] for c in self.tool_calls]


async def _completion(cfg: E2EConfig, **kwargs: Any) -> Any:
    import litellm

    return await litellm.acompletion(
        model=cfg.llm_model,
        api_base=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        temperature=cfg.llm_temperature,
        timeout=cfg.llm_timeout,
        **kwargs,
    )


async def llm_choose(
    cfg: E2EConfig, task: str, message: str, schema: dict[str, Any]
) -> dict[str, Any] | None:
    """Answer an MRTR term-choice elicitation by asking the model."""
    prop = (schema.get("properties") or {}).get("iri") or {}
    options = list(
        zip(prop.get("enum", []), prop.get("enumNames", prop.get("enum", [])), strict=False)
    )
    if not options:
        return None
    listing = "\n".join(f"{i}. {label} — {iri}" for i, (iri, label) in enumerate(options))
    resp = await _completion(
        cfg,
        messages=[
            {"role": "system", "content": "Answer with the number of the best option only."},
            {"role": "user", "content": f"Task: {task}\n\n{message}\n\n{listing}"},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()
    digits = "".join(ch for ch in text.split()[0] if ch.isdigit()) if text else ""
    if not digits or int(digits) >= len(options):
        return None
    return {"iri": options[int(digits)][0]}


async def run_agent(
    cfg: E2EConfig,
    mcp: MesaMcp,
    *,
    task: str,
    root: str,
    allowed_tools: set[str],
    log: Callable[[dict[str, Any]], None],
) -> AgentRun:
    allowed = set(allowed_tools) - NEVER_EXPOSE
    tools = [
        mcp_tool_to_openai(t.name, t.description, t.input_schema)
        for t in await mcp.list_tools()
        if t.name in allowed
    ]

    async def choose(message: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        choice = await llm_choose(cfg, task, message, schema)
        log({"event": "elicitation", "message": message, "choice": choice})
        return choice

    mcp.broker.chooser = choose
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(root=root)},
        {"role": "user", "content": task},
    ]
    run = AgentRun()
    started = time.monotonic()
    try:
        for _ in range(cfg.llm_max_turns):
            run.turns += 1
            resp = await _completion(cfg, messages=messages, tools=tools, tool_choice="auto")
            msg = resp.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])
            log(
                {
                    "event": "assistant",
                    "turn": run.turns,
                    "content": msg.content,
                    "tool_calls": [
                        {"name": c.function.name, "arguments": c.function.arguments} for c in calls
                    ],
                }
            )
            assistant: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if calls:
                assistant["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in calls
                ]
            messages.append(assistant)
            if not calls:
                run.final_text = msg.content or ""
                break
            for c in calls:
                name = c.function.name
                try:
                    args = json.loads(c.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    guard_tool_call(name, args, root, allowed)
                except (ValueError, PathGuardError) as exc:
                    run.guard_rejections.append(f"{name}: {exc}")
                    content = json.dumps(
                        {"error": {"code": "harness_rejected", "message": str(exc)}}
                    )
                    log(
                        {
                            "event": "rejected",
                            "name": name,
                            "arguments": c.function.arguments,
                            "reason": str(exc),
                        }
                    )
                else:
                    outcome = await mcp.call(name, args)
                    run.tool_calls.append(
                        {
                            "name": name,
                            "arguments": args,
                            "is_error": outcome.is_error,
                            "error_code": outcome.error_code,
                        }
                    )
                    content = outcome.as_text()
                    log(
                        {
                            "event": "tool",
                            "name": name,
                            "arguments": args,
                            "is_error": outcome.is_error,
                            "result": outcome.payload,
                        }
                    )
                messages.append({"role": "tool", "tool_call_id": c.id, "content": content})
        else:
            run.stopped = "max_turns"
    except Exception as exc:
        run.stopped = "error"
        log({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        run.latency_s = time.monotonic() - started
        mcp.broker.chooser = None
    return run
