"""MeshNode: provisioner-side runtime gluing the validated codec layers.

One node = one subnet (single NetKey/AppKey, fixed IV Index) talking through a
raw network-PDU callback; the bearer (GATT proxy) wraps and unwraps proxy
framing outside this class. Phase 0 scope: no relay, no publish/subscribe,
no ack transmission, no virtual addressing.
"""

import asyncio
import logging
from collections import deque
from typing import Callable, NamedTuple

from . import network
from .access import AccessError, parse_access
from .crypto import k4
from .errors import BtMeshError
from .network import NetworkContext, NetworkError
from .transport import (
    SEGMENT_SIZE,
    TRANS_MIC_LEN,
    UNSEG_MAX_UPPER_LEN,
    SegmentAck,
    SegmentAssembler,
    TransportError,
    UnsegmentedAccess,
    build_unsegmented_access,
    decrypt_access,
    encrypt_access,
    parse_access_lower,
    parse_control_lower,
    segment_access_message,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TTL",
    "NodeError",
    "ReceivedMessage",
    "MeshNode",
]

DEFAULT_TTL = 7  # hops enough for any Phase 0 topology (matches Zephyr default)
_RECEIVED_MAXLEN = 128


class NodeError(BtMeshError):
    """The MeshNode could not perform the requested operation."""


class ReceivedMessage(NamedTuple):
    """A decrypted, parsed access message."""

    src: int
    opcode: int
    params: bytes


def _seq_auth(seq: int, seq_zero: int) -> int:
    """Recover the first segment's SEQ (SeqAuth) from any segment (spec §3.5.3.1).

    ``seq_zero`` is the 13 LSBs of the first segment's SEQ; any later segment's
    SEQ is at most 0x1FFF greater, so rewind to the closest SEQ at or below
    ``seq`` whose 13 LSBs equal ``seq_zero``.
    """
    seq_auth = (seq & ~0x1FFF) | seq_zero
    if seq_auth > seq:
        seq_auth -= 0x2000
    return seq_auth


class MeshNode:
    """Send and receive access messages over one mesh subnet.

    ``send_network_pdu`` receives every outgoing raw network PDU and must not
    block: it is called synchronously from :meth:`send_access` /
    :meth:`request`, so an async bearer should enqueue the PDU and return
    (TODO Phase 1: native async callback support). Feed every incoming raw
    network PDU to :meth:`handle_network_pdu`. Decrypted messages land in
    :attr:`received` (newest last) and, when set, are passed to
    :attr:`on_message`.
    """

    def __init__(
        self,
        *,
        netkey: bytes,
        appkey: bytes,
        iv_index: int,
        src_addr: int,
        send_network_pdu: Callable[[bytes], None],
        seq: int = 0,
    ) -> None:
        self._ctx = NetworkContext(net_key=netkey, iv_index=iv_index, seq=seq)
        self._appkey = appkey
        self._aid = k4(appkey)
        self._iv_index = iv_index
        self._src = src_addr
        self._send = send_network_pdu
        self._assembler = SegmentAssembler()
        self._device_keys: dict[int, bytes] = {}
        self._acks: dict[int, SegmentAck] = {}
        # Last SEQ accepted per source, to drop an immediate duplicate delivery
        # (a relayed echo of a PDU we already handled). Deliberately NOT the
        # spec's replay list: rejecting every SEQ *below* the last one would
        # deafen us permanently to a node that restarted its sequence after a
        # power cut, and the worst a replayed Status can do is show a stale
        # value for a moment. The lesser guarantee is the safer trade.
        self._last_rx_seq: dict[int, int] = {}
        self._waiters: list[tuple[int, int, asyncio.Future[ReceivedMessage]]] = []
        self.received: deque[ReceivedMessage] = deque(maxlen=_RECEIVED_MAXLEN)
        self.on_message: Callable[[ReceivedMessage], None] | None = None

    # ------------------------------------------------------------------- TX

    @property
    def ctx(self) -> NetworkContext:
        """Network context; ``ctx.seq`` is the persistable sequence cursor."""
        return self._ctx

    def add_device(self, unicast: int, device_key: bytes) -> None:
        """Register the device key of the node whose primary address is ``unicast``.

        Caveat: registering a key for your own address disables the
        unregistered-dst typo guard for outgoing dev-key traffic, because
        :meth:`send_access` falls back to the own-address key for any ``dst``.
        """
        if len(device_key) != 16:
            raise NodeError(f"device key must be 16 bytes, got {len(device_key)}")
        self._device_keys[unicast] = device_key

    def send_access(
        self,
        dst: int,
        payload: bytes,
        *,
        dev_key: bool = False,
        ttl: int = DEFAULT_TTL,
    ) -> None:
        """Encrypt and send one access payload to ``dst``.

        ``dev_key=True`` uses the device key registered for ``dst`` (AKF=0,
        Foundation/Config messages), falling back to the key registered for
        our own address — a node answering its provisioner encrypts with its
        own device key. The default uses the shared AppKey (AKF=1).
        """
        if dev_key:
            key = self._device_keys.get(dst)
            if key is None:
                logger.debug(
                    "no device key for %#06x, encrypting with own device key", dst
                )
                key = self._device_keys.get(self._src)
            if key is None:
                raise NodeError(f"no device key registered for {dst:#06x}")
            akf, aid = False, 0
        else:
            key, akf, aid = self._appkey, True, self._aid

        upper_len = len(payload) + TRANS_MIC_LEN
        if upper_len <= UNSEG_MAX_UPPER_LEN:
            seq = self._ctx.next_seq()
            upper = encrypt_access(
                key, akf=akf, seq=seq, src=self._src, dst=dst,
                iv_index=self._iv_index, access_pdu=payload,
            )
            pdus = [(seq, build_unsegmented_access(upper, akf=akf, aid=aid))]
        else:
            seg_count = (upper_len + SEGMENT_SIZE - 1) // SEGMENT_SIZE
            # Allocate the whole SEQ block up front: the first SEQ feeds the
            # upper-transport nonce and each segment burns one SEQ.
            first_seq = self._ctx.next_seq(seg_count)
            upper = encrypt_access(
                key, akf=akf, seq=first_seq, src=self._src, dst=dst,
                iv_index=self._iv_index, access_pdu=payload,
            )
            pdus = segment_access_message(
                upper, akf=akf, aid=aid, first_seq=first_seq
            )

        for seq, lower in pdus:
            self._send(
                network.encode(
                    self._ctx, ctl=False, ttl=ttl, seq=seq, src=self._src,
                    dst=dst, transport_pdu=lower,
                )
            )

    # --------------------------------------------------- proxy configuration

    def build_proxy_config_pdu(self, message: bytes) -> bytes:
        """Wrap a proxy configuration message in a network PDU (spec §6.5).

        Proxy configuration travels with ``CTL=1``, ``TTL=0`` and the unassigned
        address as destination: it is consumed by the proxy node we are
        connected to, never relayed into the mesh. It still burns a SEQ like any
        other network PDU.

        Returned rather than sent: it must go out in a Proxy PDU of type
        ``MSG_TYPE_PROXY_CONFIG``, not the network-PDU type this node's
        ``send_network_pdu`` callback is wired to, so routing is the caller's.
        """
        if not message:
            raise NodeError("empty proxy configuration message")
        return network.encode(
            self._ctx, ctl=True, ttl=0, seq=self._ctx.next_seq(),
            src=self._src, dst=0x0000, transport_pdu=message,
        )

    def parse_proxy_config_pdu(self, raw: bytes) -> bytes | None:
        """Recover a proxy configuration message; ``None`` for foreign traffic.

        The proxy server answers on its own address rather than the unassigned
        one, so the destination is deliberately not checked.
        """
        try:
            pdu = network.decode(self._ctx, raw)
        except NetworkError as exc:
            logger.debug("ignoring proxy config PDU: %s", exc)
            return None
        if not pdu.ctl:
            logger.debug("proxy config PDU is not a control message, ignoring")
            return None
        return pdu.transport_pdu

    # ------------------------------------------------------------------- RX

    def handle_network_pdu(self, raw: bytes) -> None:
        """Process one incoming raw network PDU; never raises on foreign traffic."""
        try:
            pdu = network.decode(self._ctx, raw)
        except NetworkError as exc:
            logger.debug("ignoring network PDU: %s", exc)
            return
        if pdu.src == self._src:
            logger.debug("ignoring self-echo from %#06x", pdu.src)
            return
        if self._last_rx_seq.get(pdu.src) == pdu.seq:
            logger.debug(
                "ignoring duplicate PDU from %#06x (seq %#x)", pdu.src, pdu.seq
            )
            return
        self._last_rx_seq[pdu.src] = pdu.seq
        if pdu.ctl:
            self._handle_control(pdu)
        else:
            self._handle_access(pdu)

    def last_ack(self, seq_zero: int) -> SegmentAck | None:
        """Latest Segment Ack seen for ``seq_zero``, if any."""
        return self._acks.get(seq_zero)

    def _handle_control(self, pdu: network.DecodedNetworkPDU) -> None:
        try:
            msg = parse_control_lower(pdu.transport_pdu)
        except TransportError as exc:
            logger.debug("ignoring control PDU from %#06x: %s", pdu.src, exc)
            return
        if isinstance(msg, SegmentAck):
            self._acks[msg.seq_zero] = msg
        else:
            logger.debug(
                "unknown control opcode %#04x from %#06x", msg.opcode, pdu.src
            )

    def _handle_access(self, pdu: network.DecodedNetworkPDU) -> None:
        try:
            parsed = parse_access_lower(pdu.transport_pdu)
            if isinstance(parsed, UnsegmentedAccess):
                upper, seq_auth = parsed.upper_pdu, pdu.seq
            else:
                upper = self._assembler.add(parsed)
                if upper is None:
                    return  # more segments pending
                seq_auth = _seq_auth(pdu.seq, parsed.seq_zero)
                if seq_auth < 0:
                    logger.debug("implausible SeqAuth, dropping reassembled message")
                    return
        except TransportError as exc:
            logger.debug("ignoring access PDU from %#06x: %s", pdu.src, exc)
            return

        key = self._rx_key(parsed.akf, parsed.aid, pdu.src)
        if key is None:
            return
        try:
            access_payload = decrypt_access(
                key, akf=parsed.akf, seq=seq_auth, src=pdu.src, dst=pdu.dst,
                iv_index=self._iv_index, upper_pdu=upper,
            )
        except TransportError as exc:
            logger.debug("upper transport decrypt failed from %#06x: %s", pdu.src, exc)
            return
        try:
            opcode, params = parse_access(access_payload)
        except AccessError as exc:
            logger.debug("unparseable access payload from %#06x: %s", pdu.src, exc)
            return
        self._deliver(ReceivedMessage(src=pdu.src, opcode=opcode, params=params))

    def _rx_key(self, akf: bool, aid: int, src: int) -> bytes | None:
        if akf:
            if aid != self._aid:
                logger.debug("AID %#04x does not match our AppKey, dropping", aid)
                return None
            return self._appkey
        # AKF=0: a config exchange under a device key. Incoming traffic is
        # either a response from a peer we configured (key registered for the
        # peer's address) or a command addressed to us (our own key).
        key = self._device_keys.get(src)
        if key is None:
            key = self._device_keys.get(self._src)
        if key is None:
            logger.debug("no device key for %#06x, dropping", src)
        return key

    def _deliver(self, msg: ReceivedMessage) -> None:
        self.received.append(msg)
        # Mesh has no correlation ID: resolve only the OLDEST matching waiter
        # so N concurrent same-(dst, opcode) requests pair FIFO with N responses.
        for dst, opcode, fut in self._waiters:
            if msg.src == dst and msg.opcode == opcode and not fut.done():
                fut.set_result(msg)
                break
        if self.on_message is not None:
            try:
                self.on_message(msg)
            except Exception:  # never let a user handler break the bearer RX path
                logger.exception("on_message callback raised")

    # -------------------------------------------------------------- request

    async def request(
        self,
        dst: int,
        payload: bytes,
        expected_opcode: int,
        *,
        dev_key: bool = False,
        ttl: int = DEFAULT_TTL,
        timeout: float = 5.0,
        retries: int = 1,
    ) -> ReceivedMessage:
        """Send ``payload`` and await the matching response from ``dst``.

        A response matches when its source is ``dst`` and its opcode equals
        ``expected_opcode``. Mesh access messages carry no correlation ID, so
        concurrent requests with the same ``(dst, expected_opcode)`` are
        matched to responses in FIFO order; callers that need strict
        request/response pairing must serialize such requests themselves.
        On timeout the request is retransmitted up to ``retries`` times;
        :class:`TimeoutError` is raised when all attempts are exhausted.
        """
        if retries < 0:
            raise NodeError(f"retries must be >= 0, got {retries}")
        fut: asyncio.Future[ReceivedMessage] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiters.append((dst, expected_opcode, fut))
        try:
            for _ in range(retries + 1):
                self.send_access(dst, payload, dev_key=dev_key, ttl=ttl)
                if fut.done():  # resolved synchronously during send
                    return fut.result()
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), timeout)
                except TimeoutError:
                    continue
            if fut.done():  # response landed in the post-timeout race window
                return fut.result()
            raise TimeoutError(
                f"no {expected_opcode:#06x} response from {dst:#06x} "
                f"after {retries + 1} attempts"
            )
        finally:
            self._waiters = [w for w in self._waiters if w[2] is not fut]
