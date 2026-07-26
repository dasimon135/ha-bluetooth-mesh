"""GATT bearer unit tests: pure logic only (no adapter, no network).

Covers 0x1827/0x1828 service-data parsing from synthetic AdvertisementData-
shaped inputs, SAR send chunking against a fake client, reassembly dispatch
via fake notifications, and BearerError propagation. bleak is never imported.
"""

import asyncio
from types import SimpleNamespace

import pytest

from btmesh.bearer import (
    DEFAULT_MAX_FRAME,
    IDENTIFICATION_NETWORK_ID,
    IDENTIFICATION_NODE_IDENTITY,
    PROV_DATA_IN,
    PROV_DATA_OUT,
    PROV_SERVICE,
    PROXY_DATA_IN,
    PROXY_DATA_OUT,
    PROXY_SERVICE,
    BearerError,
    GattBearer,
    find_proxy_node,
    parse_proxy_service_data,
    parse_unprovisioned_service_data,
    proxy_candidates_from_discoveries,
    scan_unprovisioned,
    unprovisioned_from_discoveries,
)
from btmesh.proxy_pdu import Reassembler

UUID = bytes(range(16))
NETWORK_ID = bytes.fromhex("3ecaff672f673370")


def adv(service_data):
    """Minimal AdvertisementData-shaped object."""
    return SimpleNamespace(service_data=service_data)


def device(address="AA:BB:CC:DD:EE:FF"):
    return SimpleNamespace(address=address)


# ------------------------------------------------------- service-data parsing


def test_parse_unprovisioned_service_data():
    data = UUID + (0x0004).to_bytes(2, "big")
    assert parse_unprovisioned_service_data(data) == (UUID, 0x0004)


def test_parse_unprovisioned_service_data_extra_bytes_ignored():
    data = UUID + b"\x00\x01" + b"\xff\xff"  # trailing URI hash etc.
    assert parse_unprovisioned_service_data(data) == (UUID, 0x0001)


def test_parse_unprovisioned_service_data_truncated():
    assert parse_unprovisioned_service_data(UUID + b"\x00") is None
    assert parse_unprovisioned_service_data(b"") is None


def test_parse_proxy_service_data_network_id():
    assert parse_proxy_service_data(b"\x00" + NETWORK_ID) == (
        IDENTIFICATION_NETWORK_ID,
        NETWORK_ID,
    )


def test_parse_proxy_service_data_node_identity():
    hash_random = bytes(range(16))
    assert parse_proxy_service_data(b"\x01" + hash_random) == (
        IDENTIFICATION_NODE_IDENTITY,
        hash_random,
    )


def test_parse_proxy_service_data_invalid():
    assert parse_proxy_service_data(b"") is None
    assert parse_proxy_service_data(b"\x00" + NETWORK_ID[:-1]) is None  # short
    assert parse_proxy_service_data(b"\x01" + bytes(15)) is None  # short
    assert parse_proxy_service_data(b"\x02" + bytes(16)) is None  # unknown type


def test_unprovisioned_from_discoveries_filters_and_parses():
    beacon = UUID + b"\x00\x00"
    discoveries = {
        "aa": (device("aa"), adv({PROV_SERVICE: beacon})),
        "bb": (device("bb"), adv({"0000180f-0000-1000-8000-00805f9b34fb": b"x"})),
        "cc": (device("cc"), adv({PROV_SERVICE: b"\x01\x02"})),  # truncated
    }
    found = unprovisioned_from_discoveries(discoveries)
    assert len(found) == 1
    assert found[0].device.address == "aa"
    assert found[0].uuid == UUID
    assert found[0].oob_info == 0


def test_proxy_candidates_from_discoveries():
    discoveries = {
        "aa": (device("aa"), adv({PROXY_SERVICE: b"\x00" + NETWORK_ID})),
        "bb": (device("bb"), adv({PROXY_SERVICE: b"\x01" + bytes(16)})),
        "cc": (device("cc"), adv({})),
    }
    found = proxy_candidates_from_discoveries(discoveries)
    assert {c.device.address for c in found} == {"aa", "bb"}


# ----------------------------------------------------------------- fake bleak


class FakeClient:
    """Captures writes; exposes the notify callback for injection."""

    def __init__(self, mtu_size=69):
        self.mtu_size = mtu_size
        self.writes = []
        self.notify_callbacks = {}
        self.write_error = None

    async def start_notify(self, char, callback):
        self.notify_callbacks[char] = callback

    async def stop_notify(self, char):
        self.notify_callbacks.pop(char)

    async def write_gatt_char(self, char, data, response=None):
        assert response is False  # bearer must write without response
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((char, bytes(data)))


class BrokenMtuClient(FakeClient):
    def __init__(self):  # no super(): the read-only property forbids assignment
        self.writes = []
        self.notify_callbacks = {}
        self.write_error = None

    @property
    def mtu_size(self):
        raise RuntimeError("not connected")


# ------------------------------------------------------------------- TX / SAR


async def test_send_chunks_to_mtu_minus_3():
    client = FakeClient(mtu_size=69)
    bearer = GattBearer(client, provisioning=True)
    payload = bytes(range(256)) * 2  # 512 bytes, forces several segments
    await bearer.send(0x03, payload)

    assert all(char == PROV_DATA_IN for char, _ in client.writes)
    frames = [frame for _, frame in client.writes]
    assert all(len(frame) <= 69 - 3 for frame in frames)
    # All frames except the last must be full (contiguous SAR stream).
    assert all(len(frame) == 66 for frame in frames[:-1])
    # Round-trip through the reference reassembler restores the payload.
    reassembler = Reassembler()
    results = [reassembler.feed(frame) for frame in frames]
    assert results[:-1] == [None] * (len(frames) - 1)
    assert results[-1] == (0x03, payload)


async def test_send_single_frame_when_payload_fits():
    client = FakeClient(mtu_size=69)
    bearer = GattBearer(client)  # proxy variant
    await bearer.send(0x00, b"\x01\x02\x03")
    assert client.writes == [(PROXY_DATA_IN, b"\x00\x01\x02\x03")]


async def test_send_falls_back_to_default_frame_when_mtu_zero():
    client = FakeClient(mtu_size=0)
    bearer = GattBearer(client)
    await bearer.send(0x00, bytes(50))
    assert all(len(frame) <= DEFAULT_MAX_FRAME for _, frame in client.writes)
    assert len(client.writes) == 3  # 50 bytes / 19-byte chunks

def test_max_frame_fallback_when_mtu_property_raises():
    bearer = GattBearer(BrokenMtuClient())
    assert bearer.max_frame == DEFAULT_MAX_FRAME


async def test_send_write_failure_raises_bearer_error():
    client = FakeClient()
    client.write_error = OSError("proxy link dropped")
    bearer = GattBearer(client)
    with pytest.raises(BearerError, match="proxy link dropped"):
        await bearer.send(0x00, b"\x00")


async def test_start_failure_raises_bearer_error():
    class NoNotifyClient(FakeClient):
        async def start_notify(self, char, callback):
            raise OSError("characteristic missing")

    bearer = GattBearer(NoNotifyClient())
    with pytest.raises(BearerError, match="characteristic missing"):
        await bearer.start(lambda t, p: None)


# ------------------------------------------------------------- RX / dispatch


async def test_rx_reassembly_dispatch():
    client = FakeClient()
    bearer = GattBearer(client, provisioning=True)
    received = []
    await bearer.start(lambda t, p: received.append((t, p)))
    notify = client.notify_callbacks[PROV_DATA_OUT]

    notify(None, bytearray(b"\x43\xaa\xbb"))  # SAR first, type 0x03
    assert received == []  # incomplete
    notify(None, bytearray(b"\x83\xcc"))  # SAR continuation
    notify(None, bytearray(b"\xc3\xdd"))  # SAR last
    assert received == [(0x03, b"\xaa\xbb\xcc\xdd")]

    notify(None, bytearray(b"\x00\x11"))  # complete, type 0x00
    assert received[-1] == (0x00, b"\x11")


async def test_rx_uses_proxy_data_out_for_proxy_variant():
    client = FakeClient()
    bearer = GattBearer(client)
    await bearer.start(lambda t, p: None)
    assert set(client.notify_callbacks) == {PROXY_DATA_OUT}
    await bearer.stop()
    assert client.notify_callbacks == {}


async def test_rx_malformed_frame_dropped_without_raising():
    client = FakeClient()
    bearer = GattBearer(client)
    received = []
    await bearer.start(lambda t, p: received.append((t, p)))
    notify = client.notify_callbacks[PROXY_DATA_OUT]

    notify(None, bytearray(b"\x80\xaa"))  # continuation without first
    assert received == []
    notify(None, bytearray(b"\x00\xbb"))  # link still alive afterwards
    assert received == [(0x00, b"\xbb")]


async def test_rx_callback_exception_does_not_break_pump():
    client = FakeClient()
    bearer = GattBearer(client)

    def explode(t, p):
        raise ValueError("handler bug")

    await bearer.start(explode)
    notify = client.notify_callbacks[PROXY_DATA_OUT]
    notify(None, bytearray(b"\x00\x01"))  # must not raise


# ------------------------------------------------------------- scan helpers


class FakeScanner:
    """BleakScanner-shaped: start/stop plus a discoveries mapping."""

    def __init__(self, discoveries=None):
        self.discoveries = discoveries or {}
        self.running = False

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    @property
    def discovered_devices_and_advertisement_data(self):
        return self.discoveries


async def test_scan_unprovisioned_returns_beacons_and_stops_scanner():
    scanner = FakeScanner(
        {"aa": (device("aa"), adv({PROV_SERVICE: UUID + b"\x00\x08"}))}
    )
    found = await scan_unprovisioned(scanner, timeout=0.01)
    assert [(b.uuid, b.oob_info) for b in found] == [(UUID, 0x0008)]
    assert scanner.running is False


async def test_find_proxy_node_matches_network_id():
    scanner = FakeScanner(
        {
            "aa": (device("aa"), adv({PROXY_SERVICE: b"\x00" + NETWORK_ID})),
            "bb": (device("bb"), adv({PROXY_SERVICE: b"\x01" + bytes(16)})),
        }
    )
    match, candidates = await find_proxy_node(
        scanner, NETWORK_ID, timeout=0.05, poll_interval=0.01
    )
    assert match is not None
    assert match.device.address == "aa"
    assert match.parameter == NETWORK_ID
    assert scanner.running is False


async def test_find_proxy_node_times_out_but_reports_candidates():
    scanner = FakeScanner(
        {"bb": (device("bb"), adv({PROXY_SERVICE: b"\x01" + bytes(16)}))}
    )
    match, candidates = await find_proxy_node(
        scanner, NETWORK_ID, timeout=0.03, poll_interval=0.01
    )
    assert match is None
    assert len(candidates) == 1
    assert candidates[0].identification_type == IDENTIFICATION_NODE_IDENTITY


async def test_find_proxy_node_ignores_foreign_network_id():
    scanner = FakeScanner(
        {"aa": (device("aa"), adv({PROXY_SERVICE: b"\x00" + bytes(8)}))}
    )
    match, candidates = await find_proxy_node(
        scanner, NETWORK_ID, timeout=0.03, poll_interval=0.01
    )
    assert match is None
    assert len(candidates) == 1  # foreign subnet still reported as candidate


# ------------------------------------------------ start_notify housekeeping


class NeverConfirmingClient:
    """A proxied backend that delivers notifications but never resolves the
    ``start_notify`` await — the bleak-esphome behaviour START_NOTIFY_TIMEOUT
    exists for."""

    def __init__(self) -> None:
        self.mtu_size = 69
        self.subscribe_cancelled = False

    async def start_notify(self, char, handler):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.subscribe_cancelled = True
            raise

    async def stop_notify(self, char):
        pass


async def test_stop_cancels_an_unconfirmed_subscribe():
    """The pending subscribe must not outlive the bearer.

    start() deliberately proceeds without waiting for confirmation, which
    leaves a task running against a client the caller is about to disconnect.
    Nothing cancelled it, so it lingered until the event loop went away.
    """
    from btmesh.bearer import GattBearer

    client = NeverConfirmingClient()
    bearer = GattBearer(client, provisioning=False)
    await bearer.start(lambda msg_type, payload: None)

    await bearer.stop()

    assert client.subscribe_cancelled is True
