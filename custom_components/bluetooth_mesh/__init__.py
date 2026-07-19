"""The Bluetooth Mesh integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.LIGHT]

# The runtime data type is a placeholder for now. A later task replaces this
# with the mesh coordinator instance.
type BluetoothMeshConfigEntry = ConfigEntry[dict]


async def async_setup_entry(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> bool:
    """Set up Bluetooth Mesh from a config entry."""
    # Placeholder runtime data. Replaced by the coordinator in a later task.
    entry.runtime_data = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
