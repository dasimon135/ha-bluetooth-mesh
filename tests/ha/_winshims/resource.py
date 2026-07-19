"""Windows-only stub of the unix ``resource`` module for local HA test runs.

``homeassistant.util.resource`` imports ``resource`` unconditionally; like the
sibling ``fcntl`` stub it exists only so the HA test plugin can import on native
Windows. Not on the path under Linux/CI, where the real module is used.
``set_open_file_descriptor_limit`` uses getrlimit/setrlimit — the stub returns a
generous limit and accepts sets so the call is a harmless no-op.
"""

from __future__ import annotations

RLIMIT_NOFILE = 7
RLIM_INFINITY = -1

_LIMITS: dict[int, tuple[int, int]] = {RLIMIT_NOFILE: (1024, 1024)}


def getrlimit(resource_id):  # noqa: ANN001, D103
    return _LIMITS.get(resource_id, (1024, 1024))


def setrlimit(resource_id, limits):  # noqa: ANN001, D103
    _LIMITS[resource_id] = tuple(limits)  # type: ignore[assignment]
    return None
