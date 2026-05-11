"""Effective-AVU reconstruction queries — internal.

This module holds the canonical SQL templates that turn the append-only
``avu_changes`` fact table into the *effective* AVU set for a path at a
point in time. Execution wiring lives in :mod:`mesa_ducklake.lake` and
:mod:`mesa_ducklake.time_travel`; this file is the single source of
truth for the query shapes themselves.

The query shape is part of the project's contract: any change must be
mirrored in the time-travel tests in ``tests/test_time_travel.py``.
"""

from __future__ import annotations

EFFECTIVE_AVUS_AS_OF_SQL = """\
WITH events AS (
    SELECT attribute, value, unit, op, ts, actor, snapshot_id,
           source, via_ticket, rule_invocation,
           ROW_NUMBER() OVER (
               PARTITION BY attribute, value, unit
               ORDER BY ts DESC, snapshot_id DESC
           ) AS rn
    FROM avu_changes
    WHERE project_id = $1
      AND irods_path = $2
      AND ts <= $3
)
SELECT attribute, value, unit, actor, ts, snapshot_id,
       source, via_ticket, rule_invocation
FROM events
WHERE rn = 1 AND op = 'add';
"""
"""Reconstruct the effective AVU set for one path as of a timestamp.

The partition key is the full triple ``(attribute, value, unit)`` —
**never** ``attribute`` alone. iRODS permits multiple values per
attribute, so partitioning on the attribute alone would silently drop
valid AVUs.

Parameters (positional, libpq-style):

1. ``project_id`` — UUID of the owning project.
2. ``irods_path`` — full iRODS path of the AVU target.
3. ``ts`` — TZ-aware UTC timestamp; rows with ``ts > $3`` are ignored.
"""
