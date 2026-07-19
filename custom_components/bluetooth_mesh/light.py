"""Light platform for the Bluetooth Mesh integration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluetoothMeshConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothMeshConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up mesh lights from a config entry.

    Skeleton only: no entities are added yet. Real light entities are wired
    up in a later task once the coordinator exposes the mesh nodes.
    """
    return
