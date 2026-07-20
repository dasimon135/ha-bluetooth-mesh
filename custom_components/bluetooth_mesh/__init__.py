"""The Bluetooth Mesh integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import MeshCoordinator

PLATFORMS: list[Platform] = [Platform.LIGHT]

# The runtime data is the mesh coordinator: it owns the proxy connection, the
# controller, and the parsed network model the entities enumerate.
type BluetoothMeshConfigEntry = ConfigEntry[MeshCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> bool:
    """Set up Bluetooth Mesh from a config entry."""
    coordinator = MeshCoordinator(hass, entry)
    # Never hard-fails: async_start marks the coordinator unavailable and
    # retries in the background if no proxy is reachable yet.
    await coordinator.async_start()
    entry.runtime_data = coordinator

    # Reload when the options (keep-alive timeout) change so the coordinator
    # picks up the new value.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
