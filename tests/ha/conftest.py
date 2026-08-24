"""Shared fixtures/hooks for the HA integration tests.

This ``tests/ha`` tree is collected by two environments:

* the **library** venv, which has *no* Home Assistant. Collecting this tree
  there is refused outright (see ``_ALLOW_SKIP`` below) rather than skipped,
  because a skip here is a false green; and
* a venv with HA + ``pytest-homeassistant-custom-component`` (HHCC), which
  actually runs the ``hass``-backed tests. HA needs a newer Python than the
  library pins, so in practice that is a second venv.

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
import os
import sys

import pytest

_HAS_HHCC = (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
)

# Skipping this tree is a FALSE GREEN, not a neutral outcome: a run that reports
# "N passed, 10 skipped" looks like success while the entire Home Assistant half
# of the integration — config flow, coordinator, light entity, diagnostics — has
# not executed at all. That is exactly how an untested change reaches CI.
#
# So refuse to collect rather than skip quietly. Set the environment variable to
# opt out when you genuinely mean to run only the library tests.
_ALLOW_SKIP = "BTMESH_SKIP_HA_TESTS"

if not _HAS_HHCC and _ALLOW_SKIP not in os.environ:
    raise pytest.UsageError(
        "tests/ha needs Home Assistant + pytest-homeassistant-custom-component, "
        "and neither is importable here — every test in this tree would skip "
        "and the run would still report success. "
        "Run them with: pip install -r requirements-test.txt "
        "(needs Python >= 3.14; on Windows also set "
        "PYTHONPATH=tests/ha/_winshims). "
        f"Or opt out on purpose by setting {_ALLOW_SKIP}=1."
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
