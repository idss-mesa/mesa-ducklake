"""Tests for :mod:`mesa_ducklake.cache` LRU eviction."""

from __future__ import annotations

import os
from pathlib import Path

from mesa_ducklake.cache import evict_if_over, total_bytes


def _write(path: Path, size: int, *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_total_bytes_returns_zero_for_missing_root(tmp_path: Path) -> None:
    assert total_bytes(tmp_path / "does-not-exist") == 0


def test_total_bytes_sums_files_recursively(tmp_path: Path) -> None:
    _write(tmp_path / "a.parquet", 100)
    _write(tmp_path / "sub" / "b.parquet", 250)
    assert total_bytes(tmp_path) == 350


def test_evict_noop_when_under_cap(tmp_path: Path) -> None:
    _write(tmp_path / "a", 100)
    assert evict_if_over(tmp_path, 1000) == 0
    assert (tmp_path / "a").exists()


def test_evict_noop_when_root_missing(tmp_path: Path) -> None:
    assert evict_if_over(tmp_path / "nope", 1000) == 0


def test_evict_noop_when_max_bytes_zero_or_negative(tmp_path: Path) -> None:
    _write(tmp_path / "a", 100)
    assert evict_if_over(tmp_path, 0) == 0
    assert evict_if_over(tmp_path, -1) == 0
    assert (tmp_path / "a").exists()


def test_evict_removes_oldest_first(tmp_path: Path) -> None:
    """Three files; cap allows only one. Oldest two go."""
    _write(tmp_path / "old.parquet", 100, mtime=1000.0)
    _write(tmp_path / "mid.parquet", 100, mtime=2000.0)
    _write(tmp_path / "new.parquet", 100, mtime=3000.0)
    evicted = evict_if_over(tmp_path, 100)
    assert evicted == 2
    assert not (tmp_path / "old.parquet").exists()
    assert not (tmp_path / "mid.parquet").exists()
    assert (tmp_path / "new.parquet").exists()


def test_evict_stops_once_under_cap(tmp_path: Path) -> None:
    """Each file = 100 bytes; cap 150 => remove just 1."""
    _write(tmp_path / "old", 100, mtime=1000.0)
    _write(tmp_path / "new1", 100, mtime=2000.0)
    _write(tmp_path / "new2", 100, mtime=3000.0)
    evicted = evict_if_over(tmp_path, 250)
    # Total starts at 300, cap is 250 — drop one (100 bytes) -> 200 < 250.
    assert evicted == 1
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new1").exists()
    assert (tmp_path / "new2").exists()


def test_evict_walks_subdirectories(tmp_path: Path) -> None:
    _write(tmp_path / "proj1" / "a.parquet", 100, mtime=1000.0)
    _write(tmp_path / "proj2" / "b.parquet", 100, mtime=2000.0)
    evicted = evict_if_over(tmp_path, 100)
    assert evicted == 1
    assert not (tmp_path / "proj1" / "a.parquet").exists()
    assert (tmp_path / "proj2" / "b.parquet").exists()
