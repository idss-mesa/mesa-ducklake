"""Local cache management — internal.

mesa-ducklake's lake-on-disk is a *cache* of the Parquet files that
durably live in iRODS under each project's ``/.mesa/ducklake/``
collection. DuckDB needs real filesystem paths, so we materialize
each snapshot locally for read and write; this module caps the
total local footprint with an LRU-by-mtime eviction policy.

Called opportunistically at the *end* of a successful
``record_changes`` commit. **Never** on reads — a read populates the
cache (via :func:`mesa_ducklake.irods_sync.ensure_cached`) and we
want the next read of the same snapshot to be cheap. Evicting during
a read would defeat that.

The default cap (1 GiB) is configured by the caller — see
``DuckLakeClient.__init__``'s ``cache_cap_bytes`` parameter.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def total_bytes(root: str | Path) -> int:
    """Sum the size of every regular file under ``root``.

    Returns ``0`` when ``root`` doesn't exist; the cache is rebuildable
    from iRODS, so an absent cache directory is a normal "first run"
    state, not an error.
    """
    root = Path(root)
    if not root.exists():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def evict_if_over(root: str | Path, max_bytes: int) -> int:
    """Evict oldest-by-mtime files until total size <= ``max_bytes``.

    Parameters
    ----------
    root:
        Cache directory. Walked recursively; only regular files are
        counted/removed.
    max_bytes:
        Target cap. Values ``<= 0`` disable eviction (caller decided
        the cache is unbounded for this run).

    Returns
    -------
    int
        Number of files removed. ``0`` when the cache is already under
        the cap, when ``root`` doesn't exist, or when ``max_bytes <= 0``.

    Notes
    -----
    * Eviction is best-effort: an ``OSError`` on ``unlink`` (e.g. file
      in use) is logged and skipped rather than raised. The cache is
      rebuildable from iRODS, so partial eviction can't lose data.
    * Order is strictly ``st_mtime`` ascending. Files that were
      written most recently survive longest, matching the assumption
      that recent reads/writes predict near-future reads.
    """
    root = Path(root)
    if not root.exists() or max_bytes <= 0:
        return 0

    files = [(p, p.stat()) for p in root.rglob("*") if p.is_file()]
    total = sum(s.st_size for _, s in files)
    if total <= max_bytes:
        return 0

    files.sort(key=lambda fs: fs[1].st_mtime)
    evicted = 0
    for path, stat in files:
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= stat.st_size
            evicted += 1
        except OSError as exc:  # noqa: PERF203 — clarity wins over loop overhead
            logger.warning(
                "evict_if_over.unlink_failed path=%s error=%s",
                path,
                exc,
            )
    return evicted
