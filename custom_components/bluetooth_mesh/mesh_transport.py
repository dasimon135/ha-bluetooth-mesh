"""HA-native bridge from Home Assistant's bluetooth stack to a mesh GattBearer.

The ``btmesh`` library is transport-agnostic: :class:`btmesh.bearer.GattBearer`
wraps any connected bleak-compatible GATT client and speaks the Mesh Proxy
protocol. In ``phase0`` the standalone :class:`btmesh.bearer.EsphomeTransport`
brings up bleak-esphome itself to scan for and connect to a proxy node. Inside
Home Assistant that machinery already exists — the ``bluetooth`` integration
runs the ESPHome proxies and maintains the discovery snapshot — so this module
is the thin HA-side equivalent: find the mesh proxy advertising our Network ID,
connect to it through HA's bluetooth APIs, and hand the connected client to an
unchanged ``GattBearer``.

All mesh logic (0x1828 parsing, SAR framing) stays in ``btmesh``; this module
only bridges HA-bluetooth to ``GattBearer`` and therefore imports HA at module
scope (it only ever runs inside Home Assistant).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant

from .btmesh.bearer import (
    PROXY_SERVICE,
    GattBearer,
    parse_proxy_service_data,
)
from .btmesh.bearer import (
    IDENTIFICATION_NETWORK_ID as _IDENTIFICATION_NETWORK_ID,
)
from .btmesh.crypto import k3

if TYPE_CHECKING:
    from bleak import BleakClient

logger = logging.getLogger(__name__)

__all__ = [
    "MeshTransportError",
    "find_proxy_address",
    "async_connect_bearer",
    "async_register_proxy_callback",
]


class MeshTransportError(Exception):
    """A mesh proxy could not be located or connected through HA-bluetooth."""


def _matches_network_id(
    info: BluetoothServiceInfoBleak, network_id: bytes
) -> bool:
    """True if ``info`` carries a 0x1828 Network-ID advert equal to ``network_id``."""
    data = info.service_data.get(PROXY_SERVICE)
    if data is None:
        return False
    parsed = parse_proxy_service_data(bytes(data))
    if parsed is None:
        return False
    id_type, parameter = parsed
    return id_type == _IDENTIFICATION_NETWORK_ID and parameter == network_id


def find_proxy_address(hass: HomeAssistant, net_key: bytes) -> str | None:
    """Address of a connectable mesh proxy advertising ``net_key``'s Network ID.

    Computes ``network_id = k3(net_key)`` and scans the current connectable
    discovery snapshot (:func:`bluetooth.async_discovered_service_info`) for a
    node advertising a 0x1828 Network-ID matching it. Returns the first match's
    address, or ``None`` if none is present. The snapshot is point-in-time; the
    coordinator retries, so a transient ``None`` is expected.
    """
    network_id = k3(net_key)
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        if _matches_network_id(info, network_id):
            logger.debug(
                "mesh proxy %s advertises Network ID %s",
                info.address, network_id.hex(),
            )
            return info.address
    return None


async def async_connect_bearer(
    hass: HomeAssistant, address: str
) -> tuple["BleakClient", GattBearer]:
    """Connect to the proxy at ``address`` and wrap it in a proxy ``GattBearer``.

    Raises :class:`MeshTransportError` if HA has no connectable ``BLEDevice`` for
    the address (the proxy dropped out of range) or if the connection fails. The
    caller is responsible for calling ``bearer.start(on_message)``.
    """
    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        raise MeshTransportError(
            f"no connectable BLE device for mesh proxy {address}"
        )
    try:
        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, f"btmesh-{address}"
        )
    except Exception as exc:  # bleak_retry_connector.BleakConnectionError etc.
        raise MeshTransportError(
            f"could not connect to mesh proxy {address}: {exc}"
        ) from exc
    bearer = GattBearer(client, provisioning=False)
    return client, bearer


def async_register_proxy_callback(
    hass: HomeAssistant,
    net_key: bytes,
    on_found: Callable[[str], None],
) -> Callable[[], None]:
    """Push discovery: call ``on_found(address)`` when a matching proxy appears.

    Registers a 0x1828-service-uuid active-scan callback and forwards the
    address of every advert whose Network ID equals ``k3(net_key)``. Returns the
    HA-provided unregister callable. Complements the snapshot scan of
    :func:`find_proxy_address` for the coordinator (Task B3).
    """
    network_id = k3(net_key)

    def _callback(
        info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        if _matches_network_id(info, network_id):
            on_found(info.address)

    return bluetooth.async_register_callback(
        hass,
        _callback,
        BluetoothCallbackMatcher(service_uuid=PROXY_SERVICE),
        BluetoothScanningMode.ACTIVE,
    )
