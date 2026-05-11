"""Smoke tests: imports work, models construct, internal modules don't leak."""

from __future__ import annotations

from uuid import uuid4

import mesa_ducklake
from mesa_ducklake import AvuChange, DuckLakeClient, Project, Snapshot
from mesa_ducklake.irods_path import (
    ducklake_subpath,
    is_mesa_enabled,
    mesa_avu_attribute,
)


def test_package_exports() -> None:
    """The top-level package re-exports the documented public symbols."""
    assert mesa_ducklake.__version__ == "0.1.0"
    assert mesa_ducklake.DuckLakeClient is DuckLakeClient
    assert mesa_ducklake.AvuChange is AvuChange
    assert mesa_ducklake.Project is Project
    assert mesa_ducklake.Snapshot is Snapshot


def test_internal_modules_are_not_exported() -> None:
    """``CatalogStore`` and ``LakeStore`` are internal — never on the package surface."""
    assert not hasattr(mesa_ducklake, "CatalogStore")
    assert not hasattr(mesa_ducklake, "LakeStore")
    assert "CatalogStore" not in mesa_ducklake.__all__
    assert "LakeStore" not in mesa_ducklake.__all__


def test_avu_change_constructs() -> None:
    change = AvuChange(
        irods_path="/iplant/home/alice/myproj/file.csv",
        target_type="data_object",
        attribute="envo.biome",
        value="tropical moist broadleaf forest",
        unit="ENVO:01000228",
        op="add",
        actor="alice",
    )
    assert change.op == "add"
    assert change.source == "mesa-mcp"
    assert change.project_id is None


def test_project_constructs() -> None:
    project = Project(
        project_id=uuid4(),
        irods_path="/iplant/home/alice/myproj",
        irods_zone="iplant",
        ducklake_path="/iplant/home/alice/myproj/.mesa/ducklake",
        created_by="alice",
    )
    assert project.status == "active"


def test_snapshot_constructs() -> None:
    snap = Snapshot(
        snapshot_id=1,
        project_id=uuid4(),
        actor="alice",
        parquet_file="snapshot_1.parquet",
    )
    assert snap.parent_snapshot is None
    assert snap.note is None


def test_client_construction_succeeds(client_fixture: DuckLakeClient) -> None:
    """The client constructor does not require a live Postgres."""
    assert isinstance(client_fixture, DuckLakeClient)


def test_ducklake_subpath_returns_canonical_path() -> None:
    assert (
        ducklake_subpath("/iplant/home/alice/myproj")
        == "/iplant/home/alice/myproj/.mesa/ducklake"
    )
    assert (
        ducklake_subpath("/iplant/home/alice/myproj/")
        == "/iplant/home/alice/myproj/.mesa/ducklake"
    )


def test_mesa_avu_attribute() -> None:
    assert mesa_avu_attribute() == "mesa.enabled"


def test_is_mesa_enabled_detects_marker() -> None:
    assert is_mesa_enabled([("mesa.enabled", "true", "")]) is True
    assert is_mesa_enabled([("mesa.enabled", "true", "any-unit-ignored")]) is True
    assert is_mesa_enabled([("mesa.enabled", "false", "")]) is False
    assert is_mesa_enabled([("other.attr", "true", "")]) is False
    assert is_mesa_enabled([]) is False
