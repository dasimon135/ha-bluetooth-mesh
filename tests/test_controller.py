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

import asyncio
import os

from btmesh import node as node_module
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
from btmesh.beacon import build_secure_network_beacon
from btmesh.controller import MeshController
from btmesh.network_model import Network
from btmesh.node import MeshNode, ReceivedMessage
from btmesh.proxy_config import (
    FILTER_ACCEPT_LIST,
    OP_ADD_ADDRESSES,
    OP_FILTER_STATUS,
    OP_SET_FILTER_TYPE,
    FilterStatus,
)
from btmesh.proxy_pdu import (
    MSG_TYPE_MESH_BEACON,
    MSG_TYPE_NETWORK_PDU,
    MSG_TYPE_PROXY_CONFIG,
)

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
        # Proxy-configuration messages seen (decoded), and whether this fake
        # proxy answers them with a Filter Status like a real one would.
        self.proxy_config: list[bytes] = []
        self.answer_proxy_config = True
        self._filter_list_size = 0

    async def start(self, on_message) -> None:
        self.on_message = on_message
        self.started = True

    async def send(self, msg_type: int, payload: bytes) -> None:
        self.sent.append((msg_type, payload))
        if msg_type == MSG_TYPE_PROXY_CONFIG:
            self._handle_proxy_config(payload)
        elif self.device is not None:  # None => black hole (timeout tests)
            self.device.handle_network_pdu(payload)

    def _handle_proxy_config(self, payload: bytes) -> None:
        """Play the proxy server: record the message, answer a Filter Status."""
        if self.device is None:
            return
        message = self.device.parse_proxy_config_pdu(payload)
        if message is None:
            return
        self.proxy_config.append(message)
        if message[0] == OP_SET_FILTER_TYPE:
            self._filter_list_size = 0  # setting the type clears the list
        elif message[0] == OP_ADD_ADDRESSES:
            self._filter_list_size += (len(message) - 1) // 2
        if not self.answer_proxy_config:
            return
        status = bytes([OP_FILTER_STATUS, FILTER_ACCEPT_LIST]) + (
            self._filter_list_size
        ).to_bytes(2, "big")
        self.feed(
            self.device.build_proxy_config_pdu(status),
            msg_type=MSG_TYPE_PROXY_CONFIG,
        )

    async def stop(self) -> None:
        self.stopped = True

    def feed(self, pdu: bytes, msg_type: int = MSG_TYPE_NETWORK_PDU) -> None:
        """Device -> controller: deliver a reply PDU to the controller's RX path."""
        self.on_message(msg_type, pdu)


def make_setup(tid: int = 0, fade: bool = False):
    """Build a started controller wired to a device node through a FakeBearer.

    Returns ``(controller, bearer, captured)`` where ``captured`` is the list of
    access :class:`ReceivedMessage`s the device saw (newest last).

    ``fade=True`` makes the device answer like a lamp MID-TRANSITION: the status
    reports a present value still on its way to the commanded target, plus the
    target and a remaining time. Real lamps do this on every non-instant
    transition, and it is the case that makes "present" the wrong thing to
    trust.
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
            if fade:
                # Still ramping: present is halfway, target is what was asked.
                present = int.from_bytes(lightness, "little") // 2
                device.send_access(
                    msg.src,
                    encode_opcode(OP_LIGHT_LIGHTNESS_STATUS)
                    + present.to_bytes(2, "little")
                    + lightness
                    + bytes([0x0A]),  # remaining time
                )
            else:
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
            delta_uv = (0).to_bytes(2, "little", signed=True)
            if fade:
                present = int.from_bytes(temperature, "little") - 500
                device.send_access(
                    msg.src,
                    encode_opcode(OP_LIGHT_CTL_TEMPERATURE_STATUS)
                    + present.to_bytes(2, "little")
                    + delta_uv
                    + temperature
                    + delta_uv
                    + bytes([0x0A]),  # remaining time
                )
            else:
                device.send_access(
                    msg.src,
                    encode_opcode(OP_LIGHT_CTL_TEMPERATURE_STATUS)
                    + temperature
                    + delta_uv,  # present delta UV
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
    await controller.start()
    bearer.device = None  # black hole: no reply ever comes back
    try:
        result = await controller.set_onoff(
            UNICAST, True, timeout=0.01, retries=0
        )
    finally:
        await controller.stop()
    assert result is None


async def test_get_onoff_returns_none_on_timeout():
    controller, bearer, _ = make_setup()
    await controller.start()
    bearer.device = None
    try:
        result = await controller.get_onoff(UNICAST, timeout=0.01)
    finally:
        await controller.stop()
    assert result is None


# ------------------------------------------------------ mid-transition sets


async def test_set_lightness_returns_the_target_not_the_mid_fade_value():
    """A Set must report where the lamp is GOING, not where it currently is.

    Until the proxy filter was configured no Status ever came back, so this
    read-back path was dead code. Now that replies arrive, returning the
    present value mid-fade would drag the Home Assistant brightness slider to
    a transient half-way value — the display drift that was fixed once by
    ignoring the reply entirely.
    """
    controller, _, _ = make_setup(fade=True)
    await controller.start()
    try:
        result = await controller.set_lightness(UNICAST, 1.0)
    finally:
        await controller.stop()
    assert result == 0xFFFF  # the target, not the 0x7FFF present value


async def test_set_ctl_temperature_returns_the_target_not_the_mid_fade_value():
    controller, _, _ = make_setup(fade=True)
    await controller.start()
    try:
        result = await controller.set_ctl_temperature(UNICAST, 4000)
    finally:
        await controller.stop()
    assert result == 4000  # the target, not the 3500 present value


async def test_set_lightness_returns_the_present_value_when_settled():
    """With no transition in flight there is no target field to prefer."""
    controller, _, _ = make_setup()
    await controller.start()
    try:
        result = await controller.set_lightness(UNICAST, 1.0)
    finally:
        await controller.stop()
    assert result == 0xFFFF


# ------------------------------------------------------- proxy filter setup


async def _drain_proxy_config(bearer, expected: int) -> None:
    """Wait for the TX pump to have drained ``expected`` proxy-config messages."""
    for _ in range(200):
        if len(bearer.proxy_config) >= expected:
            return
        await asyncio.sleep(0.001)


async def test_start_configures_the_proxy_filter():
    """Without this, the proxy forwards NOTHING back to us.

    A proxy server starts each connection with an empty accept list (spec
    §6.5.1), so every Status a node sends is dropped before reaching the
    client. ``start()`` must claim our own address so replies get through.
    """
    controller, bearer, _ = make_setup()
    await controller.start()
    await _drain_proxy_config(bearer, 2)
    try:
        assert bearer.proxy_config == [
            bytes([OP_SET_FILTER_TYPE, FILTER_ACCEPT_LIST]),
            bytes([OP_ADD_ADDRESSES]) + (0x7FFF).to_bytes(2, "big"),
        ]
        # The server confirmed one address in an accept list.
        assert controller.filter_status == FilterStatus(
            filter_type=FILTER_ACCEPT_LIST, list_size=1
        )
    finally:
        await controller.stop()


async def test_start_does_not_wait_for_a_filter_status():
    """start() must not block on a confirmation the proxy may never send.

    Hardware finding (2026-07-26, Häfele/ThingOS lamp): the lamp APPLIES the
    filter — Status replies started coming back — but never answers with a
    Filter Status. Blocking on one therefore added its full timeout to every
    single connection for nothing, and warned that replies "may not be
    forwarded" while they demonstrably were. The two messages are queued on the
    ordered TX pump ahead of any command, so the filter is in place before the
    first Set regardless; waiting buys nothing.
    """
    controller, bearer, _ = make_setup()
    bearer.answer_proxy_config = False

    loop = asyncio.get_running_loop()
    started = loop.time()
    await controller.start()
    elapsed = loop.time() - started
    try:
        assert elapsed < 0.5, f"start() blocked for {elapsed:.2f}s"
        assert controller.filter_status is None  # nothing to record
        # The link is usable regardless.
        assert await controller.set_onoff(UNICAST, True) is True
    finally:
        await controller.stop()


async def test_proxy_filter_messages_travel_as_proxy_config_pdus():
    """They must go out under the proxy-config message type, not as mesh traffic."""
    controller, bearer, _ = make_setup()
    await controller.start()
    await _drain_proxy_config(bearer, 2)
    try:
        types = [msg_type for msg_type, _ in bearer.sent]
        assert types == [MSG_TYPE_PROXY_CONFIG, MSG_TYPE_PROXY_CONFIG]
    finally:
        await controller.stop()


# ----------------------------------------------------------- dead TX pump


async def test_dead_tx_pump_is_reported_as_failed():
    """A GATT write failure kills the TX pump; the controller must SAY so.

    ``BearerPump`` swallows the first ``send()`` error and then stops draining
    its queue for good, so every later command silently times out and returns
    ``None`` — indistinguishable from an unconfirmed Status. Without a failure
    flag the Home Assistant coordinator keeps reusing a controller that can no
    longer transmit anything, leaving the entity "available" while every tap
    does nothing until the entry is reloaded.
    """
    controller, bearer, _ = make_setup()
    await controller.start()

    async def dead_send(msg_type, payload):
        raise RuntimeError("GATT write failed: link wedged")

    bearer.send = dead_send
    try:
        result = await controller.set_onoff(
            UNICAST, True, timeout=0.05, retries=0
        )
    finally:
        await controller.stop()

    assert result is None  # still best-effort: the caller never sees a raise
    assert controller.failed is True
    assert isinstance(controller.failure, RuntimeError)


async def test_healthy_controller_is_not_failed():
    """A controller whose writes succeed never reports a transport failure."""
    controller, _, _ = make_setup()
    await controller.start()
    try:
        await controller.set_onoff(UNICAST, True)
        assert controller.failed is False
        assert controller.failure is None
    finally:
        await controller.stop()


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


# ------------------------------------------------- secure network beacon


async def test_beacon_is_authenticated_and_exposed():
    """The node announces the subnet's IV Index on every fresh connection.

    Ignoring it is how a client silently goes deaf after an IV Update: our own
    IV Index goes stale, every PDU we send is discarded and every PDU we
    receive fails the IVI check, with nothing in the logs to say why.
    """
    controller, bearer, _ = make_setup()
    network = Network.from_connect_file(FIXTURE)
    await controller.start()
    try:
        bearer.feed(
            build_secure_network_beacon(network.net_key, iv_index=0x2A),
            msg_type=MSG_TYPE_MESH_BEACON,
        )
        assert controller.beacon is not None
        assert controller.beacon.iv_index == 0x2A
        assert controller.beacon.iv_update is False
    finally:
        await controller.stop()


async def test_a_beacon_from_another_network_is_ignored():
    """A foreign or forged beacon must never reach our IV Index."""
    controller, bearer, _ = make_setup()
    await controller.start()
    try:
        bearer.feed(
            build_secure_network_beacon(bytes(16), iv_index=0x99),
            msg_type=MSG_TYPE_MESH_BEACON,
        )
        assert controller.beacon is None
    finally:
        await controller.stop()


def test_iv_index_override_is_what_the_node_encrypts_with():
    """The caller carries the IV Index across connections, not the export.

    A .connect export states the IV Index at export time; the mesh moves on
    without telling the file. Whoever persists the beacon-discovered value
    passes it back in here.
    """
    network = Network.from_connect_file(FIXTURE)
    controller = MeshController(network, FakeBearer(), iv_index=0x2A)
    assert controller.iv_index == 0x2A


def test_iv_index_defaults_to_the_network_model():
    network = Network.from_connect_file(FIXTURE)
    controller = MeshController(network, FakeBearer())
    assert controller.iv_index == network.iv_index


async def test_app_key_override_is_what_the_commands_are_encrypted_with():
    """A network can hold several AppKeys; only the bound one is understood.

    A node checks an incoming message's AID against the keys its models were
    bound to and drops anything else without a word, so the controller must be
    able to encrypt with a key other than the export's first.
    """
    network, bearer = Network.from_connect_file(FIXTURE), FakeBearer()
    other = bytes.fromhex("0a1b2c3d4e5f60718293a4b5c6d7e8f9")
    assert other != network.app_key

    controller = MeshController(network, bearer, app_key=other)

    assert controller.app_key == other
    assert MeshController(network, bearer).app_key == network.app_key


# ------------------------------------------------------- composition probe

# Synthetic Composition Data Page 0: CID 0x07E9, PID 1, VID 2, CRPL 10,
# features 0x0007; one element with SIG models 0x0000 + 0x1300 and vendor
# model 0x07E9:0x0001.
_COMPOSITION_PARAMS = bytes.fromhex(
    "00" "e907" "0100" "0200" "0a00" "0700"
    "0001" "02" "01" "0000" "0013" "e907" "0100"
)


def _composition_setup(answer: bool = True):
    """A controller wired to a device that answers Composition Data Get.

    The device answers under its DEVICE key (AKF=0), which is what makes this
    probe independent of the AppKey binding — the point of the whole exercise.
    """
    from btmesh.access import (
        OP_CONFIG_COMPOSITION_DATA_GET,
        OP_CONFIG_COMPOSITION_DATA_STATUS,
        OP_CONFIG_RELAY_GET,
        OP_CONFIG_RELAY_STATUS,
    )

    network = Network.from_connect_file(FIXTURE)
    device_key = network.nodes[0].device_key
    bearer = FakeBearer()
    controller = MeshController(network, bearer)

    device = MeshNode(
        netkey=network.net_key,
        appkey=network.app_key,
        iv_index=network.iv_index,
        src_addr=UNICAST,
        send_network_pdu=bearer.feed,
        seq=0x2000,
    )
    # Its own key, so send_access(dev_key=True) encrypts a reply with it.
    device.add_device(UNICAST, device_key)

    def responder(msg: ReceivedMessage) -> None:
        if not answer:
            return
        if msg.opcode == OP_CONFIG_COMPOSITION_DATA_GET:
            device.send_access(
                msg.src,
                encode_opcode(OP_CONFIG_COMPOSITION_DATA_STATUS)
                + _COMPOSITION_PARAMS,
                dev_key=True,
            )
        elif msg.opcode == OP_CONFIG_RELAY_GET:
            # relay on, retransmit count 2, interval steps 5
            device.send_access(
                msg.src,
                encode_opcode(OP_CONFIG_RELAY_STATUS) + bytes([0x01, 0x2A]),
                dev_key=True,
            )

    device.on_message = responder
    bearer.device = device
    return controller, bearer


async def test_get_composition_reads_the_node_under_its_device_key():
    """What the node says it is, rather than what the export claims.

    Device-keyed, so it answers whatever the AppKey binding, the Light LC mode
    or the vendor models are doing — which is what makes it usable to tell "the
    message never arrived" apart from "the model declined to act".
    """
    controller, _ = _composition_setup()
    await controller.start()
    try:
        comp = await controller.get_composition(UNICAST, timeout=1.0)
    finally:
        await controller.stop()

    assert comp is not None
    assert comp.cid == 0x07E9
    assert comp.elements[0].sig_models == (0x0000, 0x1300)
    assert comp.elements[0].vendor_models == ((0x07E9, 0x0001),)


async def test_get_composition_returns_none_when_the_node_stays_silent():
    """Best-effort like every other command: silence is None, never a raise."""
    controller, _ = _composition_setup(answer=False)
    await controller.start()
    try:
        result = await controller.get_composition(UNICAST, timeout=0.05)
    finally:
        await controller.stop()

    assert result is None


async def test_get_composition_without_a_device_key_is_not_fatal():
    """A node the export gave no usable device key for must not raise."""
    controller, _ = _composition_setup()
    await controller.start()
    try:
        # 0x00FF is in no export; nothing is registered for it.
        result = await controller.get_composition(0x00FF, timeout=0.05)
    finally:
        await controller.stop()

    assert result is None


async def test_get_relay_is_the_probe_that_fits_in_one_segment():
    """The reachability question, asked so the answer cannot be lost to SAR.

    A Composition Data Status is segmented and this stack transmits no Segment
    Acks, so its silence proves nothing. A Config Relay Status is two bytes.
    """
    controller, _ = _composition_setup()
    await controller.start()
    try:
        relay = await controller.get_relay(UNICAST, timeout=1.0)
    finally:
        await controller.stop()

    assert relay is not None
    assert relay.enabled is True
    assert relay.supported is True
    assert relay.retransmit_count == 2
    assert relay.retransmit_interval_steps == 5


async def test_get_relay_returns_none_when_the_node_stays_silent():
    controller, _ = _composition_setup(answer=False)
    await controller.start()
    try:
        result = await controller.get_relay(UNICAST, timeout=0.05)
    finally:
        await controller.stop()

    assert result is None


# ------------------------------------------------------------ segmentation


async def test_segmented_reply_from_a_device_is_acknowledged():
    """A peer that segments its reply is blocked until we acknowledge it (#9).

    Every lighting Status fits in one segment, which is why this went unnoticed;
    it bites the first time a device-keyed exchange needs more than 15 bytes.
    """
    controller, bearer, _ = make_setup()
    await controller.start()
    device = bearer.device
    first_seq = device.ctx.seq

    # 22 bytes of access payload → three segments.
    device.send_access(0x7FFF, encode_opcode(OP_GENERIC_ONOFF_STATUS) + bytes(20))
    await _drain()

    assert device.last_ack(first_seq & 0x1FFF) is not None
    assert device.last_ack(first_seq & 0x1FFF).block_ack == 0b111
    await controller.stop()


async def test_stop_cancels_a_pending_segment_ack(monkeypatch):
    """A stopped controller must not put an ack on air through a dead bearer.

    Observed on the SEQ cursor rather than the bearer: a fired ack burns a
    sequence number whether or not the pump is still there to carry it, and
    that cursor is persisted across restarts.
    """
    monkeypatch.setattr(node_module, "SEG_ACK_TIMEOUT_BASE", 0.01)
    monkeypatch.setattr(node_module, "SEG_ACK_TIMEOUT_PER_TTL", 0.0)
    controller, bearer, _ = make_setup()
    await controller.start()
    device = bearer.device

    # Capture the device's three segments instead of delivering them...
    segments: list[bytes] = []
    real_feed = bearer.feed
    bearer.feed = lambda pdu, msg_type=MSG_TYPE_NETWORK_PDU: segments.append(pdu)
    device.send_access(0x7FFF, encode_opcode(OP_GENERIC_ONOFF_STATUS) + bytes(20))
    bearer.feed = real_feed

    # ...then hand over only the first, which arms the acknowledgment timer.
    real_feed(segments[0])
    await _drain()
    seq_at_stop = controller.seq

    await controller.stop()
    await asyncio.sleep(0.05)

    assert controller.seq == seq_at_stop


async def _drain(rounds: int = 5) -> None:
    """Let the TX pump move queued PDUs onto the fake bearer."""
    for _ in range(rounds):
        await asyncio.sleep(0)
