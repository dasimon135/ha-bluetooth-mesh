"""The mesh proxy connection, as a Home Assistant device.

The integration holds one GATT link, to whichever node advertises this
network's Network ID, and that link occupies a Bluetooth **connection slot** on
the ESPHome proxy routing it for as long as it is held. Until this platform
existed, nothing in Home Assistant said which BLE address that was: every mesh
device is keyed on the network UUID plus a unicast address (see
:class:`~.light.MeshLight`) and carries no ``connections`` at all.

That absence has a cost outside this integration. A tool doing slot accounting
resolves a held address to a Home Assistant device and judges the link from that
device's entities; finding no device, it can only fall back to guessing from how
long the link has been quiet -- and a mesh proxy link is legitimately quiet for
hours, because it carries traffic only when something on the mesh changes. On
2026-08-28 that produced a stuck-slot report against this network's proxy link
which had been healthy the whole time.

So this platform is deliberately two things at once, and the second is the
point:

* it **names the address** on a device, so the link is resolvable; and
* it **goes unavailable exactly when the link does**, because it tracks
  ``coordinator.available`` like every other entity here -- so a reader gets a
  real health signal instead of a silence it has to interpret.

Naming the address without the second half would be worse than doing nothing: a
device whose entities never report trouble reads as permanently healthy, which
would turn one wrong alarm into a permanent blind spot.

The device is keyed on the **network**, never on a node. A mesh reaches many
nodes through one proxy, and Home Assistant treats ``connections`` as identity --
claiming the same BLE connection on several devices invites the registry to
merge them.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluetoothMeshConfigEntry
from .const import DOMAIN
from .coordinator import MeshCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothMeshConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One entity per config entry: there is exactly one proxy link."""
    async_add_entities([MeshProxySensor(entry.runtime_data)])


class MeshProxySensor(SensorEntity):
    """The BLE address this network's proxy link is currently held on."""

    _attr_has_entity_name = True
    _attr_translation_key = "proxy_address"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Pushed from the coordinator's availability callback; nothing to poll.
    _attr_should_poll = False

    def __init__(self, coordinator: MeshCoordinator) -> None:
        self._coordinator = coordinator
        identifier = coordinator.network.identifier
        self._device_key = f"{identifier}_proxy"
        self._attr_unique_id = self._device_key
        # `connections` is deliberately NOT set here. It is not known until the
        # first connect, and `device_info` is read once as the entity is added
        # -- so the address is written to the registry in `_sync_connection`
        # instead, which also lets a later address replace an earlier one.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_key)},
            name=coordinator.network.name or "Bluetooth Mesh",
            manufacturer="Bluetooth SIG Mesh",
            model="Proxy connection",
        )
        # The address last written to the registry, so an unchanged one is not
        # rewritten on every reconnect: the registry is stored on disk.
        self._synced: str | None = None

    # -------------------------------------------------------------- lifecycle

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_update)
        )
        self._sync_connection()

    @callback
    def _handle_update(self) -> None:
        """The link changed state, and possibly address: record both."""
        self._sync_connection()
        self.async_write_ha_state()

    def _sync_connection(self) -> None:
        """Point the device at the address we are actually connected through.

        ``new_connections`` **replaces** the set rather than adding to it, and
        that is the whole reason this is a registry write and not a static
        ``device_info``. A mesh proxy advertises a *random static* address:
        stable while the node is powered, free to change when it is not. Adding
        each address seen would leave the device claiming BLE connections it no
        longer has, and a reader resolving one of those stale addresses would
        name this device for a slot belonging to something else entirely.

        Silent when there is no address yet: a device claiming a MAC it has
        never reached is worse than one claiming none, because that MAC may
        belong to somebody else's device.
        """
        address = self._coordinator.proxy_address
        if address is None or address == self._synced:
            return
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self._device_key)})
        if device is None:
            # The platform creates the device from `device_info` as the entity
            # is added, so this is a race, not an error -- and raising inside an
            # availability callback would take the coordinator's notify loop
            # down with it. The next notification writes it.
            return
        registry.async_update_device(
            device.id,
            new_connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(address))},
        )
        self._synced = address

    # ------------------------------------------------------------- properties

    @property
    def available(self) -> bool:
        """Track the proxy connection, as every entity here does.

        This is the signal a slot reader judges the held link by, which is why
        it must not be softened: while the mesh is unreachable the slot really
        is held by a link that is doing nothing, and saying so is the point.
        """
        return self._coordinator.available

    @property
    def native_value(self) -> str | None:
        """The address, or ``None`` before the first successful connect."""
        return self._coordinator.proxy_address
