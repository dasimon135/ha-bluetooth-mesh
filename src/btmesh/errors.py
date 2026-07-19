"""Shared exception hierarchy for btmesh.

Every error raised by this package derives from :class:`BtMeshError`, so
bearer/transport layers can catch one type regardless of which module
(codec, state machine, ...) detected the problem.
"""


class BtMeshError(Exception):
    """Base class for all btmesh errors."""
