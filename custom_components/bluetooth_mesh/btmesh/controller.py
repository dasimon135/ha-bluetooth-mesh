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
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_CTL_STATUS,
    OP_LIGHT_CTL_TEMPERATURE_STATUS,
    OP_LIGHT_LIGHTNESS_STATUS,
    encode_opcode,
    generic_onoff_get,
    generic_onoff_set,
    light_ctl_set,
    light_ctl_temperature_set,
    light_lightness_get,
    light_lightness_set,
    parse_generic_onoff_status,
    parse_light_ctl_status,
    parse_light_ctl_temperature_status,
    parse_light_lightness_status,
)
from .bearer import GattBearer
from .network_model import Network
from .node import MeshNode, ReceivedMessage
from .proxy_pdu import MSG_TYPE_NETWORK_PDU
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


class MeshController:
    """Async facade for controlling nodes on one mesh subnet through a proxy.

    Build one per :class:`~btmesh.network_model.Network` / bearer pair, then
    :meth:`start` it (subscribes the bearer, starts the TX pump), issue the
    high-level commands, and :meth:`stop` it. All commands are best-effort:
    they return ``None`` on timeout instead of raising.

    ``seq`` seeds the underlying node's sequence cursor: pass the last persisted
    :attr:`seq` (plus a safety margin) when reconstructing a controller so the
    network never reuses a SEQ the mesh has already seen (replay safety).
    """

    def __init__(
        self,
        network: Network,
        bearer: GattBearer,
        *,
        src_addr: int = 0x7FFF,
        seq: int = 0,
        tid: int = 0,
    ) -> None:
        self._bearer = bearer
        self._pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
        self._node = MeshNode(
            netkey=network.net_key,
            appkey=network.app_key,
            iv_index=network.iv_index,
            src_addr=src_addr,
            send_network_pdu=self._pump.put,
            seq=seq,
        )
        # The Generic OnOff / Lightness / CTL Set messages carry a TID; the node
        # DEDUPLICATES consecutive Sets that share (src, TID) within a short
        # window. So the TID must keep advancing ACROSS commands. When a fresh
        # controller is built per command (on-demand connection), seed the TID
        # from the caller's persistent cursor so ON then OFF don't collide on
        # TID 0 (which would make the node ignore the second as a retransmit).
        self._tid = tid & 0xFF

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Subscribe the bearer to our RX path and start the TX pump."""
        await self._bearer.start(self._on_message)
        self._pump.start()

    async def stop(self) -> None:
        """Stop the TX pump and the bearer (reverse of :meth:`start`)."""
        await self._pump.stop()
        await self._bearer.stop()

    def _on_message(self, msg_type: int, payload: bytes) -> None:
        if msg_type == MSG_TYPE_NETWORK_PDU:
            self._node.handle_network_pdu(payload)
        else:
            logger.debug("ignoring proxy message type %#04x", msg_type)

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

    async def set_onoff(
        self, unicast: int, on: bool, *, timeout: float = 5.0, retries: int = 1
    ) -> bool | None:
        """Set Generic OnOff; return the reported present state, or None on timeout."""
        payload = generic_onoff_set(on, self._next_tid())
        try:
            resp = await self._node.request(
                unicast, payload, OP_GENERIC_ONOFF_STATUS,
                timeout=timeout, retries=retries,
            )
        except TimeoutError:
            logger.debug("set_onoff(%#06x) timed out", unicast)
            return None
        return bool(parse_generic_onoff_status(_full_payload(resp)).present_onoff)

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
        """Set Light Lightness from a 0..1 level; return present lightness or None."""
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
        return parse_light_lightness_status(_full_payload(resp)).present_lightness

    async def set_ctl(
        self, unicast: int, level_0_1: float, kelvin: int, *, timeout: float = 5.0
    ) -> int | None:
        """Set Light CTL (lightness + temperature) in one message.

        Tunable-white lamps generally respond to the Light CTL Server's
        ``Light CTL Set`` (which carries lightness, temperature and delta UV
        together) rather than the standalone Light CTL Temperature Set. Returns
        the present temperature, or None on timeout.
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
        return parse_light_ctl_status(_full_payload(resp)).present_temperature

    async def set_ctl_temperature(
        self, unicast: int, kelvin: int, *, timeout: float = 5.0
    ) -> int | None:
        """Set Light CTL Temperature only (Temperature Server); present temp or None."""
        temperature = min(_CTL_TEMP_MAX, max(_CTL_TEMP_MIN, kelvin))
        payload = light_ctl_temperature_set(temperature, 0, self._next_tid())
        try:
            resp = await self._node.request(
                unicast, payload, OP_LIGHT_CTL_TEMPERATURE_STATUS, timeout=timeout
            )
        except TimeoutError:
            logger.debug("set_ctl_temperature(%#06x) timed out", unicast)
            return None
        return parse_light_ctl_temperature_status(
            _full_payload(resp)
        ).present_temperature
