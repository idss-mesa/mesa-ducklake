"""DuckDB + Parquet I/O — internal.

This module owns the data-plane: writing append-only Parquet files
(one per snapshot) and reading them back via DuckDB.

The "lake" is a directory containing one Parquet file per snapshot,
named ``snapshot_<id>.parquet``. In tests this is a ``tmp_path``
directory; in production it is each project's
``<project_root>/.mesa/ducklake/`` collection in iRODS. The
:class:`LakeStorage` protocol below is the seam where an
iRODS-backed implementation will eventually plug in (a future PR);
for now we only ship the local-filesystem implementation
:class:`LocalLakeStorage` and tests use ``tmp_path``.

This module is **internal**. Consumers should use
:class:`mesa_ducklake.DuckLakeClient`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import duckdb

from mesa_ducklake.models import STORED_ROW, AvuChange
from mesa_ducklake.queries import EFFECTIVE_AVUS_AS_OF_SQL


class LakeStorage(Protocol):
    """The minimal contract a lake backend must satisfy.

    The local-filesystem implementation (:class:`LocalLakeStorage`)
    is the only one shipped in this PR. An iRODS-backed
    implementation will land later and plug in here without
    touching :class:`LakeStore`.
    """

    def root(self) -> str:
        """Return the storage root as a DuckDB-readable URI/path."""

    def absolute_path(self, relative_name: str) -> str:
        """Resolve a relative filename to a writable absolute path."""

    def existing_parquet_files(self) -> list[str]:
        """Return absolute paths of all snapshot Parquet files currently in the lake."""


class LocalLakeStorage:
    """Local-filesystem ``LakeStorage`` — used in tests and dev."""

    def __init__(self, lake_root: str | Path) -> None:
        self._root = Path(lake_root)
        self._root.mkdir(parents=True, exist_ok=True)

    def root(self) -> str:
        return str(self._root)

    def absolute_path(self, relative_name: str) -> str:
        return str(self._root / relative_name)

    def existing_parquet_files(self) -> list[str]:
        return sorted(str(p) for p in self._root.glob("snapshot_*.parquet"))


# ---------------------------------------------------------------------------
# DuckDB schema for the in-memory staging table used during writes.
#
# Why we stage rows through a typed table before COPYing to Parquet:
# * DuckDB infers types from Python sequences in surprising ways
#   (TIMESTAMPTZ vs TIMESTAMP, INT vs BIGINT). Forcing the schema
#   makes the on-disk Parquet stable across rows.
# * Future schema-evolution additions (nullable columns only) require
#   a single source of truth. This is it.
# ---------------------------------------------------------------------------
# project_id is stored as TEXT (UUID string form) rather than DuckDB's
# native UUID type because: (a) Parquet has no first-class UUID — it
# would round-trip as FIXED_LEN_BYTE_ARRAY; (b) string compares cross
# DuckDB/Python parameter boundaries without an explicit cast. The
# Postgres catalog still uses real UUIDs where it matters for joins.
_STAGING_DDL = """
CREATE TEMP TABLE staging_changes (
    project_id      TEXT,
    snapshot_id     BIGINT,
    irods_path      TEXT,
    target_type     TEXT,
    attribute       TEXT,
    value           TEXT,
    unit            TEXT,
    op              TEXT,
    actor           TEXT,
    ts              TIMESTAMPTZ,
    source          TEXT,
    via_ticket      TEXT,
    rule_invocation TEXT
)
"""

# Column order is part of the file format. Never reorder.
_PARQUET_COLUMNS = (
    "project_id",
    "snapshot_id",
    "irods_path",
    "target_type",
    "attribute",
    "value",
    "unit",
    "op",
    "actor",
    "ts",
    "source",
    "via_ticket",
    "rule_invocation",
)


def _stored_change(**fields: Any) -> AvuChange:
    """Build an ``AvuChange`` from a row already in Parquet.

    Full validation still applies (types, enums, UUID/timestamp coercion);
    only the input-side provenance check is waived, because stored history
    is immutable and must stay readable.
    """
    return AvuChange.model_validate(fields, context={STORED_ROW: True})


class LakeStore:
    """Internal DuckDB/Parquet backend.

    Parameters
    ----------
    lake_root:
        Local filesystem directory under which snapshot Parquet files
        live. Created on construction if absent.
    """

    def __init__(self, lake_root: str | Path) -> None:
        self._storage = LocalLakeStorage(lake_root)

    # ------------------------------------------------------------------ writes

    def write_changes(
        self,
        project_id: UUID,
        snapshot_id: int,
        changes: list[AvuChange],
    ) -> str:
        """Write a Parquet file containing all ``changes`` for one snapshot.

        Returns the **relative** filename (just ``snapshot_<id>.parquet``,
        the project-local form stored in ``mesa.snapshots.parquet_file``).
        Raises :class:`ValueError` if ``changes`` is empty — empty
        snapshots are not allowed (a snapshot is one user action, and a
        no-op user action is a bug, not a row).
        """
        if not changes:
            raise ValueError("write_changes requires at least one AvuChange row")

        relative = f"snapshot_{snapshot_id}.parquet"
        abs_path = self._storage.absolute_path(relative)

        # Tuples in _PARQUET_COLUMNS order. We always stamp project_id and
        # snapshot_id from the arguments so the caller cannot accidentally
        # ship a row with the wrong owner. (DuckLakeClient.record_changes
        # also does this, but defense-in-depth is cheap.)
        rows = [
            (
                str(project_id),
                snapshot_id,
                c.irods_path,
                c.target_type,
                c.attribute,
                c.value,
                c.unit,
                c.op,
                c.actor,
                c.ts,
                c.source,
                c.via_ticket,
                c.rule_invocation,
            )
            for c in changes
        ]

        con = duckdb.connect(":memory:")
        try:
            con.execute(_STAGING_DDL)
            placeholders = ", ".join(["?"] * len(_PARQUET_COLUMNS))
            con.executemany(
                f"INSERT INTO staging_changes ({', '.join(_PARQUET_COLUMNS)}) "
                f"VALUES ({placeholders})",
                rows,
            )
            # DuckDB's COPY ... TO must take a string literal path. The path
            # is constructed by *us* from a fixed pattern + integer
            # snapshot_id, so there's no user input to escape — but we
            # still single-quote-escape defensively.
            quoted = abs_path.replace("'", "''")
            con.execute(
                "COPY (SELECT " + ", ".join(_PARQUET_COLUMNS)
                + " FROM staging_changes ORDER BY ts, attribute, value, unit) "
                f"TO '{quoted}' (FORMAT PARQUET)"
            )
        finally:
            con.close()
        return relative

    # ------------------------------------------------------------------ reads

    def _open_read_con(self) -> duckdb.DuckDBPyConnection:
        """Open an in-memory DuckDB connection ready to read the lake.

        If no Parquet files exist yet, DuckDB's ``read_parquet`` over a
        glob errors out. We work around this by creating a typed empty
        view so the caller's queries don't have to special-case
        emptiness.
        """
        con = duckdb.connect(":memory:")
        existing = self._storage.existing_parquet_files()
        if existing:
            files_sql = (
                "read_parquet(["
                + ", ".join(f"'{p.replace(chr(39), chr(39) * 2)}'" for p in existing)
                + "])"
            )
            con.execute(
                f"CREATE OR REPLACE VIEW avu_changes AS SELECT * FROM {files_sql}"
            )
        else:
            con.execute(_STAGING_DDL)
            con.execute(
                "CREATE OR REPLACE VIEW avu_changes AS SELECT * FROM staging_changes"
            )
        return con

    def _row_to_change(self, row: tuple) -> AvuChange:
        """Re-hydrate an ``AvuChange`` from a DuckDB result row.

        Result column order is fixed by ``_PARQUET_COLUMNS``.
        """
        (
            project_id,
            snapshot_id,
            irods_path,
            target_type,
            attribute,
            value,
            unit,
            op,
            actor,
            ts,
            source,
            via_ticket,
            rule_invocation,
        ) = row
        return _stored_change(
            project_id=UUID(str(project_id)) if project_id is not None else None,
            snapshot_id=snapshot_id,
            irods_path=irods_path,
            target_type=target_type,
            attribute=attribute,
            value=value,
            unit=unit or "",
            op=op,
            actor=actor,
            ts=ts,
            source=source,
            via_ticket=via_ticket,
            rule_invocation=rule_invocation,
        )

    def read_effective_avus(
        self,
        project_id: UUID,
        irods_path: str,
        as_of_ts: datetime,
    ) -> list[AvuChange]:
        """Return the effective AVU set for ``irods_path`` as of ``as_of_ts``.

        Uses :data:`mesa_ducklake.queries.EFFECTIVE_AVUS_AS_OF_SQL`
        adapted to DuckDB's ``?`` parameter style (the canonical
        template uses libpq ``$N`` placeholders).
        """
        sql = EFFECTIVE_AVUS_AS_OF_SQL.replace("$1", "?").replace("$2", "?").replace("$3", "?")
        con = self._open_read_con()
        try:
            cur = con.execute(sql, [str(project_id), irods_path, as_of_ts])
            rows = cur.fetchall()
        finally:
            con.close()
        # Effective-AVU result columns:
        #   attribute, value, unit, actor, ts, snapshot_id,
        #   source, via_ticket, rule_invocation
        results: list[AvuChange] = []
        for r in rows:
            (
                attribute,
                value,
                unit,
                actor,
                ts,
                snapshot_id,
                source,
                via_ticket,
                rule_invocation,
            ) = r
            results.append(
                _stored_change(
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    irods_path=irods_path,
                    target_type="data_object",  # not carried in the effective-AVU projection
                    attribute=attribute,
                    value=value,
                    unit=unit or "",
                    op="add",
                    actor=actor,
                    ts=ts,
                    source=source,
                    via_ticket=via_ticket,
                    rule_invocation=rule_invocation,
                )
            )
        return results

    def read_history(
        self,
        project_id: UUID,
        irods_path: str,
        limit: int = 100,
    ) -> list[AvuChange]:
        """Return up to ``limit`` change events for ``irods_path``, newest first.

        Unlike :meth:`read_effective_avus`, this returns every add/delete
        row — the raw event stream, not the reconstructed effective set.
        """
        con = self._open_read_con()
        try:
            cur = con.execute(
                f"""
                SELECT {', '.join(_PARQUET_COLUMNS)}
                FROM avu_changes
                WHERE project_id = ?
                  AND irods_path = ?
                ORDER BY ts DESC, snapshot_id DESC
                LIMIT ?
                """,
                [str(project_id), irods_path, limit],
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [self._row_to_change(r) for r in rows]

    def diff(
        self,
        project_id: UUID,
        from_snapshot_id: int,
        to_snapshot_id: int,
    ) -> list[AvuChange]:
        """Return AVU changes strictly between two snapshot ids, chronological.

        Endpoints are inclusive of ``to_snapshot_id`` and exclusive of
        ``from_snapshot_id`` (i.e. "what changed *after* from up to and
        including to"). Both endpoints must belong to the same project.
        """
        if from_snapshot_id > to_snapshot_id:
            from_snapshot_id, to_snapshot_id = to_snapshot_id, from_snapshot_id
        con = self._open_read_con()
        try:
            cur = con.execute(
                f"""
                SELECT {', '.join(_PARQUET_COLUMNS)}
                FROM avu_changes
                WHERE project_id = ?
                  AND snapshot_id > ?
                  AND snapshot_id <= ?
                ORDER BY ts ASC, snapshot_id ASC
                """,
                [str(project_id), from_snapshot_id, to_snapshot_id],
            )
            rows = cur.fetchall()
        finally:
            con.close()
        return [self._row_to_change(r) for r in rows]
