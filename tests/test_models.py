"""Pydantic model validation tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from mesa_ducklake import AvuChange, Project


def _valid_avu_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "irods_path": "/iplant/home/alice/myproj/file.csv",
        "target_type": "data_object",
        "attribute": "envo.biome",
        "value": "tropical moist broadleaf forest",
        "unit": "ENVO:01000228",
        "op": "add",
        "actor": "alice",
    }
    base.update(overrides)
    return base


def test_avu_change_accepts_add() -> None:
    change = AvuChange(**_valid_avu_kwargs(op="add"))
    assert change.op == "add"


def test_avu_change_accepts_delete() -> None:
    change = AvuChange(**_valid_avu_kwargs(op="delete"))
    assert change.op == "delete"


def test_avu_change_rejects_invalid_op() -> None:
    with pytest.raises(ValidationError):
        AvuChange(**_valid_avu_kwargs(op="update"))


def test_avu_change_rejects_invalid_target_type() -> None:
    with pytest.raises(ValidationError):
        AvuChange(**_valid_avu_kwargs(target_type="bucket"))


@pytest.mark.parametrize("field", ["actor", "source"])
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_avu_change_rejects_empty_provenance(field: str, blank: str) -> None:
    """Provenance is mandatory; a blank author or origin is a bug, not data."""
    with pytest.raises(ValidationError):
        AvuChange(**_valid_avu_kwargs(**{field: blank}))


def test_avu_change_ts_is_timezone_aware() -> None:
    change = AvuChange(**_valid_avu_kwargs())
    assert change.ts.tzinfo is not None


def test_avu_change_provenance_defaults() -> None:
    change = AvuChange(**_valid_avu_kwargs())
    assert change.source == "mesa-mcp"
    assert change.via_ticket is None
    assert change.rule_invocation is None


def test_avu_change_accepts_ticket_provenance() -> None:
    change = AvuChange(
        **_valid_avu_kwargs(via_ticket="abc123", source="mesa-mcp"),
    )
    assert change.via_ticket == "abc123"


def test_avu_change_accepts_rule_provenance() -> None:
    change = AvuChange(
        **_valid_avu_kwargs(
            source="irods-rule:acPostProcForModifyAVUMetadata",
            rule_invocation="mesa_avu_change",
        ),
    )
    assert change.source.startswith("irods-rule:")
    assert change.rule_invocation == "mesa_avu_change"


def _valid_project_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "project_id": uuid4(),
        "irods_path": "/iplant/home/alice/myproj",
        "irods_zone": "iplant",
        "ducklake_path": "/iplant/home/alice/myproj/.mesa/ducklake",
        "created_by": "alice",
    }
    base.update(overrides)
    return base


def test_project_default_status_is_active() -> None:
    project = Project(**_valid_project_kwargs())
    assert project.status == "active"


def test_project_accepts_archived_status() -> None:
    project = Project(**_valid_project_kwargs(status="archived"))
    assert project.status == "archived"


def test_project_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        Project(**_valid_project_kwargs(status="paused"))


def test_project_created_at_is_timezone_aware() -> None:
    project = Project(**_valid_project_kwargs())
    assert project.created_at.tzinfo is not None
