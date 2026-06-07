"""iRODS sync sidecar — internal.

Why this module exists: DuckDB's ``COPY (...) TO 'path'`` and
``read_parquet([...])`` need real filesystem paths in SQL — there's
no bytes-stream API we can plug iRODS into directly (see
:mod:`mesa_ducklake.lake`). The sidecar pattern keeps
:class:`LocalLakeStorage` exactly as it is (writes/reads against a
local cache directory) and replicates each Parquet file into iRODS
so the file durably lives where the design says it should:
``<project_root>/.mesa/ducklake/`` next to the data it describes.

Write protocol (driven by :class:`DuckLakeClient.record_changes`):

1. Allocate a snapshot row with ``parquet_file='pending'``.
2. Insert a ``mesa.pending_pushes`` row (the WAL).
3. ``LakeStore.write_changes`` produces ``snapshot_<id>.parquet`` in
   the local cache.
4. :func:`push` uploads the local file to
   ``<ducklake_path>/snapshot_<id>.parquet``.
5. Update the snapshot row's ``parquet_file`` to the real name —
   **this is the commit point**.
6. Delete the pending-push row.

A crash anywhere from step 2 onwards leaves the pending row in
place; :func:`recover_pending_pushes` drains it: re-push to iRODS if
absent there, then commit the catalog row, then delete the pending
row. The whole sequence is idempotent because:

* ``data_objects.put`` overwrites, and the Parquet body is
  deterministic given the ``ORDER BY`` in :mod:`mesa_ducklake.lake`.
* ``update_snapshot_parquet_file`` is a single UPDATE; re-running it
  with the same value is a no-op.
* ``delete_pending_push`` is idempotent (DELETE WHERE).

Read protocol: :func:`ensure_cached` diffs the catalog's expected
filenames against ``LocalLakeStorage.existing_parquet_files()`` and
pulls anything missing. Called before each DuckDB read.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mesa_ducklake.models import PARQUET_FILE_PENDING

if TYPE_CHECKING:
    from mesa_ducklake.catalog_base import CatalogStore

logger = logging.getLogger(__name__)

# After this many failed attempts the recovery task gives up and marks
# the snapshot ``parquet_file='failed'`` so it stays invisible to normal
# reads (which already filter ``'pending'`` and any non-realistic name).
DEFAULT_MAX_ATTEMPTS = 5

# Sentinel written into ``mesa.snapshots.parquet_file`` when recovery
# has given up on a snapshot. Operators inspect via
# ``CatalogStore.list_snapshots(include_pending=True)``.
PARQUET_FILE_FAILED = "failed"

# How big a read chunk to use when hashing a local Parquet for
# checksum verification. Tuned for I/O coalescing on typical SSDs.
_HASH_CHUNK = 64 * 1024


class iRODSSyncError(Exception):
    """Raised when an iRODS-side operation fails."""


class iRODSChecksumMismatch(iRODSSyncError):
    """Raised when the iRODS-side checksum disagrees with the local file.

    Carries both values in the message so operators can spot a
    truncated upload or partial write.
    """


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def _file_hashes(local_path: str | Path) -> dict[str, str]:
    """Compute SHA-256 and MD5 of a local file.

    iRODS reports checksums with an algorithm prefix (``sha2:...`` or
    ``md5:...``) in 4.3+ and as a raw value in older zones. We compute
    both so the caller can match whichever form iRODS hands back.
    """
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    with Path(local_path).open("rb") as fp:
        while True:
            chunk = fp.read(_HASH_CHUNK)
            if not chunk:
                break
            sha.update(chunk)
            md5.update(chunk)
    return {"sha256": sha.hexdigest(), "md5": md5.hexdigest()}


def _checksum_matches(local_path: str | Path, remote_checksum: str) -> bool:
    """Return True iff iRODS's checksum string matches the local file."""
    if not remote_checksum:
        # iRODS didn't compute one (older zones may skip on small files).
        # Treat as "nothing to verify" rather than failing — the push
        # itself is still atomic from iRODS's perspective.
        return True
    sums = _file_hashes(local_path)
    text = remote_checksum.strip()
    algo, sep, value = text.partition(":")
    if not sep:
        # Older format: raw hex with no prefix. Try both.
        target = text.lower()
        return target in (sums["sha256"], sums["md5"])
    expected = value.lower()
    algo = algo.lower()
    # iRODS prefixes: "sha2" = SHA-256.
    if algo in {"sha2", "sha256"}:
        return expected == sums["sha256"]
    if algo == "md5":
        return expected == sums["md5"]
    # Unknown algorithm — log and pass through rather than block the
    # commit; surfacing as success here keeps a future iRODS-algo
    # rollout from breaking us.
    logger.warning(
        "irods_sync.unknown_checksum_algo algo=%s value=%s",
        algo,
        expected[:16],
    )
    return True


# ---------------------------------------------------------------------------
# Push / pull
# ---------------------------------------------------------------------------


def _ensure_parent_collection(session: Any, irods_target: str) -> None:
    """Create the iRODS collection that will hold ``irods_target``.

    Tolerates "already exists" errors. PRC's
    ``session.collections.create(..., recurse=True)`` walks up the
    parent chain creating missing collections, which is what we want.
    """
    parent = irods_target.rsplit("/", 1)[0]
    if not parent:
        return
    try:
        session.collections.create(parent, recurse=True)
    except Exception as exc:  # noqa: BLE001 - PRC raises a mix of types
        msg = str(exc).lower()
        if "exist" in msg or "duplicate" in msg:
            return
        raise iRODSSyncError(
            f"failed to ensure parent collection {parent!r}: {exc}"
        ) from exc


def push(
    local_path: str | Path,
    irods_target: str,
    *,
    session: Any,
    verify_checksum: bool = True,
) -> None:
    """Upload a local Parquet file to iRODS at ``irods_target``.

    Idempotent: re-pushing the same content over an existing iRODS
    object overwrites it (PRC's default put behavior). Combined with
    the deterministic ``ORDER BY`` in
    :func:`mesa_ducklake.lake.LakeStore.write_changes`, re-running
    push for the same snapshot is safe.

    Raises :class:`iRODSSyncError` when iRODS rejects the put; raises
    :class:`iRODSChecksumMismatch` when post-upload verification finds
    the bytes diverged from local (silent corruption).
    """
    local = str(local_path)
    _ensure_parent_collection(session, irods_target)

    try:
        session.data_objects.put(local, irods_target)
    except Exception as exc:  # noqa: BLE001
        raise iRODSSyncError(
            f"iRODS put failed for {irods_target!r}: {exc}"
        ) from exc

    if not verify_checksum:
        return

    try:
        obj = session.data_objects.get(irods_target)
        remote_checksum = getattr(obj, "checksum", None) or ""
    except Exception as exc:  # noqa: BLE001
        # We have to surface this — silent corruption would defeat the
        # whole point of the verification step.
        raise iRODSSyncError(
            f"iRODS object fetch for verification failed "
            f"{irods_target!r}: {exc}"
        ) from exc

    if not _checksum_matches(local, remote_checksum):
        raise iRODSChecksumMismatch(
            f"iRODS checksum mismatch for {irods_target!r}: "
            f"server reported {remote_checksum!r}"
        )


def pull(
    irods_path: str,
    local_dest: str | Path,
    *,
    session: Any,
) -> None:
    """Download a Parquet file from iRODS into the local cache.

    Raises :class:`iRODSSyncError` on any underlying PRC failure.
    The destination's parent directory is created if absent so the
    caller doesn't have to.
    """
    dest = Path(local_dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        session.data_objects.get(irods_path, str(dest))
    except Exception as exc:  # noqa: BLE001
        raise iRODSSyncError(
            f"iRODS get failed for {irods_path!r}: {exc}"
        ) from exc


def ensure_cached(
    lake_root: str | Path,
    irods_root: str,
    expected_files: list[str],
    *,
    session: Any,
) -> list[str]:
    """Pull any of ``expected_files`` missing from the local cache.

    Parameters
    ----------
    lake_root:
        Local cache directory (per-project, normally
        ``<cache_dir>/<project_id>/``).
    irods_root:
        iRODS collection holding the project's Parquet snapshots
        (i.e. ``project.ducklake_path``).
    expected_files:
        Relative filenames the catalog says exist for this project
        (e.g. ``["snapshot_1.parquet", "snapshot_3.parquet"]``).
    session:
        Live iRODS session.

    Returns
    -------
    list[str]
        The relative filenames that were pulled this call. Empty when
        the local cache already had everything.

    Notes
    -----
    Skips ``expected_files`` whose name is one of the sentinel values
    (``"pending"``, ``"failed"``) — those rows don't refer to real
    Parquet files in iRODS yet.
    """
    root = Path(lake_root)
    root.mkdir(parents=True, exist_ok=True)
    pulled: list[str] = []
    for name in expected_files:
        if name in {PARQUET_FILE_PENDING, PARQUET_FILE_FAILED}:
            continue
        local = root / name
        if local.exists():
            continue
        remote = f"{irods_root.rstrip('/')}/{name}"
        pull(remote, local, session=session)
        pulled.append(name)
    return pulled


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def recover_pending_pushes(
    catalog: "CatalogStore",
    session: Any,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    limit: int = 100,
) -> dict[str, int]:
    """Drain ``mesa.pending_pushes``, retrying each in-flight push.

    For each pending row this routine takes one of five paths:

    * **committed already** — the catalog row's ``parquet_file`` is no
      longer ``'pending'`` (a previous run committed but crashed before
      cleaning up the pending row). Drop the pending row.
    * **orphaned** — the snapshot row no longer exists. Drop the
      pending row. (``ON DELETE CASCADE`` makes this rare, but a
      direct ``DELETE`` against ``mesa.snapshots`` outside the
      catalog API would create one.)
    * **attempts exhausted** — ``attempts >= max_attempts``. Mark the
      snapshot ``parquet_file='failed'`` so it stays invisible to
      normal reads; operators see it via
      ``list_snapshots(include_pending=True)``.
    * **local file missing** — the cache lost the Parquet (eviction
      bug or operator wipe). Bump attempts and move on; if iRODS
      already has a previous copy, the next read will use that;
      otherwise the snapshot is unrecoverable and the attempt cap
      will eventually mark it ``'failed'``.
    * **retry** — push to iRODS, then flip ``parquet_file`` to the
      real name, then delete the pending row.

    Returns a summary dict ``{"pushed", "committed", "failed",
    "orphaned", "missing_local"}`` for telemetry/test assertions.
    """
    pending = catalog.list_pending_pushes(limit=limit)
    summary = {
        "pushed": 0,
        "committed": 0,
        "failed": 0,
        "orphaned": 0,
        "missing_local": 0,
    }

    for row in pending:
        snapshot = catalog.get_snapshot(row.snapshot_id)
        if snapshot is None:
            catalog.delete_pending_push(row.snapshot_id)
            summary["orphaned"] += 1
            continue

        if snapshot.parquet_file != PARQUET_FILE_PENDING:
            # Already committed — drain the WAL.
            catalog.delete_pending_push(row.snapshot_id)
            summary["committed"] += 1
            continue

        if row.attempts >= max_attempts:
            catalog.update_snapshot_parquet_file(
                row.snapshot_id, PARQUET_FILE_FAILED
            )
            catalog.delete_pending_push(row.snapshot_id)
            summary["failed"] += 1
            logger.error(
                "irods_sync.recover.gave_up snapshot=%s attempts=%s last_error=%r",
                row.snapshot_id,
                row.attempts,
                row.last_error,
            )
            continue

        local_path = Path(row.local_path)
        if not local_path.exists():
            catalog.bump_pending_push_attempt(
                row.snapshot_id,
                f"local parquet missing: {local_path}",
            )
            summary["missing_local"] += 1
            continue

        try:
            push(local_path, row.irods_target, session=session)
        except Exception as exc:  # noqa: BLE001
            catalog.bump_pending_push_attempt(row.snapshot_id, str(exc))
            logger.warning(
                "irods_sync.recover.push_failed snapshot=%s error=%s",
                row.snapshot_id,
                exc,
            )
            continue

        relative = local_path.name
        catalog.update_snapshot_parquet_file(row.snapshot_id, relative)
        catalog.delete_pending_push(row.snapshot_id)
        summary["pushed"] += 1

    return summary
