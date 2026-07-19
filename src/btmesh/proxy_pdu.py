"""GATT Proxy PDU segmentation and reassembly (Mesh Profile spec §6.3).

Each Proxy PDU frame starts with one octet: SAR in the 2 high bits, message
type in the 6 low bits, so every frame carries at most mtu-1 payload bytes.
Cross-checked against Zephyr subsys/bluetooth/mesh/proxy_msg.c.
"""

from __future__ import annotations

from typing import NoReturn

from .errors import BtMeshError


class ProxyPDUError(BtMeshError):
    """Malformed Proxy PDU frame or invalid SAR sequence."""


# SAR values (spec §6.3.1, Table 6.2).
SAR_COMPLETE = 0b00
SAR_FIRST = 0b01
SAR_CONTINUATION = 0b10
SAR_LAST = 0b11

# Message types (spec §6.3.1, Table 6.3).
MSG_TYPE_NETWORK_PDU = 0x00
MSG_TYPE_MESH_BEACON = 0x01
MSG_TYPE_PROXY_CONFIG = 0x02
MSG_TYPE_PROVISIONING_PDU = 0x03


def _header(sar: int, msg_type: int) -> int:
    return (sar << 6) | msg_type


def segment(msg_type: int, payload: bytes, mtu: int) -> list[bytes]:
    """Split *payload* into Proxy PDU frames of at most *mtu* bytes each."""
    if not 0 <= msg_type <= 0x3F:
        raise ProxyPDUError(f"message type 0x{msg_type:02x} does not fit in 6 bits")
    if mtu < 2:
        raise ProxyPDUError(f"mtu {mtu} leaves no room for payload")
    chunk = mtu - 1
    if len(payload) <= chunk:
        return [bytes([_header(SAR_COMPLETE, msg_type)]) + payload]
    chunks = [payload[i : i + chunk] for i in range(0, len(payload), chunk)]
    frames = []
    for i, part in enumerate(chunks):
        if i == 0:
            sar = SAR_FIRST
        elif i == len(chunks) - 1:
            sar = SAR_LAST
        else:
            sar = SAR_CONTINUATION
        frames.append(bytes([_header(sar, msg_type)]) + part)
    return frames


class Reassembler:
    """Reassemble Proxy PDU frames fed one at a time.

    ``feed`` returns ``(msg_type, payload)`` when a message completes, else
    ``None``.  Invalid SAR sequences raise :class:`ProxyPDUError` and reset
    the reassembly state.
    """

    def __init__(self) -> None:
        self._msg_type: int | None = None
        self._buffer = bytearray()

    def _reset(self) -> None:
        self._msg_type = None
        self._buffer = bytearray()

    def _fail(self, message: str) -> NoReturn:
        self._reset()
        raise ProxyPDUError(message)

    def feed(self, frame: bytes) -> tuple[int, bytes] | None:
        if not frame:
            self._fail("empty proxy PDU frame")
        sar = frame[0] >> 6
        msg_type = frame[0] & 0x3F
        payload = frame[1:]
        in_progress = self._msg_type is not None

        if sar == SAR_COMPLETE:
            if in_progress:
                self._fail("complete frame received mid-reassembly")
            return (msg_type, payload)

        if sar == SAR_FIRST:
            if in_progress:
                self._fail("first segment received mid-reassembly")
            self._msg_type = msg_type
            self._buffer = bytearray(payload)
            return None

        # SAR_CONTINUATION or SAR_LAST
        if not in_progress:
            self._fail("continuation/last segment without a first segment")
        if msg_type != self._msg_type:
            self._fail(
                f"message type changed mid-reassembly "
                f"(0x{self._msg_type:02x} -> 0x{msg_type:02x})"
            )
        self._buffer.extend(payload)
        if sar == SAR_LAST:
            result = (msg_type, bytes(self._buffer))
            self._reset()
            return result
        return None
