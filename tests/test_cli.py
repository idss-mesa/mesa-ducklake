"""Unit tests for ``mesa-ducklake record`` CLI.

We drive :func:`mesa_ducklake.cli.main` in-process so the tests don't
shell out. ``DuckLakeClient`` is patched at the use-site so we don't
need a live Postgres.
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from mesa_ducklake import cli


@pytest.fixture
def streams() -> dict[str, io.StringIO]:
    """Return fresh stdin/stdout/stderr StringIOs for one CLI invocation."""
    return {
        "stdin": io.StringIO(),
        "stdout": io.StringIO(),
        "stderr": io.StringIO(),
    }


def _invoke(
    payload: dict[str, Any],
    streams: dict[str, io.StringIO],
    *,
    monkeypatch: pytest.MonkeyPatch,
    dsn: str | None = "postgresql://stub/test",
) -> int:
    """Helper: stuff the JSON payload onto stdin and run the CLI."""
    streams["stdin"].write(json.dumps(payload))
    streams["stdin"].seek(0)
    if dsn is None:
        monkeypatch.delenv("MESA_DUCKLAKE_DSN", raising=False)
    else:
        monkeypatch.setenv("MESA_DUCKLAKE_DSN", dsn)
    return cli.main(
        argv=["record"],
        stdin=streams["stdin"],
        stdout=streams["stdout"],
        stderr=streams["stderr"],
    )


def test_missing_dsn_exits_3(streams, monkeypatch):
    streams["stdin"].write("{}")
    streams["stdin"].seek(0)
    monkeypatch.delenv("MESA_DUCKLAKE_DSN", raising=False)
    code = cli.main(
        argv=["record"],
        stdin=streams["stdin"],
        stdout=streams["stdout"],
        stderr=streams["stderr"],
    )
    assert code == 3
    err = json.loads(streams["stderr"].getvalue())
    assert err["code"] == "missing_dsn"


def test_empty_stdin_exits_1(streams, monkeypatch):
    monkeypatch.setenv("MESA_DUCKLAKE_DSN", "postgresql://stub/test")
    code = cli.main(
        argv=["record"],
        stdin=streams["stdin"],
        stdout=streams["stdout"],
        stderr=streams["stderr"],
    )
    assert code == 1
    err = json.loads(streams["stderr"].getvalue())
    assert err["code"] == "invalid_input"


def test_bad_json_exits_1(streams, monkeypatch):
    streams["stdin"].write("not json{{")
    streams["stdin"].seek(0)
    monkeypatch.setenv("MESA_DUCKLAKE_DSN", "postgresql://stub/test")
    code = cli.main(
        argv=["record"],
        stdin=streams["stdin"],
        stdout=streams["stdout"],
        stderr=streams["stderr"],
    )
    assert code == 1


def test_missing_required_field_exits_1(streams, monkeypatch):
    fake_client = MagicMock(name="DuckLakeClient")
    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = _invoke(
            {"irods_path": "/iplant/home/alice/proj/file"},  # missing target_type etc.
            streams,
            monkeypatch=monkeypatch,
        )
    assert code == 1


def test_path_not_in_mesa_project_exits_2(streams, monkeypatch):
    fake_client = MagicMock(name="DuckLakeClient")
    fake_client.find_project_by_path.return_value = None
    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = _invoke(
            {
                "irods_path": "/iplant/home/alice/elsewhere/file",
                "target_type": "data_object",
                "attribute": "k",
                "value": "v",
                "unit": "",
                "op": "add",
                "actor": "alice",
                "source": "irods-rule:acPostProcForModifyAVUMetadata",
            },
            streams,
            monkeypatch=monkeypatch,
        )
    assert code == 2
    err = json.loads(streams["stderr"].getvalue())
    assert err["code"] == "not_mesa_enabled"


def test_happy_path_with_explicit_project_id(streams, monkeypatch):
    project_id = uuid4()
    fake_project = MagicMock(name="Project", project_id=project_id)
    fake_snapshot = MagicMock(name="Snapshot", snapshot_id=42, parquet_file="snap.parquet")
    fake_client = MagicMock(name="DuckLakeClient")
    fake_client.get_project.return_value = fake_project
    fake_client.record_changes.return_value = fake_snapshot
    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = _invoke(
            {
                "project_id": str(project_id),
                "irods_path": "/iplant/home/alice/proj/file.csv",
                "target_type": "data_object",
                "attribute": "envo.biome",
                "value": "forest",
                "unit": "ENVO:00000446",
                "op": "add",
                "actor": "alice",
                "source": "irods-rule:acPostProcForModifyAVUMetadata",
            },
            streams,
            monkeypatch=monkeypatch,
        )
    assert code == 0
    out = json.loads(streams["stdout"].getvalue())
    assert out["snapshot_id"] == 42
    assert out["parquet_file"] == "snap.parquet"
    assert out["project_id"] == str(project_id)

    # Confirm the AvuChange handed to ``record_changes`` carries the right fields.
    args, kwargs = fake_client.record_changes.call_args
    assert kwargs["project_id"] == project_id
    assert kwargs["actor"] == "alice"
    assert len(kwargs["changes"]) == 1
    change = kwargs["changes"][0]
    assert change.attribute == "envo.biome"
    assert change.source == "irods-rule:acPostProcForModifyAVUMetadata"


def test_walks_to_project_when_id_missing(streams, monkeypatch):
    project_id = uuid4()
    fake_project = MagicMock(name="Project", project_id=project_id)
    fake_snapshot = MagicMock(name="Snapshot", snapshot_id=7, parquet_file="snap.parquet")

    # ``find_project_by_path`` returns None for the leaf and the immediate
    # parent, then the project for the actual root.
    def find(path: str) -> Any:
        if path == "/iplant/home/alice/proj":
            return fake_project
        return None

    fake_client = MagicMock(name="DuckLakeClient")
    fake_client.find_project_by_path.side_effect = find
    fake_client.record_changes.return_value = fake_snapshot
    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = _invoke(
            {
                "irods_path": "/iplant/home/alice/proj/subdir/file.csv",
                "target_type": "data_object",
                "attribute": "k",
                "value": "v",
                "unit": "",
                "op": "add",
                "actor": "alice",
                "source": "irods-rule:acPostProcForModifyAVUMetadata",
            },
            streams,
            monkeypatch=monkeypatch,
        )
    assert code == 0
    # The walk should have probed at least three paths (leaf, subdir, proj).
    assert fake_client.find_project_by_path.call_count >= 3


def test_unknown_project_id_exits_1(streams, monkeypatch):
    fake_client = MagicMock(name="DuckLakeClient")
    fake_client.get_project.side_effect = KeyError("missing")
    with patch("mesa_ducklake.cli.DuckLakeClient", return_value=fake_client):
        code = _invoke(
            {
                "project_id": str(uuid4()),
                "irods_path": "/x",
                "target_type": "data_object",
                "attribute": "k",
                "value": "v",
                "unit": "",
                "op": "add",
                "actor": "alice",
                "source": "x",
            },
            streams,
            monkeypatch=monkeypatch,
        )
    assert code == 1
    err = json.loads(streams["stderr"].getvalue())
    assert err["code"] == "unknown_project"


def test_unknown_verb_exits_1(streams, monkeypatch):
    monkeypatch.setenv("MESA_DUCKLAKE_DSN", "postgresql://stub/test")
    code = cli.main(
        argv=["something-else"],
        stdin=streams["stdin"],
        stdout=streams["stdout"],
        stderr=streams["stderr"],
    )
    assert code == 1
