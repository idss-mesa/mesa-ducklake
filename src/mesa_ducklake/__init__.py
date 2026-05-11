"""mesa-ducklake — AVU metadata history for MESA-enabled iRODS projects.

The public API surface is intentionally narrow: import ``DuckLakeClient`` and
the Pydantic models (``AvuChange``, ``Project``, ``Snapshot``). All other
modules in this package are internal implementation detail and may change
without notice.
"""

from mesa_ducklake.client import DuckLakeClient
from mesa_ducklake.models import AvuChange, Project, Snapshot

__version__ = "0.1.0"

__all__ = [
    "AvuChange",
    "DuckLakeClient",
    "Project",
    "Snapshot",
    "__version__",
]
