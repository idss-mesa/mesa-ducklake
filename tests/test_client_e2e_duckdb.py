"""DuckLakeClient register -> record -> history/effective/diff over a DuckDB catalog.

Local-only mode (no iRODS session): Parquet lands in the local cache dir; the
catalog lives in a DuckDB file. Proves catalog + lake compose end-to-end.
"""

from datetime import UTC, datetime, timedelta

from mesa_ducklake import AvuChange, DuckLakeClient

PATH = "/iplant/home/u/proj/f.jpg"


def _change(op: str, ts: datetime) -> AvuChange:
    return AvuChange(
        irods_path=PATH, target_type="data_object",
        attribute="envo.biome", value="forest", unit="ENVO:01000228",
        op=op, actor="u", ts=ts,
    )


def test_duckdb_catalog_end_to_end(tmp_path):
    client = DuckLakeClient(
        catalog_dsn=f"duckdb:///{tmp_path / 'cat.duckdb'}",
        cache_dir=tmp_path / "cache",
    )
    project = client.register_project(irods_path="/iplant/home/u/proj", actor="u", zone="iplant")

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    s1 = client.record_changes(project.project_id, "u", [_change("add", t0)], note="add biome")
    s2 = client.record_changes(
        project.project_id, "u", [_change("delete", t0 + timedelta(hours=1))], note="remove biome"
    )

    history = client.get_history(project.project_id, PATH)
    assert len(history) == 2

    effective = client.get_avus(project.project_id, PATH)
    assert effective == []  # added then deleted -> no effective AVU

    d = client.diff(project.project_id, s1.snapshot_id, s2.snapshot_id)
    assert len(d) == 1 and d[0].op == "delete"

    client.close()
