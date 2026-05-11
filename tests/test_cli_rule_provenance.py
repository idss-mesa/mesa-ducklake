"""Round-trip tests for rule-emitted AVU change records.

When the iRODS rule-engine callback shells out to ``mesa-ducklake record``,
it supplies ``source="irods-rule:<event>"`` plus the ``rule_invocation``
name. These fields must land verbatim on the resulting ``AvuChange``
row so an auditor can trace which rule emitted the change.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from mesa_ducklake import cli


@pytest.fixture
def streams() -> dict[str, io.StringIO]:
    return {
        "stdin": io.StringIO(),
        "stdout": io.StringIO(),
        "stderr": io.StringIO(),
    }


def test_rule_source_and_invocation_round_trip(streams, monkeypatch):
    """``source`` starting with ``irods-rule:`` and ``rule_invocation`` propagate."""
    project_id = uuid4()
    fake_project = MagicMock(name="Project", project_id=project_id)
    fake_snapshot = MagicMock(name="Snapshot", snapshot_id=11, parquet_file="r.parquet")
    fake_client = MagicMock(name="DuckLakeClient")
    fake_client.get_project.return_value = fake_project
    fake_client.record_changes.return_value = fake_snapshot

    monkeypatch.setenv("MESA_DUCKLAKE_DSN", "postgresql://stub/test")
    payload = {
        "project_id": str(project_id),
        "irods_path": "/iplant/home/alice/proj/file.csv",
        "target_type": "data_object",
        "attribute": "envo.biome",
        "value": "forest",
        "unit": "ENVO:00000446",
        "op": "add",
        "actor": "alice",
        "ts": "2026-05-10T12:00:00Z",
        "source": "irods-rule:acPostProcForModifyAVUMetadata",
        "rule_invocation": "mesa_avu_change",
        "via_ticket": "Tckt-12345",
    }
    streams["stdin"].write(json.dumps(payload))
    streams["stdin"].seek(0)

    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = cli.main(
            argv=["record"],
            stdin=streams["stdin"],
            stdout=streams["stdout"],
            stderr=streams["stderr"],
        )
    assert code == 0

    kwargs = fake_client.record_changes.call_args.kwargs
    change = kwargs["changes"][0]
    assert change.source == "irods-rule:acPostProcForModifyAVUMetadata"
    assert change.rule_invocation == "mesa_avu_change"
    assert change.via_ticket == "Tckt-12345"
    # ts should be parsed from the supplied ISO-8601 (rather than now()).
    assert change.ts.year == 2026
    assert change.ts.month == 5
    assert change.ts.day == 10


def test_non_rule_source_leaves_rule_invocation_none(streams, monkeypatch):
    """``source`` that's not a rule callback should not carry a rule_invocation."""
    project_id = uuid4()
    fake_project = MagicMock(name="Project", project_id=project_id)
    fake_snapshot = MagicMock(name="Snapshot", snapshot_id=12, parquet_file="r.parquet")
    fake_client = MagicMock(name="DuckLakeClient")
    fake_client.get_project.return_value = fake_project
    fake_client.record_changes.return_value = fake_snapshot

    monkeypatch.setenv("MESA_DUCKLAKE_DSN", "postgresql://stub/test")
    payload = {
        "project_id": str(project_id),
        "irods_path": "/iplant/home/alice/proj/file.csv",
        "target_type": "data_object",
        "attribute": "k",
        "value": "v",
        "unit": "",
        "op": "add",
        "actor": "alice",
        "source": "mesa-mcp",
    }
    streams["stdin"].write(json.dumps(payload))
    streams["stdin"].seek(0)

    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = cli.main(
            argv=["record"],
            stdin=streams["stdin"],
            stdout=streams["stdout"],
            stderr=streams["stderr"],
        )
    assert code == 0
    change = fake_client.record_changes.call_args.kwargs["changes"][0]
    assert change.source == "mesa-mcp"
    assert change.rule_invocation is None
    assert change.via_ticket is None
