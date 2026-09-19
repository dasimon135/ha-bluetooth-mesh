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
from time import monotonic
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
    IDENTIFICATION_NETWORK_ID as _IDENTIFICATION_NETWORK_ID,
)
from .btmesh.bearer import (
    PROXY_SERVICE,
    GattBearer,
    parse_proxy_service_data,
)
from .btmesh.crypto import k3

if TYPE_CHECKING:
    from bleak import BleakClient

logger = logging.getLogger(__name__)

__all__ = [
    "MeshTransportError",
    "PROXY_ADVERT_MAX_AGE",
    "find_proxy_address",
    "async_connect_bearer",
    "async_register_proxy_callback",
    "discovered_proxies",
]


class MeshTransportError(Exception):
    """A mesh proxy could not be located or connected through HA-bluetooth."""


# Past this, a cached advert is treated as silence rather than a live proxy
# (ha-bluetooth-mesh#31): HA's discovered-service-info snapshot keeps an entry
# connectable for minutes after the last real advert, and a connect attempt
# against a node that has gone quiet only charges a failure to whichever proxy
# habluetooth currently scores best -- exactly the attempts that poisoned that
# scoring during the 2026-09-12 storm.
#
# The rule has one blind spot, and the caller owns it: a node stops advertising
# 0x1828 for as long as its single GATT slot is held, ours included. Right after
# a link of ours ends, the newest advert is therefore as old as that link was
# long, whatever state the node is in. ``find_proxy_address(max_age=None)`` is
# for that window.
PROXY_ADVERT_MAX_AGE = 30.0


def _advert_age(hass: HomeAssistant, info: BluetoothServiceInfoBleak, now: float) -> float:
    """Seconds since ANY scanner last heard ``info.address``.

    ``info.time`` alone is not that. habluetooth keeps one entry per address,
    owned by one scanner, and drops the adverts of every other scanner for as
    long as the owner is still scanning and nobody is clearly louder, without
    touching the entry. An owner that stops hearing the node while another
    proxy still does leaves the timestamp frozen until the entry changes hands,
    which can take minutes before the advertising interval has been learned.
    Each scanner keeps its own timestamps, so ask them, but only once the cheap
    answer says "stale": this walks every device of every scanner.
    """
    age = now - info.time
    if age <= PROXY_ADVERT_MAX_AGE:
        return age
    for scanner_device in bluetooth.async_scanner_devices_by_address(
        hass, info.address, connectable=False
    ):
        heard = scanner_device.scanner.discovered_device_timestamps.get(info.address)
        if heard is not None:
            age = min(age, now - heard)
    return age


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


def find_proxy_address(
    hass: HomeAssistant,
    net_key: bytes,
    *,
    max_age: float | None = PROXY_ADVERT_MAX_AGE,
) -> str | None:
    """Address of a connectable mesh proxy advertising ``net_key``'s Network ID.

    Computes ``network_id = k3(net_key)`` and scans HA's **full** advertisement
    snapshot (:func:`bluetooth.async_discovered_service_info` with
    ``connectable=False``) for a node advertising a 0x1828 Network-ID matching
    it, returning the first match HA can currently connect through (its
    per-advert ``connectable`` flag is set).

    Why scan the full snapshot rather than the ``connectable=True`` view: HA keeps
    the connectable-only history and the full history as separate structures that
    update independently, and with a *remote* (ESPHome) scanner the
    connectable-only view can momentarily lack a proxy that the full snapshot
    already reports as ``connectable=yes``. Scanning the full snapshot and
    selecting on the per-advert ``connectable`` flag uses the exact data path as
    :func:`discovered_proxies`, so discovery agrees with the diagnostic instead of
    contradicting it. Actual connectability is re-verified at connect time by
    :func:`async_ble_device_from_address`. The snapshot is point-in-time; the
    coordinator retries, so a transient ``None`` is expected.

    A match nobody has heard for more than ``max_age`` seconds is skipped rather
    than returned: the entry can still read ``connectable=yes`` well after the
    node actually went quiet, and connecting on that stale word only spends a
    bleak attempt nobody can win (ha-bluetooth-mesh#31). ``None`` lifts the
    rule, for a caller that knows why the advert is old (see
    :data:`PROXY_ADVERT_MAX_AGE`).
    """
    network_id = k3(net_key)
    now = monotonic()
    for info in bluetooth.async_discovered_service_info(hass, connectable=False):
        if not _matches_network_id(info, network_id):
            continue
        age = _advert_age(hass, info, now)
        if max_age is not None and age > max_age:
            logger.debug(
                "mesh proxy %s advertises Network ID %s but the last advert "
                "is %.0f s old; treating it as silent",
                info.address, network_id.hex(), age,
            )
            continue
        if getattr(info, "connectable", False):
            logger.debug(
                "mesh proxy %s advertises Network ID %s (connectable)",
                info.address, network_id.hex(),
            )
            return info.address
        logger.debug(
            "mesh proxy %s advertises Network ID %s but only via a "
            "non-connectable scanner; skipping",
            info.address, network_id.hex(),
        )
    return None


def discovered_proxies(hass: HomeAssistant) -> list[tuple[str, str]]:
    """Every 0x1828 mesh-proxy advert HA currently sees (for diagnostics).

    Scans ALL adverts (connectable and not) so the diagnostic can distinguish
    three cases: no mesh proxy in range at all (empty), a proxy seen only by a
    *passive* / non-connectable scanner (``connectable=no`` — HA can see it but
    cannot connect through it), and a foreign network (a mismatching
    ``network_id``). Returns ``(address, description)`` pairs.

    Each description carries the advert's age, because
    :func:`find_proxy_address` turns down a match on it: without the age, the
    one warning of an outage would read "no connectable proxy" next to our own
    Network ID marked ``connectable=yes``.
    """
    out: list[tuple[str, str]] = []
    now = monotonic()
    for info in bluetooth.async_discovered_service_info(hass, connectable=False):
        data = info.service_data.get(PROXY_SERVICE)
        if data is None:
            continue
        parsed = parse_proxy_service_data(bytes(data))
        if parsed is None:
            continue
        id_type, parameter = parsed
        kind = (
            f"network_id={parameter.hex()}"
            if id_type == _IDENTIFICATION_NETWORK_ID
            else "node-identity"
        )
        conn = "yes" if getattr(info, "connectable", False) else "no"
        age = _advert_age(hass, info, now)
        out.append(
            (info.address, f"{kind}, connectable={conn}, heard {age:.0f} s ago")
        )
    return out


async def async_connect_bearer(
    hass: HomeAssistant, address: str, *, max_attempts: int = 4
) -> tuple["BleakClient", GattBearer]:
    """Connect to the proxy at ``address`` and wrap it in a proxy ``GattBearer``.

    Raises :class:`MeshTransportError` if HA has no connectable ``BLEDevice`` for
    the address (the proxy dropped out of range) or if the connection fails. The
    caller is responsible for calling ``bearer.start(on_message)``.

    ``max_attempts`` is bleak-retry-connector's retry budget for this one call.
    Its default of four suits a transient BLE miss. The coordinator asks for
    one on every automatic try, and on any try while it is backing off from a
    node it has been hammering: each attempt is pressure on a node that needs
    quiet, and a failure charged to whichever proxy carried it.
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
            BleakClientWithServiceCache,
            ble_device,
            f"btmesh-{address}",
            max_attempts=max_attempts,
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
