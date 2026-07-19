"""Windows-only stub of the unix ``fcntl`` module for local HA test runs.

Home Assistant's ``homeassistant.runner`` does an unconditional ``import
fcntl`` at module import, which explodes on native Windows (``fcntl`` is
unix-only). ``pytest-homeassistant-custom-component`` imports ``runner`` while
loading its pytest plugin, so *no* pytest process can even start in this venv on
Windows without ``fcntl`` being importable.

This directory is put on ``PYTHONPATH`` ahead of ``src`` for local Windows runs
only (see ``tests/ha/README`` / the task command). On Linux/CI it is NOT on the
path, so the real C ``fcntl`` is used and this stub is never imported. The stub
only needs to satisfy the ``import`` and the handful of names ``runner`` may
reference; the locking primitives it stubs are no-ops (single-process tests).
"""

from __future__ import annotations

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


def flock(fd, operation):  # noqa: ANN001, D103
    return None


def lockf(fd, operation, length=0, start=0, whence=0):  # noqa: ANN001, D103
    return None


def fcntl(fd, cmd, arg=0):  # noqa: ANN001, D103
    return 0


def ioctl(fd, request, arg=0, mutate_flag=True):  # noqa: ANN001, D103
    return 0
