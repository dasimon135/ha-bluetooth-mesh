"""Lightweight tests for the bluetooth_mesh integration skeleton.

The manifest-sorting and const tests do NOT require Home Assistant to be
installed, so they run in the lean test environment. The config-entry
setup/unload test is guarded with ``pytest.importorskip("homeassistant")`` so
the suite stays green until full HA CI is wired up (Task B5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

INTEGRATION_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "bluetooth_mesh"
)
MANIFEST_PATH = INTEGRATION_DIR / "manifest.json"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_keys_sorted_per_hassfest() -> None:
    """hassfest requires: domain, name, then the rest alphabetical."""
    manifest = _load_manifest()
    keys = list(manifest.keys())

    assert keys[0] == "domain"
    assert keys[1] == "name"

    rest = keys[2:]
    assert rest == sorted(rest), f"manifest keys after name not sorted: {rest}"


def test_manifest_core_fields() -> None:
    manifest = _load_manifest()
    assert manifest["domain"] == "bluetooth_mesh"
    assert manifest["name"] == "Bluetooth Mesh"
    assert manifest["config_flow"] is True
    assert "bluetooth" in manifest["dependencies"]


def test_iot_class_is_polling_not_push() -> None:
    """Nothing pushes: state is read when the mesh becomes reachable.

    ``local_push`` claims the device tells Home Assistant about changes on its
    own. This integration subscribes to no unsolicited publication — it asks.
    Claiming otherwise misleads anyone reading the integration list.
    """
    assert _load_manifest()["iot_class"] == "local_polling"


def test_manifest_version_matches_the_library() -> None:
    """The integration and the vendored library ship as one version.

    Asserting a hard-coded literal here only turns every release into a failing
    test; the invariant worth guarding is that the two halves of a release move
    together, so a half-done version bump is caught instead.
    """
    import tomllib

    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert _load_manifest()["version"] == pyproject["project"]["version"]


def test_hacs_json_has_no_domains_key() -> None:
    """HACS rejects a ``domains`` key; only a small set of keys are allowed."""
    hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert "domains" not in hacs
    allowed = {
        "name",
        "content_in_root",
        "render_readme",
        "homeassistant",
        "zip_release",
        "filename",
    }
    assert set(hacs).issubset(allowed), f"unexpected hacs.json keys: {set(hacs)}"
    assert hacs["name"] == "Bluetooth Mesh"


def test_hacs_declares_a_minimum_core_version() -> None:
    """The integration uses APIs older cores do not have.

    ``entry.runtime_data`` (2024.6), the reconfigure-flow helpers
    ``_abort_if_unique_id_mismatch`` / ``async_update_reload_and_abort``
    (2024.11) and PEP 695 ``type`` aliases (Python 3.12) all fail on an older
    core; without this key HACS would happily install it and the user would
    get a traceback instead of a reason.
    """
    hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["homeassistant"] == "2024.11.0"


def test_domain_const_matches_manifest() -> None:
    """``DOMAIN`` must equal the manifest domain; importing const needs no HA."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_bluetooth_mesh_const", INTEGRATION_DIR / "const.py"
    )
    assert spec is not None and spec.loader is not None
    const = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(const)

    assert const.DOMAIN == "bluetooth_mesh"
    assert const.DOMAIN == _load_manifest()["domain"]


def test_translations_selector_and_data_description_rules() -> None:
    """strings.json: data_description values are strings (not dicts)."""
    strings = json.loads(
        (INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8")
    )
    step = strings["config"]["step"]["user"]
    for value in step.get("data_description", {}).values():
        assert isinstance(value, str)


def test_platforms_includes_light() -> None:
    """PLATFORMS must forward to the light platform. Needs HA for imports."""
    pytest.importorskip("homeassistant")

    import importlib

    module = importlib.import_module("custom_components.bluetooth_mesh")
    from homeassistant.const import Platform

    assert Platform.LIGHT in module.PLATFORMS


async def test_setup_and_unload_entry() -> None:
    """Full config-entry setup/unload round trip (skipped without HA).

    The coordinator itself is mocked (its own behaviour is covered by
    test_coordinator.py); this only asserts __init__ wires it up: build it,
    ``async_start`` it, store it in ``runtime_data``, forward the platforms,
    and ``async_stop`` it on a successful unload.
    """
    pytest.importorskip("pytest_homeassistant_custom_component")

    import importlib
    from unittest.mock import AsyncMock, MagicMock, patch

    module = importlib.import_module("custom_components.bluetooth_mesh")

    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=None)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    entry = MagicMock()
    entry.runtime_data = None

    fake_coordinator = MagicMock()
    fake_coordinator.async_start = AsyncMock(return_value=None)
    fake_coordinator.async_stop = AsyncMock(return_value=None)

    with patch.object(
        module, "MeshCoordinator", return_value=fake_coordinator
    ) as coordinator_cls:
        assert await module.async_setup_entry(hass, entry) is True
        coordinator_cls.assert_called_once_with(hass, entry)
        fake_coordinator.async_start.assert_awaited_once()
        assert entry.runtime_data is fake_coordinator
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, module.PLATFORMS
        )

        assert await module.async_unload_entry(hass, entry) is True
        hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            entry, module.PLATFORMS
        )
        fake_coordinator.async_stop.assert_awaited_once()


async def test_corrupt_entry_data_fails_the_setup_cleanly() -> None:
    """A corrupt export must not surface as a raw traceback.

    The coordinator parses the stored export in its constructor, so a damaged
    entry blew up inside async_setup_entry with a stack trace and no
    actionable message. ConfigEntryError puts a reason in the UI instead.
    """
    pytest.importorskip("homeassistant")

    import importlib
    from unittest.mock import MagicMock

    from homeassistant.exceptions import ConfigEntryError

    module = importlib.import_module("custom_components.bluetooth_mesh")
    from custom_components.bluetooth_mesh.const import CONF_CONNECT_JSON

    hass = MagicMock()
    entry = MagicMock()
    entry.data = {CONF_CONNECT_JSON: "{ not json"}
    entry.options = {}

    with pytest.raises(ConfigEntryError):
        await module.async_setup_entry(hass, entry)


async def test_no_config_entry_update_listener_is_registered() -> None:
    """The listener pattern stops working in HA 2026.12.

    Home Assistant reloads the entry itself now: the options flow is an
    ``OptionsFlowWithReload`` and the reconfigure flow schedules its own
    reload. Registering a listener on top is not merely redundant — it makes
    ``OptionsFlowWithReload`` raise.
    """
    pytest.importorskip("pytest_homeassistant_custom_component")

    import importlib
    from unittest.mock import AsyncMock, MagicMock, patch

    module = importlib.import_module("custom_components.bluetooth_mesh")

    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=None)
    entry = MagicMock()

    coordinator = MagicMock()
    coordinator.async_start = AsyncMock(return_value=None)
    with patch.object(module, "MeshCoordinator", return_value=coordinator):
        assert await module.async_setup_entry(hass, entry) is True

    entry.add_update_listener.assert_not_called()


def test_options_flow_reloads_the_entry_itself() -> None:
    """...which is what replaces the deprecated update listener."""
    pytest.importorskip("pytest_homeassistant_custom_component")

    from homeassistant.config_entries import OptionsFlowWithReload

    from custom_components.bluetooth_mesh.config_flow import (
        BluetoothMeshOptionsFlow,
    )

    assert issubclass(BluetoothMeshOptionsFlow, OptionsFlowWithReload)


async def test_corrupt_entry_data_error_is_translatable() -> None:
    """The setup failure is UI text, so it must not be hardcoded English.

    ConfigEntryError renders on the integration card, not just in the log. A
    French user got an English sentence there, because the message was built
    with an f-string instead of a translation key.
    """
    pytest.importorskip("homeassistant")

    import importlib
    import re
    from unittest.mock import MagicMock

    from homeassistant.exceptions import ConfigEntryError

    module = importlib.import_module("custom_components.bluetooth_mesh")
    from custom_components.bluetooth_mesh.const import CONF_CONNECT_JSON

    hass = MagicMock()
    entry = MagicMock()
    entry.data = {CONF_CONNECT_JSON: "{ not json"}
    entry.options = {}

    with pytest.raises(ConfigEntryError) as excinfo:
        await module.async_setup_entry(hass, entry)

    exc = excinfo.value
    assert exc.translation_domain == "bluetooth_mesh"
    strings = json.loads(
        (INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8")
    )
    message = strings["exceptions"][exc.translation_key]["message"]
    assert set(exc.translation_placeholders or {}) == set(
        re.findall(r"\{(\w+)\}", message)
    )


def test_every_shipped_language_covers_every_string() -> None:
    """A key present in strings.json but missing from a translation renders
    in English, silently, for that language only. Compare the key sets.
    """

    def _paths(obj: dict, prefix: str = "") -> set[str]:
        found: set[str] = set()
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                found |= _paths(value, path)
            else:
                found.add(path)
        return found

    reference = _paths(
        json.loads((INTEGRATION_DIR / "strings.json").read_text(encoding="utf-8"))
    )
    translations = sorted((INTEGRATION_DIR / "translations").glob("*.json"))
    assert translations, "no translations shipped"
    for path in translations:
        keys = _paths(json.loads(path.read_text(encoding="utf-8")))
        assert keys == reference, f"{path.name} diverges from strings.json"
