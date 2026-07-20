"""MeshController facade tests: full encode -> decode -> status round trip.

The controller drives a real :class:`btmesh.node.MeshNode` through a real
:class:`btmesh.pump.BearerPump`; a ``FakeBearer`` stands in for the GATT proxy.
Its ``send`` decodes each outgoing network PDU with a SECOND MeshNode (the
"device") that shares the network's keys and plays the addressed unicast. The
device records the access message it received and crafts the model's Status
reply, which the fake feeds back to the controller's RX path. This proves the
whole codec chain, not just the payload the controller emits.

The network keys/unicast come from the sanitized ``sample.connect.json``
fixture via :meth:`Network.from_connect_file`.
"""

import os

from btmesh.access import (
    OP_GENERIC_ONOFF_GET,
    OP_GENERIC_ONOFF_SET,
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_CTL_SET,
    OP_LIGHT_CTL_STATUS,
    OP_LIGHT_CTL_TEMPERATURE_SET,
    OP_LIGHT_CTL_TEMPERATURE_STATUS,
    OP_LIGHT_LIGHTNESS_GET,
    OP_LIGHT_LIGHTNESS_SET,
    OP_LIGHT_LIGHTNESS_STATUS,
    encode_opcode,
)
from btmesh.controller import MeshController
from btmesh.network_model import Network
from btmesh.node import MeshNode, ReceivedMessage
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "sample.connect.json"
)
UNICAST = 0x000C  # the fixture node's primary element


class FakeBearer:
    """In-memory GATT proxy: forwards controller TX to a device MeshNode."""

    def __init__(self) -> None:
        self.on_message = None
        self.sent: list[tuple[int, bytes]] = []
        self.device: MeshNode | None = None
        self.started = False
        self.stopped = False

    async def start(self, on_message) -> None:
        self.on_message = on_message
        self.started = True

    async def send(self, msg_type: int, payload: bytes) -> None:
        self.sent.append((msg_type, payload))
        if self.device is not None:  # None => black hole (timeout tests)
            self.device.handle_network_pdu(payload)

    async def stop(self) -> None:
        self.stopped = True

    def feed(self, pdu: bytes) -> None:
        """Device -> controller: deliver a reply PDU to the controller's RX path."""
        self.on_message(MSG_TYPE_NETWORK_PDU, pdu)


def make_setup(tid: int = 0):
    """Build a started controller wired to a device node through a FakeBearer.

    Returns ``(controller, bearer, captured)`` where ``captured`` is the list of
    access :class:`ReceivedMessage`s the device saw (newest last).
    """
    network = Network.from_connect_file(FIXTURE)
    bearer = FakeBearer()
    controller = MeshController(network, bearer, tid=tid)

    captured: list[ReceivedMessage] = []

    def device_send(pdu: bytes) -> None:
        bearer.feed(pdu)

    device = MeshNode(
        netkey=network.net_key,
        appkey=network.app_key,
        iv_index=network.iv_index,
        src_addr=UNICAST,
        send_network_pdu=device_send,
        seq=0x2000,
    )

    def responder(msg: ReceivedMessage) -> None:
        captured.append(msg)
        if msg.opcode in (OP_GENERIC_ONOFF_SET, OP_GENERIC_ONOFF_GET):
            onoff = msg.params[0] if msg.opcode == OP_GENERIC_ONOFF_SET else 1
            device.send_access(
                msg.src, encode_opcode(OP_GENERIC_ONOFF_STATUS) + bytes([onoff])
            )
        elif msg.opcode == OP_LIGHT_LIGHTNESS_SET:
            lightness = msg.params[0:2]
            device.send_access(
                msg.src, encode_opcode(OP_LIGHT_LIGHTNESS_STATUS) + lightness
            )
        elif msg.opcode == OP_LIGHT_LIGHTNESS_GET:
            device.send_access(  # report a fixed present lightness of 0x4000
                msg.src,
                encode_opcode(OP_LIGHT_LIGHTNESS_STATUS)
                + (0x4000).to_bytes(2, "little"),
            )
        elif msg.opcode == OP_LIGHT_CTL_TEMPERATURE_SET:
            temperature = msg.params[0:2]
            device.send_access(
                msg.src,
                encode_opcode(OP_LIGHT_CTL_TEMPERATURE_STATUS)
                + temperature
                + (0).to_bytes(2, "little", signed=True),  # present delta UV
            )
        elif msg.opcode == OP_LIGHT_CTL_SET:
            # Light CTL Set params: lightness(2) + temperature(2) + delta_uv(2) + tid.
            lightness = msg.params[0:2]
            temperature = msg.params[2:4]
            device.send_access(
                msg.src,
                encode_opcode(OP_LIGHT_CTL_STATUS) + lightness + temperature,
            )

    device.on_message = responder
    bearer.device = device
    return controller, bearer, captured


# ------------------------------------------------------------------- lifecycle


async def test_start_subscribes_and_stop_tears_down():
    controller, bearer, _ = make_setup()
    await controller.start()
    assert bearer.started
    assert bearer.on_message is not None
    await controller.stop()
    assert bearer.stopped


# --------------------------------------------------------------------- onoff


async def test_set_onoff_emits_set_and_returns_present():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.set_onoff(UNICAST, True)
    finally:
        await controller.stop()
    assert result is True
    assert captured[-1].opcode == OP_GENERIC_ONOFF_SET
    assert captured[-1].params[0] == 1  # onoff = ON


async def test_set_onoff_off_round_trip():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.set_onoff(UNICAST, False)
    finally:
        await controller.stop()
    assert result is False
    assert captured[-1].params[0] == 0


async def test_get_onoff_emits_get_and_returns_present():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.get_onoff(UNICAST)
    finally:
        await controller.stop()
    assert result is True
    assert captured[-1].opcode == OP_GENERIC_ONOFF_GET
    assert captured[-1].params == b""  # Get carries no parameters


# ----------------------------------------------------------------- lightness


async def test_get_lightness_emits_get_and_returns_present():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.get_lightness(UNICAST)
    finally:
        await controller.stop()
    assert result == 0x4000
    assert captured[-1].opcode == OP_LIGHT_LIGHTNESS_GET
    assert captured[-1].params == b""  # Get carries no parameters


async def test_set_lightness_full_emits_max():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.set_lightness(UNICAST, 1.0)
    finally:
        await controller.stop()
    assert result == 0xFFFF
    assert captured[-1].opcode == OP_LIGHT_LIGHTNESS_SET
    assert int.from_bytes(captured[-1].params[0:2], "little") == 0xFFFF


async def test_set_lightness_half_emits_mid_scale():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.set_lightness(UNICAST, 0.5)
    finally:
        await controller.stop()
    assert int.from_bytes(captured[-1].params[0:2], "little") == 0x8000
    assert result == 0x8000


async def test_set_lightness_clamps_out_of_range():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        assert await controller.set_lightness(UNICAST, -5.0) == 0x0000
        assert await controller.set_lightness(UNICAST, 9.0) == 0xFFFF
    finally:
        await controller.stop()


# ----------------------------------------------------------- ctl temperature


async def test_set_ctl_emits_lightness_and_kelvin():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.set_ctl(UNICAST, 0.5, 4000)
    finally:
        await controller.stop()
    assert result == 4000
    assert captured[-1].opcode == OP_LIGHT_CTL_SET
    # Light CTL Set: lightness(2 LE) then temperature(2 LE).
    assert int.from_bytes(captured[-1].params[0:2], "little") == 0x8000
    assert int.from_bytes(captured[-1].params[2:4], "little") == 4000


async def test_set_ctl_temperature_emits_kelvin():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        result = await controller.set_ctl_temperature(UNICAST, 4000)
    finally:
        await controller.stop()
    assert result == 4000
    assert captured[-1].opcode == OP_LIGHT_CTL_TEMPERATURE_SET
    assert int.from_bytes(captured[-1].params[0:2], "little") == 4000


async def test_set_ctl_temperature_clamps_to_valid_range():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        assert await controller.set_ctl_temperature(UNICAST, 100) == 800
        assert await controller.set_ctl_temperature(UNICAST, 99999) == 20000
    finally:
        await controller.stop()


# ------------------------------------------------------------------- timeout


async def test_set_onoff_returns_none_on_timeout():
    controller, bearer, _ = make_setup()
    bearer.device = None  # black hole: no reply ever comes back
    await controller.start()
    try:
        result = await controller.set_onoff(
            UNICAST, True, timeout=0.01, retries=0
        )
    finally:
        await controller.stop()
    assert result is None


async def test_get_onoff_returns_none_on_timeout():
    controller, bearer, _ = make_setup()
    bearer.device = None
    await controller.start()
    try:
        result = await controller.get_onoff(UNICAST, timeout=0.01)
    finally:
        await controller.stop()
    assert result is None


# --------------------------------------------------------------- tid and seq


async def test_tid_auto_increments_across_commands():
    controller, _, captured = make_setup()
    await controller.start()
    try:
        await controller.set_onoff(UNICAST, True)
        await controller.set_onoff(UNICAST, False)
    finally:
        await controller.stop()
    tid_first = captured[0].params[1]
    tid_second = captured[1].params[1]
    assert tid_second == (tid_first + 1) & 0xFF


async def test_seq_increments_and_is_exposed():
    controller, _, _ = make_setup()
    await controller.start()
    try:
        before = controller.seq
        await controller.set_onoff(UNICAST, True)
        after = controller.seq
    finally:
        await controller.stop()
    assert after > before


def test_seq_param_seeds_node_cursor():
    """A ``seq`` seed is handed to the underlying node (replay safety)."""
    network = Network.from_connect_file(FIXTURE)
    controller = MeshController(network, FakeBearer(), seq=0x4321)
    assert controller.seq == 0x4321


async def test_tid_param_seeds_cursor():
    """TID seeds from the param so on-demand controllers don't reuse TID 0."""
    c = MeshController(Network.from_connect_file(FIXTURE), FakeBearer(), tid=7)
    assert c.tid == 7  # seeded


async def test_onoff_uses_seeded_tid():
    controller, _, captured = make_setup(tid=42)
    await controller.start()
    try:
        await controller.set_onoff(UNICAST, True)
    finally:
        await controller.stop()
    # Generic OnOff Set params = [onoff, tid]; tid must be the seeded 42.
    assert captured[-1].params[1] == 42
    assert controller.tid == 43  # advanced
