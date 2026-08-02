"""End-to-end ``DuckLakeClient`` flow against an ephemeral Postgres + tmp lake."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mesa_ducklake import AvuChange, DuckLakeClient

pytestmark = pytest.mark.requires_postgres


def _change(
    irods_path: str,
    attribute: str,
    value: str,
    *,
    op: str = "add",
    unit: str = "",
    ts: datetime | None = None,
) -> AvuChange:
    return AvuChange(
        irods_path=irods_path,
        target_type="data_object",
        attribute=attribute,
        value=value,
        unit=unit,
        op=op,  # type: ignore[arg-type]
        actor="alice",
        ts=ts if ts is not None else datetime.now(tz=UTC),
    )


@pytest.fixture
def client(
    catalog_db: str,
    tmp_lake_root: Path,
) -> DuckLakeClient:
    # ``irods_session=None`` keeps these tests in local-only mode — the
    # WAL + iRODS-push flow is exercised end-to-end in
    # ``test_irods_sync.py`` with a properly mocked session.
    return DuckLakeClient(
        postgres_dsn=catalog_db,
        irods_session=None,
        lake_root_override=tmp_lake_root,
    )


def test_register_and_lookup_project(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/proj-e2e",
        actor="alice",
        zone="iplant",
    )
    assert project.ducklake_path.endswith("/.mesa/ducklake")

    by_id = client.get_project(project.project_id)
    assert by_id.project_id == project.project_id

    by_path = client.find_project_by_path("/iplant/home/alice/proj-e2e")
    assert by_path is not None
    assert by_path.project_id == project.project_id

    assert client.find_project_by_path("/never") is None


def test_record_changes_then_get_avus(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p1",
        actor="alice",
        zone="iplant",
    )
    snap = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[
            _change("/iplant/home/alice/p1/f.csv", "envo.biome", "tundra"),
            _change(
                "/iplant/home/alice/p1/f.csv",
                "envo.biome",
                "desert",
                unit="ENVO:01000176",
            ),
        ],
        note="seed",
    )
    assert snap.snapshot_id is not None
    assert snap.parquet_file == f"snapshot_{snap.snapshot_id}.parquet"

    current = client.get_avus(project.project_id, "/iplant/home/alice/p1/f.csv")
    triples = sorted((a.attribute, a.value, a.unit) for a in current)
    assert triples == [
        ("envo.biome", "desert", "ENVO:01000176"),
        ("envo.biome", "tundra", ""),
    ]


def test_get_avus_as_of_time_travel(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p2",
        actor="alice",
        zone="iplant",
    )
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)

    client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[
            _change("/iplant/home/alice/p2/f.csv", "envo.biome", "tundra", ts=t0),
        ],
    )
    client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[
            _change(
                "/iplant/home/alice/p2/f.csv",
                "envo.biome",
                "tundra",
                op="delete",
                ts=t1,
            ),
        ],
    )

    # Between the two snapshots, AVU is visible.
    middle = client.get_avus_as_of(
        project.project_id,
        "/iplant/home/alice/p2/f.csv",
        t0 + timedelta(hours=12),
    )
    assert [(a.attribute, a.value) for a in middle] == [("envo.biome", "tundra")]

    # After the delete, AVU is gone.
    after = client.get_avus_as_of(
        project.project_id,
        "/iplant/home/alice/p2/f.csv",
        t1 + timedelta(hours=1),
    )
    assert after == []


def test_get_avus_as_of_accepts_iso_string(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p2b",
        actor="alice",
        zone="iplant",
    )
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[_change("/iplant/home/alice/p2b/f.csv", "k", "v", ts=t0)],
    )
    later = client.get_avus_as_of(
        project.project_id,
        "/iplant/home/alice/p2b/f.csv",
        "2026-06-01T00:00:00Z",
    )
    assert [(a.attribute, a.value) for a in later] == [("k", "v")]


def test_diff_between_two_snapshots(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p3",
        actor="alice",
        zone="iplant",
    )
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=2)

    s1 = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[_change("/iplant/home/alice/p3/f.csv", "k", "v1", ts=t0)],
    )
    s2 = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[_change("/iplant/home/alice/p3/f.csv", "k", "v2", ts=t1)],
    )
    s3 = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[
            _change("/iplant/home/alice/p3/f.csv", "k", "v2", op="delete", ts=t2),
        ],
    )

    diff = client.diff(project.project_id, s1.snapshot_id, s3.snapshot_id)
    pairs = [(d.snapshot_id, d.op, d.value) for d in diff]
    assert pairs == [(s2.snapshot_id, "add", "v2"), (s3.snapshot_id, "delete", "v2")]


def test_list_snapshots_newest_first(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p4",
        actor="alice",
        zone="iplant",
    )
    a = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[_change("/iplant/home/alice/p4/f.csv", "k", "v1")],
    )
    b = client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[_change("/iplant/home/alice/p4/f.csv", "k", "v2")],
    )

    listed = client.list_snapshots(project.project_id)
    ids = [s.snapshot_id for s in listed]
    assert ids[0] == b.snapshot_id
    assert ids[-1] == a.snapshot_id
    assert b.parent_snapshot == a.snapshot_id


def test_record_changes_rejects_empty_batch(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p5",
        actor="alice",
        zone="iplant",
    )
    with pytest.raises(ValueError):
        client.record_changes(
            project_id=project.project_id,
            actor="alice",
            changes=[],
        )


def test_get_history_returns_events(client: DuckLakeClient) -> None:
    project = client.register_project(
        irods_path="/iplant/home/alice/p6",
        actor="alice",
        zone="iplant",
    )
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[_change("/iplant/home/alice/p6/f.csv", "k", "v", ts=t0)],
    )
    client.record_changes(
        project_id=project.project_id,
        actor="alice",
        changes=[
            _change("/iplant/home/alice/p6/f.csv", "k", "v", op="delete", ts=t1),
        ],
    )
    history = client.get_history(project.project_id, "/iplant/home/alice/p6/f.csv")
    assert [h.op for h in history] == ["delete", "add"]


def test_close_is_idempotent(client: DuckLakeClient) -> None:
    client.close()
    client.close()  # second call must not raise
