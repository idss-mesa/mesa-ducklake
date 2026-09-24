"""A throwaway, MESA-enabled iRODS collection per e2e run, plus the path guard.

The LLM tier hands write-capable tools to a model, against a live Data
Store. Two independent fences keep that contained:

* only the tools a scenario lists are exposed, and destructive ones never
  are (see ``llm_agent.NEVER_EXPOSE``);
* every tool call passes :func:`guard_tool_call` before it reaches
  mesa-mcp, which rejects any iRODS path outside the sandbox.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Argument names mesa-mcp tools use for iRODS paths.
PATH_KEYS = frozenset({"path", "target", "irods_path", "project_path"})


class PathGuardError(ValueError):
    """A tool call tried to touch something outside the sandbox."""


def _walk_strings(obj: Any, key: str | None = None) -> Iterator[tuple[str | None, str]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v, key)
    elif isinstance(obj, str):
        yield key, obj


def guard_tool_call(tool: str, args: dict[str, Any], root: str, allowed: set[str]) -> None:
    """Raise :class:`PathGuardError` unless ``tool(args)`` stays inside ``root``."""
    if tool not in allowed:
        raise PathGuardError(f"tool {tool!r} is not exposed in this scenario")
    if args.get("target_type") in {"resource", "user"}:
        raise PathGuardError("AVUs on resources/users are out of scope for e2e runs")
    root = root.rstrip("/")
    for key, value in _walk_strings(args):
        if key not in PATH_KEYS:
            continue
        if not value.startswith("/"):
            raise PathGuardError(f"{key}={value!r} is not an absolute iRODS path")
        # normpath collapses ``..`` so ``<root>/../elsewhere`` cannot slip by.
        norm = os.path.normpath(value)
        if norm != root and not norm.startswith(root + "/"):
            raise PathGuardError(f"{key}={value!r} is outside the sandbox {root}")


def open_irods_session() -> Any:
    """A python-irodsclient session from the same sources mesa-mcp uses.

    ``MESA_MCP_IRODS_USER``/``_PASSWORD`` (+ ``MESA_MCP_IRODS__HOST`` /
    ``__PORT`` / ``__ZONE``) win; otherwise ``IRODS_ENVIRONMENT_FILE`` or
    ``~/.irods/irods_environment.json``.
    """
    from irods.session import iRODSSession

    user = os.environ.get("MESA_MCP_IRODS_USER")
    password = os.environ.get("MESA_MCP_IRODS_PASSWORD")
    if user and password:
        return iRODSSession(
            host=os.environ.get("MESA_MCP_IRODS__HOST", "data.cyverse.org"),
            port=int(os.environ.get("MESA_MCP_IRODS__PORT", "1247")),
            zone=os.environ.get("MESA_MCP_IRODS__ZONE", "iplant"),
            user=user,
            password=password,
        )
    env_file = os.environ.get(
        "IRODS_ENVIRONMENT_FILE", str(Path.home() / ".irods" / "irods_environment.json")
    )
    return iRODSSession(irods_env_file=env_file)


@dataclass
class Sandbox:
    """One run's collection: ``<irods_root>/mesa-e2e-<run>-<rand>``."""

    session: Any
    root: str
    actor: str
    zone: str
    created: list[str] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)

    @classmethod
    def create(cls, session: Any, irods_root: str, run_id: str) -> Sandbox:
        stamp = run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        root = f"{irods_root}/mesa-e2e-{stamp}-{secrets.token_hex(3)}"
        session.collections.create(root)
        return cls(session=session, root=root, actor=session.username, zone=session.zone)

    def data_object(self, name: str, body: bytes = b"id,value\n1,42\n") -> str:
        path = f"{self.root}/{name}"
        obj = self.session.data_objects.create(path)
        with obj.open("w") as fh:
            fh.write(body)
        self.created.append(path)
        return path

    def collection(self, name: str) -> str:
        path = f"{self.root}/{name}"
        self.session.collections.create(path)
        self.created.append(path)
        return path

    def write_ticket(self) -> str:
        """Issue a write ticket on the sandbox root (deleted at teardown)."""
        from irods.ticket import Ticket

        ticket = Ticket(self.session)
        ticket.issue("write", self.root)
        self.tickets.append(ticket.string)
        return ticket.string

    def irods_avus(self, path: str) -> set[tuple[str, str, str]]:
        """The live iCAT AVU set on ``path`` (data object or collection)."""
        try:
            target = self.session.data_objects.get(path)
        except Exception:
            target = self.session.collections.get(path)
        return {(m.name, m.value, m.units or "") for m in target.metadata.items()}

    def teardown(self, keep: bool = False, retries: int = 5, retry_delay: float = 15.0) -> None:
        from irods.ticket import Ticket

        for t in self.tickets:
            try:
                Ticket(self.session, t).delete()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        if keep:
            return
        # A Parquet upload that stalled mid-run can leave a replica locked for
        # a while, so the first remove may fail with CAT_COLLECTION_NOT_EMPTY.
        for attempt in range(retries):
            try:
                self.session.collections.remove(self.root, recurse=True, force=True)
                return
            except Exception:
                if attempt == retries - 1:
                    raise
                time.sleep(retry_delay)
