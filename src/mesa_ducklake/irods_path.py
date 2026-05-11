"""Pure string helpers for iRODS path math.

These functions are deliberately free of side effects so they can be
imported and tested without an iRODS session. Anything that needs to
*talk* to iRODS belongs in :mod:`mesa_ducklake.lake`.
"""

from __future__ import annotations

_MESA_ENABLED_ATTRIBUTE = "mesa.enabled"
_MESA_ENABLED_VALUE = "true"


def ducklake_subpath(project_root: str) -> str:
    """Return the canonical DuckLake subpath for a project root.

    The convention is ``<project_root>/.mesa/ducklake``. Trailing slashes
    on the input are normalized away so the result is well-formed.

    Examples
    --------
    >>> ducklake_subpath("/iplant/home/alice/myproj")
    '/iplant/home/alice/myproj/.mesa/ducklake'
    >>> ducklake_subpath("/iplant/home/alice/myproj/")
    '/iplant/home/alice/myproj/.mesa/ducklake'
    """
    return f"{project_root.rstrip('/')}/.mesa/ducklake"


def mesa_avu_attribute() -> str:
    """Return the AVU attribute that marks a MESA-enabled iRODS collection."""
    return _MESA_ENABLED_ATTRIBUTE


def is_mesa_enabled(avus: list[tuple[str, str, str]]) -> bool:
    """Return ``True`` iff any AVU in ``avus`` marks the collection as MESA-enabled.

    The marker AVU is ``("mesa.enabled", "true", <unit>)``. The unit
    field is intentionally ignored — only the attribute and value are
    load-bearing.
    """
    for attribute, value, _unit in avus:
        if attribute == _MESA_ENABLED_ATTRIBUTE and value == _MESA_ENABLED_VALUE:
            return True
    return False
