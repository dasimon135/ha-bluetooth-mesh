"""The Bluetooth Mesh integration."""

from __future__ import annotations

import json

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .btmesh.network_model import NetworkModelError
from .const import DOMAIN
from .coordinator import MeshCoordinator

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SENSOR]

# The runtime data is the mesh coordinator: it owns the proxy connection, the
# controller, and the parsed network model the entities enumerate.
type BluetoothMeshConfigEntry = ConfigEntry[MeshCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> bool:
    """Set up Bluetooth Mesh from a config entry."""
    try:
        coordinator = MeshCoordinator(hass, entry)
    except (json.JSONDecodeError, NetworkModelError, KeyError) as exc:
        # The stored export is parsed in the constructor, so a damaged entry
        # used to blow up here as a raw traceback with nothing actionable in
        # it. Re-importing the export (the reconfigure flow) is the fix, and
        # that is what the user needs to be told.
        #
        # This renders on the integration card, so it is UI text: it goes
        # through a translation key. Only the parser's own detail stays
        # English — it names JSON fields that are English in the file itself.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="corrupt_connect_export",
            translation_placeholders={"error": str(exc)},
        ) from exc
    # Never hard-fails: async_start marks the coordinator unavailable and
    # retries in the background if no proxy is reachable yet.
    await coordinator.async_start()
    entry.runtime_data = coordinator

    # No config-entry update listener: the options flow is an
    # ``OptionsFlowWithReload`` and reloads the entry itself, and the
    # reconfigure flow schedules its own reload. Home Assistant reports the
    # listener pattern as deprecated and stops honouring it in 2026.12.

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
