"""Pydantic models for the mesa-ducklake public API.

These models mirror the shapes documented in ``CLAUDE.md``:

* ``AvuChange`` — one row in the Parquet ``avu_changes`` fact table.
* ``Project`` — one row in the Postgres ``mesa.projects`` catalog table.
* ``Snapshot`` — one row in the Postgres ``mesa.snapshots`` catalog table.

The AVU triple ``(attribute, value, unit)`` is canonical and matches the
shape iRODS iCAT stores and ``python-irodsclient`` returns. Never split,
merge, or rename those three fields — they are a hard contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(tz=UTC)


AvuOp = Literal["add", "delete"]
"""Allowed values for ``AvuChange.op``. Append-only: corrections are new
``delete`` rows followed by new ``add`` rows in a later snapshot."""

AvuTargetType = Literal["data_object", "collection", "resource", "user"]
"""Allowed values for ``AvuChange.target_type``. Mirrors iRODS iCAT's notion
of what an AVU can be attached to."""

ProjectStatus = Literal["active", "archived"]
"""Allowed values for ``Project.status``."""


class AvuChange(BaseModel):
    """A single AVU add/delete event bound to an iRODS path at a point in time.

    Conceptually one row in the Parquet ``avu_changes`` fact table. The
    ``project_id`` and ``snapshot_id`` may be unset on input — they are
    assigned by :meth:`DuckLakeClient.record_changes` when the snapshot is
    created.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: UUID | None = Field(
        default=None,
        description="Project UUID. Optional on input; assigned by record_changes.",
    )
    snapshot_id: int | None = Field(
        default=None,
        description="Snapshot id. Optional on input; assigned by record_changes.",
    )
    irods_path: str = Field(
        ...,
        description="Full iRODS path the AVU is bound to.",
    )
    target_type: AvuTargetType = Field(
        ...,
        description="Kind of iRODS object the AVU is attached to.",
    )
    attribute: str = Field(..., description="AVU attribute name.")
    value: str = Field(..., description="AVU value.")
    unit: str = Field(
        default="",
        description=(
            "AVU unit. Often the ontology CURIE for OBO/OLS-sourced AVUs. "
            "Empty string when no unit is supplied (matches iCAT)."
        ),
    )
    op: AvuOp = Field(..., description="Either 'add' or 'delete'.")
    actor: str = Field(..., description="iRODS username that triggered the change.")
    ts: datetime = Field(
        default_factory=_utcnow,
        description="Timestamp of the change, TZ-aware UTC.",
    )
    source: str = Field(
        default="mesa-mcp",
        description=(
            "Origin of the change. Examples: 'mesa-mcp', 'esiil-portal', "
            "'irods-rule:on_data_obj_modify', 'irods-rule:acPostProcForModifyAVUMetadata'."
        ),
    )
    via_ticket: str | None = Field(
        default=None,
        description=(
            "iRODS ticket id when the change went through a ticket-mediated session; "
            "NULL otherwise. Set by mesa-mcp's ds_use_ticket session attribute or "
            "by the rule-engine callback reading $ticketUserName."
        ),
    )
    rule_invocation: str | None = Field(
        default=None,
        description=(
            "Name of the iRODS rule that emitted this change when `source` starts "
            "with 'irods-rule:'. NULL for changes that did not originate in a rule."
        ),
    )

    @field_validator("actor", "source")
    @classmethod
    def _provenance_required(cls, v: str) -> str:
        # Provenance is mandatory (CLAUDE.md, "Conventions"): a change with
        # no author or no origin cannot be audited, so reject it here rather
        # than let it reach an append-only Parquet file where it can never
        # be corrected in place.
        if not v.strip():
            raise ValueError("must be a non-empty string (provenance is mandatory)")
        return v


class Project(BaseModel):
    """A MESA-enabled iRODS project tracked in the Postgres catalog."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID = Field(..., description="Server-generated project UUID.")
    irods_path: str = Field(
        ...,
        description="Absolute iRODS path of the project's root collection.",
    )
    irods_zone: str = Field(..., description="iRODS zone the project lives in.")
    ducklake_path: str = Field(
        ...,
        description="iRODS path of the project's '/.mesa/ducklake/' subcollection.",
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        description="When the project was registered.",
    )
    created_by: str = Field(..., description="iRODS user who registered the project.")
    status: ProjectStatus = Field(
        default="active",
        description="Lifecycle status: 'active' or 'archived'.",
    )


class Snapshot(BaseModel):
    """A single atomic batch of AVU changes — one user action, one Parquet write."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: int = Field(..., description="Server-assigned monotonic snapshot id.")
    project_id: UUID = Field(..., description="Owning project UUID.")
    ts: datetime = Field(
        default_factory=_utcnow,
        description="Snapshot timestamp, TZ-aware UTC.",
    )
    actor: str = Field(..., description="iRODS user who made the change.")
    parent_snapshot: int | None = Field(
        default=None,
        description="Previous snapshot id for this project, if any.",
    )
    note: str | None = Field(
        default=None,
        description="Optional human-readable commit message.",
    )
    parquet_file: str = Field(
        ...,
        description="Path of the Parquet file (relative to the project's ducklake_path).",
    )


PARQUET_FILE_PENDING = "pending"
"""Sentinel value for ``Snapshot.parquet_file`` while the underlying
Parquet write or iRODS push hasn't yet committed. ``DuckLakeClient``
inserts the snapshot row with this value, performs the local write +
iRODS push, then flips the column to the real relative filename. Reads
filter rows with ``parquet_file = 'pending'`` so in-flight writes are
invisible. See migration ``0002_pending_pushes.sql``."""


class PendingPush(BaseModel):
    """A row in ``mesa.pending_pushes`` — one in-flight Parquet upload.

    Inserted by :class:`DuckLakeClient` before it begins the
    ``LakeStore.write_changes`` + ``irods_sync.push`` sequence; deleted
    after the catalog row's ``parquet_file`` is flipped to its real
    filename. A surviving row after a crash signals "this snapshot
    needs its Parquet pushed to iRODS"; the recovery task drains it.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: int = Field(..., description="Snapshot the push is for.")
    local_path: str = Field(..., description="Absolute path in the local cache.")
    irods_target: str = Field(
        ...,
        description="Absolute iRODS path the Parquet should land at.",
    )
    attempts: int = Field(
        default=0,
        description="Count of push attempts made so far (0 before any try).",
    )
    last_error: str | None = Field(
        default=None,
        description="Stringified exception from the most recent failed attempt, if any.",
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        description="When this row was inserted.",
    )
