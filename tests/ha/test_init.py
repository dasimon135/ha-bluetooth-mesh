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

    from unittest.mock import AsyncMock, MagicMock, patch

    import importlib

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
