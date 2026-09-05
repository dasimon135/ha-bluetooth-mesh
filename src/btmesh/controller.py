"""MeshController: a clean async facade over MeshNode + BearerPump + GattBearer.

This is the surface the Home Assistant integration drives. It hides the sync
producer / async bearer plumbing (:class:`btmesh.pump.BearerPump`) and the
raw access-message codecs behind a handful of high-level, best-effort coroutines
(``set_onoff``, ``get_onoff``, ``set_lightness``, ``set_ctl_temperature``).

Best-effort means a control command returns ``None`` on timeout rather than
raising, so the integration can mark an entity unavailable without special-casing
exceptions. The :class:`btmesh.network_model.Network` supplies the keys and IV
index; addressing is by element unicast, taken from that same static model.
"""

from __future__ import annotations

import logging

from .access import (
    OP_CONFIG_COMPOSITION_DATA_STATUS,
    OP_CONFIG_RELAY_STATUS,
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_CTL_STATUS,
    OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS,
    OP_LIGHT_CTL_TEMPERATURE_STATUS,
    OP_LIGHT_LIGHTNESS_STATUS,
    AccessError,
    CompositionData,
    RelayStatus,
    config_composition_data_get,
    config_relay_get,
    encode_opcode,
    generic_onoff_get,
    generic_onoff_set,
    light_ctl_get,
    light_ctl_set,
    light_ctl_temperature_get,
    light_ctl_temperature_range_get,
    light_ctl_temperature_set,
    light_lightness_get,
    light_lightness_set,
    parse_composition_data_status,
    parse_config_relay_status,
    parse_generic_onoff_status,
    parse_light_ctl_status,
    parse_light_ctl_temperature_range_status,
    parse_light_ctl_temperature_status,
    parse_light_lightness_status,
)
from .beacon import BeaconError, SecureNetworkBeacon, parse_secure_network_beacon
from .bearer import GattBearer
from .network_model import Network
from .node import MeshNode, NodeError, ReceivedMessage
from .proxy_config import (
    FILTER_ACCEPT_LIST,
    FilterStatus,
    ProxyConfigError,
    add_addresses,
    parse_filter_status,
    set_filter_type,
)
from .proxy_pdu import (
    MSG_TYPE_MESH_BEACON,
    MSG_TYPE_NETWORK_PDU,
    MSG_TYPE_PROXY_CONFIG,
)
from .pump import BearerPump

logger = logging.getLogger(__name__)

__all__ = ["MeshController"]

# CTL temperature is a 16-bit Kelvin value clamped to the model's valid range
# (Mesh Model spec §6.1.3.1): 800 K .. 20000 K.
_CTL_TEMP_MIN = 800
_CTL_TEMP_MAX = 20000


def _full_payload(msg: ReceivedMessage) -> bytes:
    """Rebuild the on-air access payload the access.parse_* helpers expect."""
    return encode_opcode(msg.opcode) + msg.params


def _settled(present, target):
    """Where the lamp will END UP: the target while a transition is running.

    A Status sent mid-transition carries the value the lamp is currently
    passing through plus the one it is heading for. A Set asked for the latter,
    so reporting the former back would make a caller's cached state chase a
    transient — a brightness slider snapping to a half-way value mid-fade.
    """
    return present if target is None else target


class MeshController:
    """Async facade for controlling nodes on one mesh subnet through a proxy.

    Build one per :class:`~btmesh.network_model.Network` / bearer pair, then
    :meth:`start` it (subscribes the bearer, starts the TX pump), issue the
    high-level commands, and :meth:`stop` it. All commands are best-effort:
    they return ``None`` on timeout instead of raising.

    ``seq`` seeds the underlying node's sequence cursor: pass the last persisted
    :attr:`seq` (plus a safety margin) when reconstructing a controller so the
    network never reuses a SEQ the mesh has already seen (replay safety).
    ``app_key`` overrides the network's first application key, for a network
    whose target models are bound to a different one.
    """

    def __init__(
        self,
        network: Network,
        bearer: GattBearer,
        *,
        src_addr: int = 0x7FFF,
        seq: int = 0,
        tid: int = 0,
        iv_index: int | None = None,
        app_key: bytes | None = None,
    ) -> None:
        self._bearer = bearer
        self._pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
        self._net_key = network.net_key
        # The export states the IV Index as it was at export time; the mesh
        # moves on without telling the file. The caller passes back whatever it
        # persisted from a Secure Network Beacon (see :attr:`beacon`).
        self._iv_index = network.iv_index if iv_index is None else iv_index
        self._beacon: SecureNetworkBeacon | None = None
        # An export may carry several application keys, and a node only accepts
        # the one its models were bound to. The caller resolves that binding
        # (:meth:`btmesh.network_model.Network.app_key_for_models`); the
        # export's first key is merely the default.
        self._app_key = network.app_key if app_key is None else app_key
        self._node = MeshNode(
            netkey=network.net_key,
            appkey=self._app_key,
            iv_index=self._iv_index,
            src_addr=src_addr,
            send_network_pdu=self._pump.put,
            seq=seq,
        )
        # Every node's device key, so Foundation-model traffic (the composition
        # probe) can be addressed to any of them. Device-keyed messages bypass
        # the AppKey binding entirely, which is what makes them the honest test
        # of whether a node is reachable at all.
        for node in network.nodes:
            try:
                self._node.add_device(node.unicast, node.device_key)
            except NodeError as exc:  # a key the export got wrong
                logger.debug("no usable device key for %#06x: %s", node.unicast, exc)
        # A GATT write failure kills the TX pump: it records the error and stops
        # draining its queue for good. Commands stay best-effort (they time out
        # and return None), so without this flag the failure is indistinguishable
        # from an unconfirmed Status and a caller would keep reusing a controller
        # that can no longer transmit. Mirror it here so the caller can drop the
        # link and reconnect.
        self._pump.on_error = self._on_pump_error
        self._src = src_addr
        # Proxy address-filter state (see _configure_filter).
        self._filter_status: FilterStatus | None = None
        # The Generic OnOff / Lightness / CTL Set messages carry a TID; the node
        # DEDUPLICATES consecutive Sets that share (src, TID) within a short
        # window. So the TID must keep advancing ACROSS commands. When a fresh
        # controller is built per command (on-demand connection), seed the TID
        # from the caller's persistent cursor so ON then OFF don't collide on
        # TID 0 (which would make the node ignore the second as a retransmit).
        self._tid = tid & 0xFF

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Subscribe the bearer, start the TX pump, and claim the proxy filter."""
        await self._bearer.start(self._on_message)
        self._pump.start()
        self._configure_filter()

    async def stop(self) -> None:
        """Stop the TX pump and the bearer (reverse of :meth:`start`)."""
        await self._pump.stop()
        await self._bearer.stop()
        # Release the node's reassembly timers last: one could otherwise fire
        # after the bearer is gone, burning a SEQ on an ack nothing can carry.
        self._node.close()

    def _on_message(self, msg_type: int, payload: bytes) -> None:
        if msg_type == MSG_TYPE_NETWORK_PDU:
            self._node.handle_network_pdu(payload)
        elif msg_type == MSG_TYPE_PROXY_CONFIG:
            self._handle_proxy_config(payload)
        elif msg_type == MSG_TYPE_MESH_BEACON:
            self._handle_beacon(payload)
        else:
            logger.debug("ignoring proxy message type %#04x", msg_type)

    # ---------------------------------------------------------- beacon

    def _handle_beacon(self, payload: bytes) -> None:
        """Record the subnet's announced IV Index, if the beacon authenticates.

        Best-effort and strictly read-only: a beacon that is malformed, foreign
        or forged is dropped without touching anything. Adopting the value is
        the caller's decision (it owns the persistence and the SEQ cursor that
        goes with it).
        """
        try:
            beacon = parse_secure_network_beacon(payload, self._net_key)
        except BeaconError as exc:
            logger.debug("ignoring mesh beacon: %s", exc)
            return
        self._beacon = beacon
        if beacon.iv_index != self._iv_index:
            logger.warning(
                "subnet announces IV Index %#x but we are using %#x; "
                "traffic will be dropped until the new index is adopted",
                beacon.iv_index, self._iv_index,
            )
        if beacon.iv_update:
            logger.info("subnet is running an IV Update")

    @property
    def app_key(self) -> bytes:
        """The application key this controller encrypts commands with."""
        return self._app_key

    @property
    def beacon(self) -> SecureNetworkBeacon | None:
        """The last authenticated Secure Network Beacon, if any."""
        return self._beacon

    @property
    def iv_index(self) -> int:
        """The IV Index this controller encrypts with."""
        return self._iv_index

    # ------------------------------------------------------- proxy filter

    def _configure_filter(self) -> None:
        """Ask the proxy to forward the traffic addressed to us.

        A Proxy Server starts every connection with an accept list that is
        EMPTY (spec §6.5.1) — it forwards nothing inbound until told otherwise,
        which is why an unconfigured connection never sees a single Status.
        Setting the type (which clears the list) and adding our own address is
        what makes confirmed state possible.

        Fire-and-forget by design. The spec says the server answers each change
        with a Filter Status, but a real ThingOS/Häfele lamp applies the filter
        and never sends one, so waiting for it added its whole timeout to every
        connection and warned about replies that were in fact being forwarded.
        Nothing is lost by not waiting: both messages are queued on the ordered
        TX pump ahead of any command, so the filter is in place before the first
        Set reaches the node. A Status is still recorded if one does arrive.
        """
        try:
            self._send_proxy_config(set_filter_type(FILTER_ACCEPT_LIST))
            self._send_proxy_config(add_addresses([self._src]))
        except Exception as exc:  # noqa: BLE001 - never fail the connection
            logger.warning("could not configure the proxy filter: %s", exc)

    def _send_proxy_config(self, message: bytes) -> None:
        """Queue one proxy configuration message on the shared TX pump."""
        self._pump.put(
            self._node.build_proxy_config_pdu(message),
            msg_type=MSG_TYPE_PROXY_CONFIG,
        )

    def _handle_proxy_config(self, payload: bytes) -> None:
        message = self._node.parse_proxy_config_pdu(payload)
        if message is None:
            return
        try:
            status = parse_filter_status(message)
        except ProxyConfigError as exc:
            logger.debug("ignoring proxy configuration message: %s", exc)
            return
        logger.debug(
            "proxy filter: type %#04x, %d address(es)",
            status.filter_type, status.list_size,
        )
        self._filter_status = status

    @property
    def filter_status(self) -> FilterStatus | None:
        """The proxy's last Filter Status, or ``None`` if it never answered."""
        return self._filter_status

    def _on_pump_error(self, exc: BaseException) -> None:
        logger.warning("mesh transport died, controller is unusable: %s", exc)

    @property
    def failure(self) -> BaseException | None:
        """The transport error that killed the link, if any.

        Either half can die silently: the TX pump on a GATT write, or the
        bearer's Data Out subscribe when it fails only after its grace period
        (``GattBearer.failure``). The pump is consulted first because a write
        error is the more specific diagnosis when both are set.
        """
        return self._pump.failure or getattr(self._bearer, "failure", None)

    @property
    def failed(self) -> bool:
        """True once the link died: nothing can be transmitted or received.

        Best-effort commands cannot report this by raising, so callers must
        check it to tell "the node did not answer" (normal, the proxy filter
        forwards no Status) apart from "we can no longer send at all" — the
        latter requires dropping the link and reconnecting. A lost subscribe
        counts too: with no Data Out, no Status can ever come back, and on
        2026-09-04 the proxy dropped the connection along with it.
        """
        return self.failure is not None

    @property
    def seq(self) -> int:
        """Persistable sequence cursor (mirror to storage to avoid SEQ replay)."""
        return self._node.ctx.seq

    @property
    def tid(self) -> int:
        """The next transaction ID; carry it across on-demand controllers."""
        return self._tid

    def _next_tid(self) -> int:
        tid = self._tid
        self._tid = (self._tid + 1) & 0xFF
        return tid

    # ------------------------------------------------------------- commands

    async def get_relay(
        self, unicast: int, *, timeout: float = 5.0
    ) -> RelayStatus | None:
        """Read ``unicast``'s Relay state under its device key; None if silent.

        This is the reachability probe to trust. Both directions fit in one
        unsegmented message — the request is two bytes, the Status is four — so
        a silence here is a real silence, not a segmented reply that never
        reassembled. :meth:`get_composition` cannot make that promise: its
        Status is segmented and this stack transmits no Segment Acks.

        The answer is worth having for itself, too. A node with the Relay
        feature off forwards nothing from our proxy connection into the rest of
        the mesh, which is invisible from every other angle.
        """
        try:
            resp = await self._node.request(
                unicast, config_relay_get(), OP_CONFIG_RELAY_STATUS,
                dev_key=True, timeout=timeout, retries=0,
            )
        except TimeoutError:
            logger.debug("get_relay(%#06x) timed out", unicast)
            return None
        except NodeError as exc:
            logger.debug("get_relay(%#06x): %s", unicast, exc)
            return None
        try:
            return parse_config_relay_status(_full_payload(resp))
        except AccessError as exc:
            logger.debug("unparseable relay status from %#06x: %s", unicast, exc)
            return None

    async def get_composition(
        self, unicast: int, *, page: int = 0, timeout: float = 5.0
    ) -> CompositionData | None:
        """Read Composition Data Page 0 from ``unicast``; ``None`` if silent.

        Device-keyed (Foundation model), and that is the whole point: it is
        answered by the node's Config Server without consulting an AppKey
        binding, a Light LC mode, or a vendor model. So it separates the two
        questions every silent mesh failure conflates — *did the message reach
        the node* and *did the node choose to act on it*. An answer proves the
        round trip; silence points at the transport rather than the model.

        It also reports what the node says it is, which the ``.connect`` export
        only claims: the export is the vendor app's account of the node.
        """
        try:
            resp = await self._node.request(
                unicast, config_composition_data_get(page),
                OP_CONFIG_COMPOSITION_DATA_STATUS,
                dev_key=True, timeout=timeout, retries=0,
            )
        except TimeoutError:
            logger.debug("get_composition(%#06x) timed out", unicast)
            return None
        except NodeError as exc:  # no device key registered for that address
            logger.debug("get_composition(%#06x): %s", unicast, exc)
            return None
        try:
            return parse_composition_data_status(_full_payload(resp))
        except AccessError as exc:
            logger.debug("unparseable composition from %#06x: %s", unicast, exc)
            return None

    async def set_onoff(
        self, unicast: int, on: bool, *, timeout: float = 5.0, retries: int = 1
    ) -> bool | None:
        """Set Generic OnOff; return the settled state, or None on timeout."""
        payload = generic_onoff_set(on, self._next_tid())
        try:
            resp = await self._node.request(
                unicast, payload, OP_GENERIC_ONOFF_STATUS,
                timeout=timeout, retries=retries,
            )
        except TimeoutError:
            logger.debug("set_onoff(%#06x) timed out", unicast)
            return None
        status = parse_generic_onoff_status(_full_payload(resp))
        return bool(_settled(status.present_onoff, status.target_onoff))

    async def get_onoff(
        self, unicast: int, *, timeout: float = 5.0
    ) -> bool | None:
        """Read Generic OnOff; return the present state, or None on timeout."""
        try:
            resp = await self._node.request(
                unicast, generic_onoff_get(), OP_GENERIC_ONOFF_STATUS,
                timeout=timeout,
            )
        except TimeoutError:
            logger.debug("get_onoff(%#06x) timed out", unicast)
            return None
        return bool(parse_generic_onoff_status(_full_payload(resp)).present_onoff)

    async def get_lightness(
        self, unicast: int, *, timeout: float = 5.0
    ) -> int | None:
        """Read Light Lightness; return present lightness (0..0xFFFF) or None."""
        try:
            resp = await self._node.request(
                unicast, light_lightness_get(), OP_LIGHT_LIGHTNESS_STATUS,
                timeout=timeout,
            )
        except TimeoutError:
            logger.debug("get_lightness(%#06x) timed out", unicast)
            return None
        return parse_light_lightness_status(_full_payload(resp)).present_lightness

    async def set_lightness(
        self, unicast: int, level_0_1: float, *, timeout: float = 5.0
    ) -> int | None:
        """Set Light Lightness from a 0..1 level; return settled lightness or None."""
        level = min(1.0, max(0.0, level_0_1))
        lightness = round(level * 0xFFFF)
        payload = light_lightness_set(lightness, self._next_tid())
        try:
            resp = await self._node.request(
                unicast, payload, OP_LIGHT_LIGHTNESS_STATUS, timeout=timeout
            )
        except TimeoutError:
            logger.debug("set_lightness(%#06x) timed out", unicast)
            return None
        status = parse_light_lightness_status(_full_payload(resp))
        return _settled(status.present_lightness, status.target_lightness)

    async def set_ctl(
        self, unicast: int, level_0_1: float, kelvin: int, *, timeout: float = 5.0
    ) -> int | None:
        """Set Light CTL (lightness + temperature) in one message.

        Tunable-white lamps generally respond to the Light CTL Server's
        ``Light CTL Set`` (which carries lightness, temperature and delta UV
        together) rather than the standalone Light CTL Temperature Set. Returns
        the settled temperature, or None on timeout.
        """
        level = min(1.0, max(0.0, level_0_1))
        lightness = round(level * 0xFFFF)
        temperature = min(_CTL_TEMP_MAX, max(_CTL_TEMP_MIN, kelvin))
        payload = light_ctl_set(lightness, temperature, 0, self._next_tid())
        try:
            resp = await self._node.request(
                unicast, payload, OP_LIGHT_CTL_STATUS, timeout=timeout
            )
        except TimeoutError:
            logger.debug("set_ctl(%#06x) timed out", unicast)
            return None
        status = parse_light_ctl_status(_full_payload(resp))
        return _settled(status.present_temperature, status.target_temperature)

    async def get_ctl_temperature(
        self, unicast: int, *, timeout: float = 5.0
    ) -> int | None:
        """Read Light CTL Temperature; settled Kelvin or None.

        Addressed to the Light CTL Temperature Server, which sits on its own
        element. ``_settled`` resolves a mid-transition report the same way
        every ``set_*`` does: a lamp still ramping names both where it is and
        where it is going, and where it is going is the answer.
        """
        try:
            resp = await self._node.request(
                unicast, light_ctl_temperature_get(),
                OP_LIGHT_CTL_TEMPERATURE_STATUS, timeout=timeout,
            )
        except TimeoutError:
            logger.debug("get_ctl_temperature(%#06x) timed out", unicast)
            return None
        status = parse_light_ctl_temperature_status(_full_payload(resp))
        return _settled(status.present_temperature, status.target_temperature)

    async def get_ctl(
        self, unicast: int, *, timeout: float = 5.0
    ) -> int | None:
        """Read Light CTL; settled temperature in Kelvin or None.

        The fallback for a node that exposes no Light CTL Temperature Server
        element: its Light CTL Status carries temperature next to lightness.
        Sending already falls back to Light CTL Set on such a node, so reading
        has to as well, or it would be written and never read.
        """
        try:
            resp = await self._node.request(
                unicast, light_ctl_get(), OP_LIGHT_CTL_STATUS, timeout=timeout
            )
        except TimeoutError:
            logger.debug("get_ctl(%#06x) timed out", unicast)
            return None
        status = parse_light_ctl_status(_full_payload(resp))
        return _settled(status.present_temperature, status.target_temperature)

    async def get_ctl_temperature_range(
        self, unicast: int, *, timeout: float = 5.0
    ) -> tuple[int, int] | None:
        """Read the lamp's own Kelvin limits, or None if it has none to give.

        A non-zero status code is a well-formed answer meaning *I have no valid
        range*, and the min/max beside it carry nothing. The caller falls back
        to a default, so believing those bytes would replace a reasonable range
        with junk — it must read as silence.
        """
        try:
            resp = await self._node.request(
                unicast, light_ctl_temperature_range_get(),
                OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS, timeout=timeout,
            )
        except TimeoutError:
            logger.debug("get_ctl_temperature_range(%#06x) timed out", unicast)
            return None
        status = parse_light_ctl_temperature_range_status(_full_payload(resp))
        if status.status_code != 0x00:
            logger.debug(
                "get_ctl_temperature_range(%#06x): status %#04x, no range",
                unicast, status.status_code,
            )
            return None
        return status.range_min, status.range_max

    async def set_ctl_temperature(
        self, unicast: int, kelvin: int, *, timeout: float = 5.0
    ) -> int | None:
        """Set Light CTL Temperature only (Temperature Server); settled temp or None."""
        temperature = min(_CTL_TEMP_MAX, max(_CTL_TEMP_MIN, kelvin))
        payload = light_ctl_temperature_set(temperature, 0, self._next_tid())
        try:
            resp = await self._node.request(
                unicast, payload, OP_LIGHT_CTL_TEMPERATURE_STATUS, timeout=timeout
            )
        except TimeoutError:
            logger.debug("set_ctl_temperature(%#06x) timed out", unicast)
            return None
        status = parse_light_ctl_temperature_status(_full_payload(resp))
        return _settled(status.present_temperature, status.target_temperature)
