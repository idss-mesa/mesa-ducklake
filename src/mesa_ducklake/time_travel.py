"""Small time-travel helpers — internal.

The actual time-travel queries live in :mod:`mesa_ducklake.queries`
and are executed in :mod:`mesa_ducklake.lake`. This module holds the
one or two pure utilities that don't belong on either side of that
boundary.

Kept as a separate module so future helpers (snapshot-id-based
time travel, history pagination) have a natural home.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_as_of(value: str | datetime) -> datetime:
    """Normalize an "as-of" timestamp into a TZ-aware UTC datetime.

    Accepts either a :class:`datetime` (already TZ-aware UTC, or naive,
    in which case UTC is assumed) or an RFC-3339-ish string. ``Z``
    suffix is supported (Python's ``fromisoformat`` only learned it
    in 3.11+, but we require Python 3.11).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    # str path
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
