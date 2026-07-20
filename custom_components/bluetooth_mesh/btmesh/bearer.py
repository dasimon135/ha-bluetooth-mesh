"""GATT bearer: Proxy PDU framing over a bleak-compatible GATT client.

Wraps a connected client (plain bleak, or habluetooth's ``HaBleakClientWrapper``
routed through an ESPHome Bluetooth proxy) and speaks the Mesh Proxy protocol
(spec §6.3): SAR segmentation on TX, reassembly + dispatch on RX. Every frame
in and out is hex-dumped at DEBUG level — that log is the Phase 0 interop
evidence.

Transport wiring, standalone ESPHome (ground truth: installed bleak-esphome
3.9.7 / aioesphomeapi 45.6.2 / habluetooth 6.26.5 sources):

1. ``habluetooth.BluetoothManager().async_setup()`` — installs itself as the
   central manager (``central_manager.CentralBluetoothManager``).
2. ``bleak_esphome.APIConnectionManager({"address", "noise_psk"}).start()`` —
   builds the aioesphomeapi ``APIClient`` (port 6053) + ``ReconnectLogic``,
   and on connect runs ``bleak_esphome.connect_scanner`` and registers the
   resulting ``ESPHomeScanner`` with the central manager.
3. ``habluetooth.HaBleakScannerWrapper`` / ``HaBleakClientWrapper`` then
   scan/connect through whatever proxies are registered.

The bleak surface used here is deliberately minimal: ``mtu_size``,
``start_notify``, ``stop_notify``, ``write_gatt_char(..., response=False)``,
``disconnect``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from .errors import BtMeshError
from .proxy_pdu import ProxyPDUError, Reassembler, segment

if TYPE_CHECKING:
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice

logger = logging.getLogger(__name__)

__all__ = [
    "PROV_SERVICE",
    "PROV_DATA_IN",
    "PROV_DATA_OUT",
    "PROXY_SERVICE",
    "PROXY_DATA_IN",
    "PROXY_DATA_OUT",
    "ATT_HEADER_LEN",
    "DEFAULT_MAX_FRAME",
    "IDENTIFICATION_NETWORK_ID",
    "IDENTIFICATION_NODE_IDENTITY",
    "BearerError",
    "GattBearer",
    "UnprovisionedDevice",
    "ProxyCandidate",
    "parse_unprovisioned_service_data",
    "parse_proxy_service_data",
    "unprovisioned_from_discoveries",
    "proxy_candidates_from_discoveries",
    "scan_unprovisioned",
    "find_proxy_node",
    "connect_local",
    "EsphomeTransport",
]

# Mesh Provisioning Service (PB-GATT, spec §7.1).
PROV_SERVICE = "00001827-0000-1000-8000-00805f9b34fb"
PROV_DATA_IN = "00002adb-0000-1000-8000-00805f9b34fb"  # write-without-response
PROV_DATA_OUT = "00002adc-0000-1000-8000-00805f9b34fb"  # notify
# Mesh Proxy Service (spec §7.2).
PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"
PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

# A Write Without Response payload is ATT_MTU - 3 (opcode + handle).
ATT_HEADER_LEN = 3
# Fallback frame budget when mtu_size is unavailable/absurd: min ATT MTU 23 - 3.
DEFAULT_MAX_FRAME = 20

# How long :meth:`GattBearer.start` waits for ``start_notify`` to CONFIRM before
# proceeding anyway. Some proxied backends (bleak-esphome behind an ESPHome BLE
# proxy) begin delivering Data Out notifications immediately yet never resolve
# the ``start_notify`` await, which would hang the caller forever. The
# subscription is active regardless, so we bound the wait and carry on.
START_NOTIFY_TIMEOUT = 5.0

# Mesh Proxy Service advertising identification types (spec §7.2.2.2).
IDENTIFICATION_NETWORK_ID = 0x00
IDENTIFICATION_NODE_IDENTITY = 0x01


class BearerError(BtMeshError):
    """The GATT bearer could not subscribe, write, or connect."""


class GattBearer:
    """SAR framing over one connected GATT client (prov or proxy service).

    ``provisioning=True`` selects the Mesh Provisioning Data In/Out pair
    (PB-GATT); the default selects the Mesh Proxy pair. :meth:`start`
    subscribes to Data Out and dispatches every complete message to the
    ``on_message(msg_type, payload)`` callback; :meth:`send` SAR-segments to
    the negotiated payload size and writes without response to Data In.
    """

    def __init__(self, client: Any, *, provisioning: bool = False) -> None:
        self._client = client
        if provisioning:
            self._data_in, self._data_out = PROV_DATA_IN, PROV_DATA_OUT
        else:
            self._data_in, self._data_out = PROXY_DATA_IN, PROXY_DATA_OUT
        self._reassembler = Reassembler()
        self._on_message: Callable[[int, bytes], None] | None = None

    @property
    def max_frame(self) -> int:
        """Largest proxy PDU frame the link carries: ``mtu_size - 3``.

        Falls back to :data:`DEFAULT_MAX_FRAME` when ``mtu_size`` is
        unavailable, zero, or too small to be a real negotiated ATT MTU.
        """
        try:
            mtu = int(self._client.mtu_size)
        except Exception:  # backend not connected / attribute missing
            mtu = 0
        if mtu - ATT_HEADER_LEN < 2:  # need header byte + >=1 payload byte
            return DEFAULT_MAX_FRAME
        return mtu - ATT_HEADER_LEN

    async def start(self, on_message: Callable[[int, bytes], None]) -> None:
        """Subscribe to the Data Out characteristic and begin dispatching.

        Bounded by :data:`START_NOTIFY_TIMEOUT`: some proxied backends (notably
        bleak-esphome behind an ESPHome BLE proxy) start delivering Data Out
        notifications immediately but never resolve the ``start_notify`` await,
        which would hang the caller forever. The subscription is active anyway
        (frames flow) and the proxy link is usable for writes regardless, so if
        the await does not confirm in time we log and proceed rather than block.
        A genuine subscribe *error* still raises :class:`BearerError`.
        """
        self._on_message = on_message
        task = asyncio.ensure_future(
            self._client.start_notify(self._data_out, self._handle_notify)
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), START_NOTIFY_TIMEOUT)
        except asyncio.TimeoutError:
            # Leave the still-pending subscribe running (notifications already
            # flow) and swallow any late result so it is not reported as an
            # unretrieved task exception.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            logger.warning(
                "start_notify(%s) unconfirmed after %ss; proceeding "
                "(proxy backend likely delivers notifications without "
                "resolving the await)",
                self._data_out, START_NOTIFY_TIMEOUT,
            )
            return
        except Exception as exc:
            task.cancel()
            raise BearerError(
                f"could not subscribe to {self._data_out}: {exc}"
            ) from exc
        logger.debug("subscribed to %s", self._data_out)

    async def stop(self) -> None:
        """Unsubscribe from Data Out; safe to call on a dead connection."""
        try:
            await self._client.stop_notify(self._data_out)
        except Exception as exc:
            logger.debug("stop_notify failed (ignored): %s", exc)

    async def send(self, msg_type: int, payload: bytes) -> None:
        """SAR-segment ``payload`` and write each frame without response.

        Callers must serialize send() calls: two concurrent sends would
        interleave their SAR frames and corrupt reassembly at the peer.
        (The Phase 0 script serializes through its single-task BearerPump.)
        """
        max_frame = self.max_frame
        try:
            frames = segment(msg_type, payload, max_frame)
        except ProxyPDUError as exc:
            raise BearerError(str(exc)) from exc
        logger.debug(
            "TX message type=0x%02x (%d bytes, %d frame(s), max_frame=%d): %s",
            msg_type, len(payload), len(frames), max_frame, payload.hex(),
        )
        for frame in frames:
            logger.debug("TX frame (%d bytes): %s", len(frame), frame.hex())
            try:
                await self._client.write_gatt_char(
                    self._data_in, frame, response=False
                )
            except Exception as exc:
                raise BearerError(
                    f"GATT write to {self._data_in} failed: {exc}"
                ) from exc

    # bleak notify callbacks receive (characteristic, bytearray).
    def _handle_notify(self, _characteristic: Any, data: bytearray) -> None:
        frame = bytes(data)
        logger.debug("RX frame (%d bytes): %s", len(frame), frame.hex())
        try:
            complete = self._reassembler.feed(frame)
        except ProxyPDUError as exc:
            # feed() already reset the reassembly state; keep the link alive.
            logger.warning("dropping malformed proxy frame: %s", exc)
            return
        if complete is None:
            return
        msg_type, payload = complete
        logger.debug(
            "RX message type=0x%02x (%d bytes): %s",
            msg_type, len(payload), payload.hex(),
        )
        if self._on_message is None:
            return
        try:
            self._on_message(msg_type, payload)
        except Exception:  # never let a handler kill the notification pump
            logger.exception("bearer on_message callback raised")


# --------------------------------------------------------- beacon parsing


class UnprovisionedDevice(NamedTuple):
    """One unprovisioned node seen advertising the 0x1827 service."""

    device: Any  # bleak BLEDevice
    uuid: bytes  # 16-byte Device UUID
    oob_info: int  # 16-bit OOB Information field


class ProxyCandidate(NamedTuple):
    """One provisioned node seen advertising the 0x1828 proxy service."""

    device: Any  # bleak BLEDevice
    identification_type: int  # IDENTIFICATION_NETWORK_ID / _NODE_IDENTITY
    parameter: bytes  # Network ID (8) or Hash+Random (16)


def parse_unprovisioned_service_data(data: bytes) -> tuple[bytes, int] | None:
    """Device UUID(16) + OOB Info(2, big-endian) from 0x1827 service data.

    Returns ``None`` for truncated data (spec §7.1.2.2.1 requires 18 octets).
    """
    if len(data) < 18:
        return None
    return bytes(data[:16]), int.from_bytes(data[16:18], "big")


def parse_proxy_service_data(data: bytes) -> tuple[int, bytes] | None:
    """(identification type, parameter) from 0x1828 service data.

    Type 0x00 carries the 8-byte Network ID (= k3(NetKey)); type 0x01 carries
    Node Identity Hash(8) + Random(8). Unknown types and truncated data
    return ``None`` (spec §7.2.2.2).
    """
    if not data:
        return None
    id_type = data[0]
    if id_type == IDENTIFICATION_NETWORK_ID and len(data) >= 9:
        return id_type, bytes(data[1:9])
    if id_type == IDENTIFICATION_NODE_IDENTITY and len(data) >= 17:
        return id_type, bytes(data[1:17])
    return None


def unprovisioned_from_discoveries(
    discoveries: dict[str, tuple[Any, Any]],
) -> list[UnprovisionedDevice]:
    """Extract 0x1827 beacons from ``{address: (device, advertisement_data)}``."""
    found: list[UnprovisionedDevice] = []
    for device, adv in discoveries.values():
        data = adv.service_data.get(PROV_SERVICE)
        if data is None:
            continue
        parsed = parse_unprovisioned_service_data(bytes(data))
        if parsed is None:
            logger.debug(
                "truncated 0x1827 service data from %s: %s",
                device.address, bytes(data).hex(),
            )
            continue
        found.append(UnprovisionedDevice(device, *parsed))
    return found


def proxy_candidates_from_discoveries(
    discoveries: dict[str, tuple[Any, Any]],
) -> list[ProxyCandidate]:
    """Extract 0x1828 proxy adverts from ``{address: (device, adv_data)}``."""
    found: list[ProxyCandidate] = []
    for device, adv in discoveries.values():
        data = adv.service_data.get(PROXY_SERVICE)
        if data is None:
            continue
        parsed = parse_proxy_service_data(bytes(data))
        if parsed is None:
            logger.debug(
                "unparseable 0x1828 service data from %s: %s",
                device.address, bytes(data).hex(),
            )
            continue
        found.append(ProxyCandidate(device, *parsed))
    return found


# --------------------------------------------------------- scan helpers


async def scan_unprovisioned(
    scanner: Any,
    timeout: float = 10.0,
    *,
    stop_on: bytes | None = None,
    stop_on_any: bool = False,
    poll_interval: float = 0.5,
) -> list[UnprovisionedDevice]:
    """Scan for unprovisioned-device beacons (0x1827 service data).

    ``scanner`` is any BleakScanner-compatible object (plain bleak or
    habluetooth's ``HaBleakScannerWrapper``): ``start()``, ``stop()`` and the
    ``discovered_devices_and_advertisement_data`` mapping are used.

    ``stop_on`` (a 16-byte Device UUID) or ``stop_on_any`` end the scan as
    soon as a matching beacon appears: Telink mesh nodes only accept
    connections in a short window after power-up, so every second between
    beacon and connect attempt counts.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    await scanner.start()
    try:
        while True:
            found = unprovisioned_from_discoveries(
                scanner.discovered_devices_and_advertisement_data
            )
            if stop_on is not None and any(d.uuid == stop_on for d in found):
                return found
            if stop_on_any and found:
                return found
            if loop.time() >= deadline:
                return found
            await asyncio.sleep(poll_interval)
    finally:
        await scanner.stop()


async def find_proxy_node(
    scanner: Any,
    network_id: bytes,
    timeout: float = 15.0,
    poll_interval: float = 1.0,
) -> tuple[ProxyCandidate | None, list[ProxyCandidate]]:
    """Scan for provisioned nodes advertising the Mesh Proxy service (0x1828).

    Returns ``(match, candidates)``: ``match`` is the first advert whose
    Network ID equals ``network_id`` (returned as soon as it appears), else
    ``None`` after ``timeout``. ``candidates`` lists every 0x1828 advert seen,
    including Node Identity ones — those cannot be verified without the
    identity crypto Phase 0 lacks, so they are logged and left to the caller.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    candidates: dict[str, ProxyCandidate] = {}
    await scanner.start()
    try:
        while True:
            for cand in proxy_candidates_from_discoveries(
                scanner.discovered_devices_and_advertisement_data
            ):
                if cand.device.address not in candidates:
                    if cand.identification_type == IDENTIFICATION_NODE_IDENTITY:
                        logger.info(
                            "0x1828 Node Identity advert from %s (hash+random "
                            "%s) — cannot be verified in Phase 0",
                            cand.device.address, cand.parameter.hex(),
                        )
                    else:
                        logger.info(
                            "0x1828 Network ID advert from %s: %s",
                            cand.device.address, cand.parameter.hex(),
                        )
                candidates[cand.device.address] = cand
                if (
                    cand.identification_type == IDENTIFICATION_NETWORK_ID
                    and cand.parameter == network_id
                ):
                    return cand, list(candidates.values())
            if loop.time() >= deadline:
                return None, list(candidates.values())
            await asyncio.sleep(poll_interval)
    finally:
        await scanner.stop()


# --------------------------------------------------------- transports


async def connect_local(
    address_or_device: "str | BLEDevice", timeout: float = 20.0
) -> "BleakClient":
    """Connect with the PC's own adapter through plain bleak.

    A ``BLEDevice`` (from scanning) goes through bleak-retry-connector's
    ``establish_connection`` — mesh nodes routinely drop the first attempt.
    A bare address string falls back to a single direct attempt.
    """
    from bleak import BleakClient

    if not isinstance(address_or_device, str):
        from bleak_retry_connector import establish_connection

        try:
            return await establish_connection(
                BleakClient, address_or_device, address_or_device.address
            )
        except Exception as exc:
            raise BearerError(
                f"local BLE connect to {address_or_device} failed: {exc}"
            ) from exc

    client = BleakClient(address_or_device, timeout=timeout)
    try:
        await client.connect()
    except Exception as exc:
        raise BearerError(
            f"local BLE connect to {address_or_device} failed: {exc}"
        ) from exc
    return client


class EsphomeTransport:
    """BLE via an ESPHome Bluetooth proxy (bleak-esphome standalone wiring).

    Usage::

        transport = EsphomeTransport("proxy.local", noise_psk)
        await transport.start()          # API connect + scanner registration
        scanner = transport.scanner()    # BleakScanner-compatible
        client = await transport.connect(ble_device)   # BleakClient-compatible
        ...
        await transport.stop()

    ``start()`` sets up habluetooth's central ``BluetoothManager`` (once per
    process) and brings up ``bleak_esphome.APIConnectionManager``, which
    registers the proxy's remote scanner. The imports stay inside the methods
    so unit tests never touch bleak/habluetooth.
    """

    def __init__(self, host: str, noise_psk: str | None) -> None:
        self._host = host
        self._noise_psk = noise_psk or None
        self._conn: Any = None
        self._manager: Any = None

    async def start(self) -> None:
        import habluetooth
        from bleak_esphome import APIConnectionManager

        try:
            habluetooth.get_manager()
        except RuntimeError:
            # async_setup() installs the instance as the central manager.
            self._manager = habluetooth.BluetoothManager()
            await self._manager.async_setup()
        self._conn = APIConnectionManager(
            {"address": self._host, "noise_psk": self._noise_psk}
        )
        try:
            await self._conn.start()
        except Exception as exc:
            self._conn = None
            raise BearerError(
                f"could not connect to ESPHome proxy {self._host}: {exc}"
            ) from exc
        logger.info("ESPHome proxy %s connected, scanner registered", self._host)

    def scanner(self) -> Any:
        """A BleakScanner-compatible scanner fed by the registered proxies."""
        from habluetooth import HaBleakScannerWrapper

        return HaBleakScannerWrapper()

    async def connect(
        self, address_or_device: "str | BLEDevice", timeout: float = 30.0
    ) -> "BleakClient":
        """Connect to ``address_or_device`` through the proxy.

        A ``BLEDevice`` goes through bleak-retry-connector's
        ``establish_connection`` — the mechanism Home Assistant itself uses
        for ESPHome proxies (transient-failure retries, slot management).
        A bare address string falls back to a single direct attempt.
        """
        from habluetooth import HaBleakClientWrapper

        if not isinstance(address_or_device, str):
            from bleak_retry_connector import establish_connection

            try:
                return await establish_connection(
                    HaBleakClientWrapper,
                    address_or_device,
                    address_or_device.address,
                    max_attempts=4,
                )
            except Exception as exc:
                raise BearerError(
                    f"BLE connect to {address_or_device} via ESPHome proxy "
                    f"{self._host} failed: {exc}"
                ) from exc

        client = HaBleakClientWrapper(address_or_device, timeout=timeout)
        try:
            await client.connect()
        except Exception as exc:
            raise BearerError(
                f"BLE connect to {address_or_device} via ESPHome proxy "
                f"{self._host} failed: {exc}"
            ) from exc
        return client

    async def stop(self) -> None:
        if self._conn is not None:
            await self._conn.stop()
            self._conn = None
        if self._manager is not None:
            self._manager.async_stop()
            self._manager = None
