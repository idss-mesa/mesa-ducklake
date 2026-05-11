"""mesa_avu_change — Python Rule Engine equivalent of mesa_avu_change.re.

Install this file in the directory configured for the
``irods_rule_engine_plugin-python`` instance in ``server_config.json``.
The hook names match those iRODS dispatches to the Python rule engine;
each one shells out to ``mesa-ducklake record`` with a single JSON
payload on stdin.

The hook receives a ``rule_args`` list whose contents mirror the
arguments the iRL rule sees. The exact slots depend on the iRODS
version, so we extract by index defensively and fall back to a
no-op when the shape doesn't match what we expect.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any


def _emit_record(payload: dict[str, Any]) -> None:
    """Pipe ``payload`` to the ``mesa-ducklake record`` CLI.

    We do not branch on the CLI's exit code: exit 2 ("not MESA-enabled")
    is the documented silent-skip case, and any other failure is
    surfaced through the CLI's stderr and the iRODS server log. The
    AVU operation itself has already succeeded by the time this rule
    fires.
    """
    cmd = ["mesa-ducklake-record-wrapper.sh", "record"]
    try:
        subprocess.run(
            cmd,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        # Never raise from the rule callback; the AVU mutation has
        # already committed. The wrapper script logs failures.
        return


_OP_MAP = {
    "add": "add",
    "rm": "delete",
    "rmw": "delete",
    "rmi": "delete",
    "set": "add",
    "mod": "add",
}


_ITEM_TYPE_MAP = {
    "-d": "data_object",
    "-C": "collection",
    "-R": "resource",
    "-u": "user",
}


def acPostProcForModifyAVUMetadata(rule_args: list[Any], callback: Any, rei: Any) -> None:
    """PEP fired after iRODS modifies an AVU.

    Args (in order):
        0: option ("add" | "rm" | "rmw" | "rmi" | "mod" | "set" | ...)
        1: item type ("-d" | "-C" | "-R" | "-u")
        2: item name (path or resource/user name)
        3: attribute
        4: value
        5: unit (may be empty)
    """
    try:
        option = str(rule_args[0])
        item_type = str(rule_args[1])
        item_name = str(rule_args[2])
        attribute = str(rule_args[3])
        value = str(rule_args[4])
        unit = str(rule_args[5]) if len(rule_args) > 5 else ""
    except (IndexError, ValueError):
        return

    # Pull caller identity off the iRODS RuleExecInfo.
    actor = ""
    via_ticket: str | None = None
    user_info = getattr(rei, "uoic", None) or getattr(rei, "uoip", None)
    if user_info is not None:
        actor = getattr(user_info, "userName", "") or ""

    cond_input = getattr(rei, "doi", None)
    if cond_input is not None:
        # Ticket username is propagated through the conditional input
        # map; some iRODS versions expose it directly on the RuleExecInfo
        # as ``ticketUserName``.
        ticket_value = (
            getattr(cond_input, "ticketUserName", None)
            or getattr(rei, "ticketUserName", None)
        )
        if ticket_value:
            via_ticket = str(ticket_value)

    payload: dict[str, Any] = {
        "irods_path": item_name,
        "target_type": _ITEM_TYPE_MAP.get(item_type, "data_object"),
        "attribute": attribute,
        "value": value,
        "unit": unit,
        "op": _OP_MAP.get(option, "add"),
        "actor": actor,
        "source": "irods-rule:acPostProcForModifyAVUMetadata",
        "rule_invocation": "mesa_avu_change",
    }
    if via_ticket:
        payload["via_ticket"] = via_ticket

    _emit_record(payload)


# A few iRODS deployments alias this PEP under a different name; we
# register the canonical aliases so admins don't have to fork the file.
acPostProcForModifyAVU = acPostProcForModifyAVUMetadata  # type: ignore[assignment]


# Local debugging helper. Run as:
#   python3 mesa_avu_change.py '{"irods_path":"/x", ...}'
# to test the wrapper script wiring without going through iRODS.
if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) > 1:
        _emit_record(json.loads(sys.argv[1]))
    else:
        print("usage: mesa_avu_change.py '<json-payload>'", file=sys.stderr)
        print("       payload fields match mesa-ducklake record", file=sys.stderr)
        # Show the wrapper command for transparency.
        print(shlex.join(["mesa-ducklake-record-wrapper.sh", "record"]))
