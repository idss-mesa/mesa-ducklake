"""Tests for the iRODS sync sidecar.

We mock ``session.collections`` and ``session.data_objects`` at the
boundary mesa-ducklake uses; that's enough to verify push ordering,
checksum verification, pull behavior, ensure_cached, and the full
recovery state machine without depending on a live iRODS server.

The end-to-end ``DuckLakeClient.record_changes`` flow with a mocked
session is exercised under ``@pytest.mark.requires_postgres`` so the
catalog/lake parts use a real ephemeral Postgres.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mesa_ducklake import AvuChange, DuckLakeClient
from mesa_ducklake.catalog import PostgresCatalogStore
from mesa_ducklake.irods_sync import (
    DEFAULT_MAX_ATTEMPTS,
    PARQUET_FILE_FAILED,
    ensure_cached,
    iRODSChecksumMismatch,
    iRODSSyncError,
    pull,
    push,
    recover_pending_pushes,
)
from mesa_ducklake.models import PARQUET_FILE_PENDING


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _FakeDataObject:
    """Mimic the shape of ``python-irodsclient``'s data-object record."""

    def __init__(self, checksum: str = "") -> None:
        self.checksum = checksum


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_session(
    *,
    remote_checksum_factory=None,
    put_raises: Exception | None = None,
    create_raises: Exception | None = None,
) -> MagicMock:
    """Build a mock iRODS session.

    ``remote_checksum_factory`` is called with the local path argument
    of ``put`` and must return the checksum string the mocked iRODS
    will report. Default: SHA-256 of the local file (matches what
    iRODS reports with ``sha2:`` prefix).
    """
    sess = MagicMock(name="iRODSSession")

    if create_raises is not None:
        sess.collections.create.side_effect = create_raises
    # Track which local path was passed in so the get() mock can
    # report a matching checksum.
    recorded: dict[str, str] = {}

    def _put(local: str, remote: str) -> None:  # noqa: ARG001
        if put_raises is not None:
            raise put_raises
        if remote_checksum_factory is None:
            recorded[remote] = "sha2:" + _sha256_of(Path(local))
        else:
            recorded[remote] = remote_checksum_factory(local)

    def _get(remote: str, local: str | None = None) -> Any:  # noqa: ARG001
        if local is not None:
            # pull(): write a stub file at the destination
            Path(local).write_bytes(b"stub-from-irods")
            return None
        return _FakeDataObject(checksum=recorded.get(remote, ""))

    sess.data_objects.put.side_effect = _put
    sess.data_objects.get.side_effect = _get
    return sess


# ---------------------------------------------------------------------------
# push()
# ---------------------------------------------------------------------------


def test_push_creates_parent_collection_then_puts(tmp_path: Path) -> None:
    sess = _make_session()
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    push(local, "/iplant/home/alice/proj/.mesa/ducklake/snap.parquet", session=sess)

    sess.collections.create.assert_called_once_with(
        "/iplant/home/alice/proj/.mesa/ducklake", recurse=True
    )
    sess.data_objects.put.assert_called_once_with(
        str(local), "/iplant/home/alice/proj/.mesa/ducklake/snap.parquet"
    )


def test_push_tolerates_existing_collection(tmp_path: Path) -> None:
    """PRC raising 'already exists' should not fail the push."""
    sess = _make_session(create_raises=RuntimeError("collection already exists"))
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)
    sess.data_objects.put.assert_called_once()


def test_push_propagates_collection_create_failure(tmp_path: Path) -> None:
    sess = _make_session(create_raises=RuntimeError("permission denied"))
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    with pytest.raises(iRODSSyncError, match="parent collection"):
        push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)
    sess.data_objects.put.assert_not_called()


def test_push_wraps_put_failure(tmp_path: Path) -> None:
    sess = _make_session(put_raises=RuntimeError("network blip"))
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    with pytest.raises(iRODSSyncError, match="iRODS put failed"):
        push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)


def test_push_verifies_checksum_default(tmp_path: Path) -> None:
    """Default checksum matches local SHA-256 — passes silently."""
    sess = _make_session()
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)
    # No raise == pass.


def test_push_raises_on_checksum_mismatch(tmp_path: Path) -> None:
    sess = _make_session(remote_checksum_factory=lambda _: "sha2:deadbeef" * 8)
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    with pytest.raises(iRODSChecksumMismatch):
        push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)


def test_push_accepts_md5_prefix(tmp_path: Path) -> None:
    """Older iRODS zones report ``md5:<hex>``."""
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    md5 = hashlib.md5(local.read_bytes()).hexdigest()
    sess = _make_session(remote_checksum_factory=lambda _: f"md5:{md5}")
    push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)


def test_push_accepts_unprefixed_checksum(tmp_path: Path) -> None:
    """Very old iRODS reports raw hex without an algorithm prefix."""
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    sha = hashlib.sha256(local.read_bytes()).hexdigest()
    sess = _make_session(remote_checksum_factory=lambda _: sha)
    push(local, "/iplant/p/.mesa/ducklake/snap.parquet", session=sess)


def test_push_skips_verification_when_disabled(tmp_path: Path) -> None:
    sess = _make_session(remote_checksum_factory=lambda _: "sha2:bogus" * 8)
    local = tmp_path / "snap.parquet"
    local.write_bytes(b"abc")
    push(
        local,
        "/iplant/p/.mesa/ducklake/snap.parquet",
        session=sess,
        verify_checksum=False,
    )
    sess.data_objects.get.assert_not_called()


# ---------------------------------------------------------------------------
# pull() and ensure_cached()
# ---------------------------------------------------------------------------


def test_pull_creates_parent_dirs(tmp_path: Path) -> None:
    sess = _make_session()
    dest = tmp_path / "subdir" / "snap.parquet"
    pull("/iplant/p/.mesa/ducklake/snap.parquet", dest, session=sess)
    assert dest.exists()
    sess.data_objects.get.assert_called_once_with(
        "/iplant/p/.mesa/ducklake/snap.parquet", str(dest)
    )


def test_pull_wraps_failure(tmp_path: Path) -> None:
    sess = _make_session()
    sess.data_objects.get.side_effect = RuntimeError("not found")
    with pytest.raises(iRODSSyncError, match="iRODS get failed"):
        pull("/iplant/p/.mesa/ducklake/missing.parquet", tmp_path / "x", session=sess)


def test_ensure_cached_pulls_missing_only(tmp_path: Path) -> None:
    sess = _make_session()
    (tmp_path / "snapshot_1.parquet").write_bytes(b"already-cached")
    pulled = ensure_cached(
        tmp_path,
        "/iplant/p/.mesa/ducklake",
        ["snapshot_1.parquet", "snapshot_2.parquet"],
        session=sess,
    )
    assert pulled == ["snapshot_2.parquet"]
    assert (tmp_path / "snapshot_2.parquet").exists()
    sess.data_objects.get.assert_called_once()


def test_ensure_cached_skips_sentinels(tmp_path: Path) -> None:
    """``'pending'`` and ``'failed'`` are not real filenames."""
    sess = _make_session()
    pulled = ensure_cached(
        tmp_path,
        "/iplant/p/.mesa/ducklake",
        [PARQUET_FILE_PENDING, PARQUET_FILE_FAILED, "snapshot_1.parquet"],
        session=sess,
    )
    assert pulled == ["snapshot_1.parquet"]


def test_ensure_cached_creates_lake_root(tmp_path: Path) -> None:
    sess = _make_session()
    lake_root = tmp_path / "fresh-cache"
    assert not lake_root.exists()
    ensure_cached(lake_root, "/iplant/p/.mesa/ducklake", [], session=sess)
    assert lake_root.exists()


# ---------------------------------------------------------------------------
# recover_pending_pushes — state machine
# ---------------------------------------------------------------------------

pytestmark_pg = pytest.mark.requires_postgres


@pytest.mark.requires_postgres
def test_recover_drains_committed_rows(catalog_db: str, tmp_path: Path) -> None:
    """Snapshot already committed — drop the WAL row."""
    catalog = PostgresCatalogStore(catalog_db)
    project = catalog.register_project(
        irods_path="/iplant/p/r1",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file="snapshot_1.parquet",  # already committed
    )
    catalog.insert_pending_push(snap.snapshot_id, str(tmp_path / "x"), "/i/x")
    sess = _make_session()

    summary = recover_pending_pushes(catalog, sess)

    assert summary["committed"] == 1
    assert catalog.get_pending_push(snap.snapshot_id) is None


@pytest.mark.requires_postgres
def test_recover_retries_pushes_and_commits(catalog_db: str, tmp_path: Path) -> None:
    """Pending row + local file present + push succeeds -> commit."""
    catalog = PostgresCatalogStore(catalog_db)
    project = catalog.register_project(
        irods_path="/iplant/p/r2",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file=PARQUET_FILE_PENDING,
    )
    local = tmp_path / "snapshot_99.parquet"
    local.write_bytes(b"xyz")
    catalog.insert_pending_push(snap.snapshot_id, str(local), "/i/snap.parquet")
    sess = _make_session()

    summary = recover_pending_pushes(catalog, sess)

    assert summary["pushed"] == 1
    assert catalog.get_pending_push(snap.snapshot_id) is None
    committed = catalog.get_snapshot(snap.snapshot_id)
    assert committed is not None
    assert committed.parquet_file == "snapshot_99.parquet"


@pytest.mark.requires_postgres
def test_recover_bumps_attempts_on_push_failure(
    catalog_db: str, tmp_path: Path
) -> None:
    catalog = PostgresCatalogStore(catalog_db)
    project = catalog.register_project(
        irods_path="/iplant/p/r3",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file=PARQUET_FILE_PENDING,
    )
    local = tmp_path / "snapshot_1.parquet"
    local.write_bytes(b"x")
    catalog.insert_pending_push(snap.snapshot_id, str(local), "/i/snap.parquet")
    sess = _make_session(put_raises=RuntimeError("iRODS unreachable"))

    summary = recover_pending_pushes(catalog, sess)

    assert summary["pushed"] == 0
    pending = catalog.get_pending_push(snap.snapshot_id)
    assert pending is not None
    assert pending.attempts == 1
    assert pending.last_error and "unreachable" in pending.last_error


@pytest.mark.requires_postgres
def test_recover_marks_failed_after_max_attempts(
    catalog_db: str, tmp_path: Path
) -> None:
    catalog = PostgresCatalogStore(catalog_db)
    project = catalog.register_project(
        irods_path="/iplant/p/r4",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file=PARQUET_FILE_PENDING,
    )
    local = tmp_path / "snapshot_1.parquet"
    local.write_bytes(b"x")
    catalog.insert_pending_push(snap.snapshot_id, str(local), "/i/snap.parquet")
    # Pre-bump attempts to the cap so this run is the "give up" iteration.
    for _ in range(DEFAULT_MAX_ATTEMPTS):
        catalog.bump_pending_push_attempt(snap.snapshot_id, "earlier failure")
    sess = _make_session()

    summary = recover_pending_pushes(catalog, sess)

    assert summary["failed"] == 1
    assert catalog.get_pending_push(snap.snapshot_id) is None
    final = catalog.get_snapshot(snap.snapshot_id)
    assert final is not None
    assert final.parquet_file == PARQUET_FILE_FAILED


@pytest.mark.requires_postgres
def test_recover_handles_missing_local(catalog_db: str, tmp_path: Path) -> None:
    """Local cache lost the Parquet — bump attempts and move on."""
    catalog = PostgresCatalogStore(catalog_db)
    project = catalog.register_project(
        irods_path="/iplant/p/r5",
        irods_zone="iplant",
        ducklake_path=None,
        created_by="alice",
    )
    snap = catalog.create_snapshot(
        project_id=project.project_id,
        actor="alice",
        parent_snapshot=None,
        note=None,
        parquet_file=PARQUET_FILE_PENDING,
    )
    missing_local = tmp_path / "does-not-exist.parquet"
    catalog.insert_pending_push(snap.snapshot_id, str(missing_local), "/i/x.parquet")
    sess = _make_session()

    summary = recover_pending_pushes(catalog, sess)

    assert summary["missing_local"] == 1
    pending = catalog.get_pending_push(snap.snapshot_id)
    assert pending is not None
    assert pending.attempts == 1


# ---------------------------------------------------------------------------
# DuckLakeClient.record_changes end-to-end with mocked iRODS
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
def test_record_changes_pushes_to_irods_then_commits(
    catalog_db: str, tmp_path: Path
) -> None:
    sess = _make_session()
    client = DuckLakeClient(
        postgres_dsn=catalog_db,
        irods_session=sess,
        lake_root_override=tmp_path,
    )
    project = client.register_project(
        irods_path="/iplant/home/alice/r6",
        actor="alice",
        zone="iplant",
    )
    change = AvuChange(
        irods_path="/iplant/home/alice/r6/data/file.csv",
        target_type="data_object",
        attribute="color",
        value="blue",
        unit="",
        op="add",
        actor="alice",
    )
    snap = client.record_changes(project.project_id, "alice", [change], note="first")

    # Local Parquet exists in the cache directory for this project.
    project_cache = tmp_path / str(project.project_id)
    assert (project_cache / snap.parquet_file).exists()

    # iRODS push was invoked.
    sess.data_objects.put.assert_called_once()
    args, _ = sess.data_objects.put.call_args
    local_arg, irods_arg = args
    assert local_arg == str(project_cache / snap.parquet_file)
    assert irods_arg.endswith("/" + snap.parquet_file)
    assert irods_arg.startswith(project.ducklake_path)

    # Catalog row is committed (no 'pending' sentinel).
    assert snap.parquet_file != PARQUET_FILE_PENDING
    assert snap.parquet_file.startswith("snapshot_")

    # WAL is drained.
    assert (
        PostgresCatalogStore(catalog_db).get_pending_push(snap.snapshot_id) is None
    )


@pytest.mark.requires_postgres
def test_record_changes_keeps_wal_row_when_push_fails(
    catalog_db: str, tmp_path: Path, monkeypatch
) -> None:
    """Crash simulation: push raises after local write; pending stays."""
    sess = _make_session(put_raises=RuntimeError("iRODS down"))
    client = DuckLakeClient(
        postgres_dsn=catalog_db,
        irods_session=sess,
        lake_root_override=tmp_path,
    )
    project = client.register_project(
        irods_path="/iplant/home/alice/r7",
        actor="alice",
        zone="iplant",
    )
    change = AvuChange(
        irods_path="/iplant/home/alice/r7/file.csv",
        target_type="data_object",
        attribute="k",
        value="v",
        unit="",
        op="add",
        actor="alice",
    )
    with pytest.raises(iRODSSyncError):
        client.record_changes(project.project_id, "alice", [change])

    catalog = PostgresCatalogStore(catalog_db)
    # Catalog row stays in 'pending'.
    snaps = catalog.list_snapshots(project.project_id, include_pending=True)
    assert len(snaps) == 1
    assert snaps[0].parquet_file == PARQUET_FILE_PENDING
    # WAL row survives for recovery.
    pending = catalog.get_pending_push(snaps[0].snapshot_id)
    assert pending is not None

    # Now a successful recover() drains the WAL and commits.
    healthy = _make_session()
    summary = recover_pending_pushes(catalog, healthy)
    assert summary["pushed"] == 1
    committed = catalog.get_snapshot(snaps[0].snapshot_id)
    assert committed is not None
    assert committed.parquet_file.startswith("snapshot_")


@pytest.mark.requires_postgres
def test_record_changes_local_only_when_no_session(
    catalog_db: str, tmp_path: Path
) -> None:
    """irods_session=None and no per-call session => local-only mode.

    The WAL is NOT touched, the iRODS session methods are not invoked,
    and on lake failure the snapshot row is rolled back (pre-sync
    shape).
    """
    client = DuckLakeClient(
        postgres_dsn=catalog_db,
        irods_session=None,
        lake_root_override=tmp_path,
    )
    project = client.register_project(
        irods_path="/iplant/home/alice/r8",
        actor="alice",
        zone="iplant",
    )
    change = AvuChange(
        irods_path="/iplant/home/alice/r8/file.csv",
        target_type="data_object",
        attribute="k",
        value="v",
        unit="",
        op="add",
        actor="alice",
    )
    snap = client.record_changes(project.project_id, "alice", [change])
    assert snap.parquet_file.startswith("snapshot_")
    catalog = PostgresCatalogStore(catalog_db)
    assert catalog.list_pending_pushes() == []


def test_recover_pending_pushes_requires_session() -> None:
    client = DuckLakeClient(postgres_dsn="postgresql://stub", irods_session=None)
    with pytest.raises(RuntimeError, match="requires an iRODS session"):
        client.recover_pending_pushes()
