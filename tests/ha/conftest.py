"""Shared fixtures/hooks for the HA integration tests.

This ``tests/ha`` tree is collected by two environments:

* the **library** venv (``uv run pytest tests``) which has *no* Home Assistant —
  only the HA-independent parts of ``test_init.py`` run there (the rest
  ``importorskip`` HA); and
* the **daikin_madoka** venv which has HA + ``pytest-homeassistant-custom-
  component`` (HHCC) and actually runs the ``hass``-backed tests.

So everything HA-specific here is gated on HHCC being importable, otherwise the
library collection would blow up importing ``pytest_socket`` / requesting the
``enable_custom_integrations`` fixture.

Windows-local caveat: HHCC calls ``pytest_socket.disable_socket(allow_unix_
socket=True)`` in its setup hook. On Linux/CI the asyncio event-loop self-pipe
uses an ``AF_UNIX`` socketpair (allowed), so the ``hass`` fixture builds. On
native Windows there is no ``AF_UNIX``: the ProactorEventLoop self-pipe falls
back to an ``AF_INET`` socketpair, which the guard blocks — so no event loop,
hence no ``hass`` fixture, can be created. Neutralising ``disable_socket`` on
Windows keeps sockets usable; the tests are mock-only (no real network), and
Linux/CI keeps HHCC's socket safety net untouched.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

_HAS_HHCC = (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
)

if _HAS_HHCC and sys.platform == "win32":  # pragma: no cover - platform shim
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None


if _HAS_HHCC:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Make custom_components/ importable for the test hass instance."""
        yield


if _HAS_HHCC and sys.platform == "win32":  # pragma: no cover - platform shim

    @pytest.fixture(autouse=True)
    def _mock_bluetooth_history():
        """Stub the local BlueZ adapter history on Windows.

        Setting up the ``bluetooth`` dependency (e.g. when a config flow that
        depends on it is initialised) calls ``async_load_history_from_system``,
        which reads ``LinuxAdapters.history``. That path goes through
        ``dbus_fast``'s ``unpack_variants`` — absent on native Windows, so the
        attribute is ``None`` and the call raises ``TypeError``. HHCC's session
        ``mock_bluetooth_adapters`` fixture already stubs ``.adapters`` and
        ``.refresh`` the same way but leaves ``.history`` untouched; patch it to
        an empty mapping so bluetooth setup completes with no cached history.
        """
        from unittest.mock import patch

        with patch(
            "bluetooth_adapters.systems.linux.LinuxAdapters.history", {}
        ):
            yield
