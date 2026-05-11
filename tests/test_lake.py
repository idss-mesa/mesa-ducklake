"""LakeStore tests — pure DuckDB/Parquet, no Postgres needed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from mesa_ducklake.lake import LakeStore
from mesa_ducklake.models import AvuChange


def _change(
    irods_path: str,
    attribute: str,
    value: str,
    *,
    op: str = "add",
    unit: str = "",
    ts: datetime,
    actor: str = "alice",
    source: str = "mesa-mcp",
    via_ticket: str | None = None,
    rule_invocation: str | None = None,
) -> AvuChange:
    return AvuChange(
        irods_path=irods_path,
        target_type="data_object",
        attribute=attribute,
        value=value,
        unit=unit,
        op=op,  # type: ignore[arg-type]
        actor=actor,
        ts=ts,
        source=source,
        via_ticket=via_ticket,
        rule_invocation=rule_invocation,
    )


def test_write_changes_emits_parquet_file(tmp_lake_root: Path) -> None:
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    relative = store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[
            _change("/p/f.csv", "envo.biome", "tundra", unit="ENVO:01000180", ts=t0),
        ],
    )
    assert relative == "snapshot_1.parquet"
    assert (tmp_lake_root / "snapshot_1.parquet").exists()


def test_write_changes_rejects_empty_list(tmp_lake_root: Path) -> None:
    store = LakeStore(tmp_lake_root)
    with pytest.raises(ValueError):
        store.write_changes(project_id=uuid4(), snapshot_id=1, changes=[])


def test_read_effective_avus_add_only(tmp_lake_root: Path) -> None:
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[
            _change("/p/f.csv", "envo.biome", "tundra", unit="ENVO:01000180", ts=t0),
            _change("/p/f.csv", "envo.biome", "desert", unit="ENVO:01000176", ts=t0),
        ],
    )

    effective = store.read_effective_avus(
        project_id=project_id,
        irods_path="/p/f.csv",
        as_of_ts=datetime.now(tz=UTC),
    )
    triples = sorted((a.attribute, a.value, a.unit) for a in effective)
    assert triples == [
        ("envo.biome", "desert", "ENVO:01000176"),
        ("envo.biome", "tundra", "ENVO:01000180"),
    ]


def test_read_effective_avus_add_then_delete(tmp_lake_root: Path) -> None:
    """An ``add`` superseded by a later ``delete`` of the same triple disappears."""
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)

    store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[
            _change("/p/f.csv", "envo.biome", "tundra", unit="ENVO:01000180", ts=t0),
        ],
    )
    store.write_changes(
        project_id=project_id,
        snapshot_id=2,
        changes=[
            _change(
                "/p/f.csv",
                "envo.biome",
                "tundra",
                unit="ENVO:01000180",
                op="delete",
                ts=t1,
            ),
        ],
    )

    effective = store.read_effective_avus(
        project_id=project_id,
        irods_path="/p/f.csv",
        as_of_ts=datetime.now(tz=UTC),
    )
    assert effective == []


def test_read_effective_avus_time_travel_before_and_after(tmp_lake_root: Path) -> None:
    """``as_of_ts`` between two snapshots shows the earlier state only."""
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 2, 1, tzinfo=UTC)

    store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[_change("/p/f.csv", "envo.biome", "tundra", ts=t0)],
    )
    store.write_changes(
        project_id=project_id,
        snapshot_id=2,
        changes=[
            _change("/p/f.csv", "envo.biome", "tundra", op="delete", ts=t1),
        ],
    )

    # Just after t0, before t1 → AVU is visible.
    between = store.read_effective_avus(
        project_id=project_id,
        irods_path="/p/f.csv",
        as_of_ts=t0 + timedelta(hours=1),
    )
    assert [(a.attribute, a.value) for a in between] == [("envo.biome", "tundra")]

    # After t1 → AVU is gone.
    after = store.read_effective_avus(
        project_id=project_id,
        irods_path="/p/f.csv",
        as_of_ts=t1 + timedelta(hours=1),
    )
    assert after == []

    # Before t0 → not yet set.
    before = store.read_effective_avus(
        project_id=project_id,
        irods_path="/p/f.csv",
        as_of_ts=t0 - timedelta(hours=1),
    )
    assert before == []


def test_read_effective_avus_empty_lake(tmp_lake_root: Path) -> None:
    """Querying an empty lake yields no rows and does not error."""
    store = LakeStore(tmp_lake_root)
    result = store.read_effective_avus(
        project_id=uuid4(),
        irods_path="/p/f.csv",
        as_of_ts=datetime.now(tz=UTC),
    )
    assert result == []


def test_read_history_returns_all_events(tmp_lake_root: Path) -> None:
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)

    store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[_change("/p/f.csv", "envo.biome", "tundra", ts=t0)],
    )
    store.write_changes(
        project_id=project_id,
        snapshot_id=2,
        changes=[
            _change("/p/f.csv", "envo.biome", "tundra", op="delete", ts=t1),
        ],
    )

    history = store.read_history(project_id, "/p/f.csv")
    # newest first
    assert [h.op for h in history] == ["delete", "add"]
    assert [h.snapshot_id for h in history] == [2, 1]


def test_diff_between_snapshots(tmp_lake_root: Path) -> None:
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = t0 + timedelta(days=1)
    t2 = t0 + timedelta(days=2)

    store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[_change("/p/f.csv", "envo.biome", "tundra", ts=t0)],
    )
    store.write_changes(
        project_id=project_id,
        snapshot_id=2,
        changes=[_change("/p/f.csv", "envo.biome", "desert", ts=t1)],
    )
    store.write_changes(
        project_id=project_id,
        snapshot_id=3,
        changes=[
            _change("/p/f.csv", "envo.biome", "desert", op="delete", ts=t2),
        ],
    )

    diff = store.diff(project_id, from_snapshot_id=1, to_snapshot_id=3)
    # Rows from snapshots 2 and 3 only — snapshot 1 is excluded.
    assert [(d.snapshot_id, d.op, d.value) for d in diff] == [
        (2, "add", "desert"),
        (3, "delete", "desert"),
    ]


def test_provenance_round_trips_through_parquet(tmp_lake_root: Path) -> None:
    """``via_ticket`` and ``rule_invocation`` survive write+read."""
    store = LakeStore(tmp_lake_root)
    project_id = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    store.write_changes(
        project_id=project_id,
        snapshot_id=1,
        changes=[
            _change(
                "/p/f.csv",
                "envo.biome",
                "tundra",
                ts=t0,
                source="irods-rule:acPostProcForModifyAVUMetadata",
                via_ticket="tkt-42",
                rule_invocation="mesa_avu_change",
            ),
        ],
    )

    history = store.read_history(project_id, "/p/f.csv")
    assert len(history) == 1
    row = history[0]
    assert row.source == "irods-rule:acPostProcForModifyAVUMetadata"
    assert row.via_ticket == "tkt-42"
    assert row.rule_invocation == "mesa_avu_change"
