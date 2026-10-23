"""The Quack catalog backend and the ``serve-catalog`` verb.

The shared catalog contract runs against Quack through the parametrized
``store`` fixture in ``test_catalog_duckdb.py``. This module covers what
is Quack-specific: placeholder rewriting, client misconfiguration,
several writer processes against one server, the full client over Quack,
and ``serve-catalog`` argument handling.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from datetime import UTC, datetime

import duckdb
import pytest

from mesa_ducklake import AvuChange, DuckLakeClient
from mesa_ducklake.catalog import catalog_backend, open_catalog
from mesa_ducklake.catalog_quack import QuackCatalogStore, bind_as_variables
from mesa_ducklake.cli import main

from .conftest import QUACK_TEST_TOKEN

# ------------------------------------------------------------------ placeholders


def test_bind_rewrites_each_placeholder_in_order():
    assert bind_as_variables("SELECT ? , ?", 2) == (
        "SELECT getvariable('_mesa_p0') , getvariable('_mesa_p1')"
    )


def test_bind_leaves_question_marks_in_string_literals():
    sql = "SELECT 'why?' AS q, ? AS v WHERE x <> 'it''s ?'"
    assert bind_as_variables(sql, 1) == (
        "SELECT 'why?' AS q, getvariable('_mesa_p0') AS v WHERE x <> 'it''s ?'"
    )


def test_bind_rejects_a_count_mismatch():
    with pytest.raises(ValueError, match="2 placeholders but 1"):
        bind_as_variables("SELECT ?, ?", 1)


# ------------------------------------------------------------------ client config


def test_dsn_selects_quack_backend():
    assert catalog_backend("quack://localhost:9494") == "quack"


def test_quack_dsn_rejects_options():
    with pytest.raises(ValueError, match="take no options"):
        open_catalog("quack://localhost:9494?storage_version=v1.5.0")


def test_missing_token_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("MESA_DUCKLAKE_QUACK_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MESA_DUCKLAKE_QUACK_TOKEN"):
        open_catalog("quack://localhost:9494")


@pytest.mark.requires_quack
def test_server_without_mesa_schema_is_rejected(monkeypatch):
    # A Quack server that never ran the bootstrap: clients must not create
    # the schema themselves (concurrent CREATEs race), so they refuse.
    from .conftest import _free_port

    server = duckdb.connect(":memory:")
    port = _free_port()
    try:
        server.execute("LOAD quack")
        server.execute(
            "SELECT * FROM quack_serve(?, token := ?)", [f"quack:localhost:{port}", "tttt"]
        )
        with pytest.raises(RuntimeError, match="serve-catalog"):
            QuackCatalogStore(f"localhost:{port}", token="tttt")
    finally:
        server.close()


# ------------------------------------------------------------------ several writers

_WRITER = """
import sys
from mesa_ducklake.catalog import open_catalog
from mesa_ducklake.models import PARQUET_FILE_PENDING
store = open_catalog(sys.argv[1])
project = store.find_project_by_path("/iplant/home/u/shared")
for _ in range(int(sys.argv[2])):
    s = store.create_snapshot(project.project_id, "u", None, None, PARQUET_FILE_PENDING)
    store.insert_pending_push(s.snapshot_id, "/l", "/i")
    store.update_snapshot_parquet_file(s.snapshot_id, f"snapshot_{s.snapshot_id}.parquet")
    store.delete_pending_push(s.snapshot_id)
    print(s.snapshot_id)
store.close()
"""


@pytest.mark.requires_quack
def test_several_writer_processes_share_one_catalog(quack_dsn):
    store = open_catalog(quack_dsn)
    project = store.register_project("/iplant/home/u/shared", "iplant", None, "u")
    n, writers = 25, 3
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, quack_dsn, str(n)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(writers)
    ]
    ids: list[int] = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        ids += [int(line) for line in out.split()]
    try:
        assert len(ids) == len(set(ids)) == n * writers
        listed = store.list_snapshots(project.project_id, limit=1000)
        assert {s.snapshot_id for s in listed} == set(ids)
        assert store.list_pending_pushes() == []
    finally:
        store.close()


@pytest.mark.requires_quack
def test_client_end_to_end_over_quack(quack_dsn, tmp_path):
    path = "/iplant/home/u/proj/f.jpg"
    client = DuckLakeClient(catalog_dsn=quack_dsn, cache_dir=tmp_path / "cache")
    try:
        project = client.register_project(
            irods_path="/iplant/home/u/proj", actor="u", zone="iplant"
        )
        change = AvuChange(
            irods_path=path, target_type="data_object",
            attribute="envo.biome", value="forest", unit="ENVO:01000228",
            op="add", actor="u", ts=datetime(2026, 1, 1, tzinfo=UTC),
        )
        snap = client.record_changes(project.project_id, "u", [change], note="add biome")
        assert client.list_snapshots(project.project_id)[0].snapshot_id == snap.snapshot_id
        assert [a.value for a in client.get_avus(project.project_id, path)] == ["forest"]
    finally:
        client.close()


# ------------------------------------------------------------------ serve-catalog verb


def _serve(argv, monkeypatch, dsn, token=QUACK_TEST_TOKEN):
    monkeypatch.setenv("MESA_DUCKLAKE_DSN", dsn)
    if token is None:
        monkeypatch.delenv("MESA_DUCKLAKE_QUACK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MESA_DUCKLAKE_QUACK_TOKEN", token)
    out, err = io.StringIO(), io.StringIO()
    code = main(["serve-catalog", *argv], stdin=io.StringIO(""), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_serve_catalog_needs_a_token(tmp_path, monkeypatch):
    code, _, err = _serve([], monkeypatch, f"duckdb://{tmp_path / 'c.duckdb'}", token=None)
    assert code == 3
    assert json.loads(err)["code"] == "missing_token"


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://u@127.0.0.1:1/x", "quack://localhost:9494"],
)
def test_serve_catalog_only_serves_duckdb_files(dsn, monkeypatch):
    code, _, err = _serve([], monkeypatch, dsn)
    assert code == 1
    assert "DuckDB-file catalog" in json.loads(err)["message"]


@pytest.mark.parametrize("argv", [["--port"], ["--port", "x"], ["--bogus"]])
def test_serve_catalog_rejects_bad_arguments(argv, tmp_path, monkeypatch):
    code, _, err = _serve(argv, monkeypatch, f"duckdb://{tmp_path / 'c.duckdb'}")
    assert code == 1
    assert json.loads(err)["code"] == "invalid_input"


def test_migrate_accepts_a_quack_dsn_without_a_server(monkeypatch):
    # ``migrate`` on quack only checks the served schema; with nothing
    # listening it must fail as JSON, not hang or traceback.
    monkeypatch.setenv("MESA_DUCKLAKE_DSN", "quack://localhost:1")
    monkeypatch.setenv("MESA_DUCKLAKE_QUACK_TOKEN", "t")
    out, err = io.StringIO(), io.StringIO()
    code = main(["migrate"], stdin=io.StringIO(""), stdout=out, stderr=err)
    assert code == 2
    assert json.loads(err.getvalue())["code"] == "migration_failed"
