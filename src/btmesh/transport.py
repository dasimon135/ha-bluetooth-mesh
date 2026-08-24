"""Upper and lower transport layers (Mesh Profile spec §3.5, §3.6).

Scope: access messages with 32-bit TransMIC (SZMIC=0) over app or device keys;
segmentation for TX and reassembly for RX. Of the control messages, Segment
Acknowledgment is both parsed and built (:func:`build_segment_ack`); the rest
are parse-only. No friendship, no SZMIC=1.
"""

from typing import NamedTuple

from cryptography.exceptions import InvalidTag

from .crypto import ccm_decrypt, ccm_encrypt
from .errors import BtMeshError

__all__ = [
    "SEGMENT_SIZE",
    "UNSEG_MAX_UPPER_LEN",
    "TRANS_MIC_LEN",
    "MAX_SEGMENTS",
    "TransportError",
    "UnsegmentedAccess",
    "AccessSegment",
    "SegmentAck",
    "UnknownControl",
    "SegmentAssembler",
    "encrypt_access",
    "decrypt_access",
    "build_unsegmented_access",
    "build_segment_ack",
    "segment_access_message",
    "parse_access_lower",
    "parse_control_lower",
]

SEGMENT_SIZE = 12  # bytes of upper-transport PDU per segment (spec §3.5.2.2)
UNSEG_MAX_UPPER_LEN = 15  # max upper-transport PDU in an unsegmented message
TRANS_MIC_LEN = 4  # SZMIC=0 → 32-bit TransMIC
MAX_SEGMENTS = 32  # SegN is 5 bits

_APP_NONCE_TYPE = 0x01
_DEVICE_NONCE_TYPE = 0x02
_SEG_ACK_OPCODE = 0x00


class TransportError(BtMeshError):
    """A transport PDU could not be built or parsed."""


class UnsegmentedAccess(NamedTuple):
    """Parsed unsegmented access message (spec §3.5.2.1)."""

    akf: bool
    aid: int
    upper_pdu: bytes


class AccessSegment(NamedTuple):
    """Parsed segment of a segmented access message (spec §3.5.2.2)."""

    akf: bool
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    segment: bytes


class SegmentAck(NamedTuple):
    """Parsed Segment Acknowledgment control message (spec §3.5.3.3)."""

    obo: bool
    seq_zero: int
    block_ack: int


class UnknownControl(NamedTuple):
    """Unsegmented control message we do not interpret (raw parameters)."""

    opcode: int
    parameters: bytes


# --------------------------------------------------------------- upper layer


def _access_nonce(
    akf: bool, seq: int, src: int, dst: int, iv_index: int
) -> bytes:
    """Application or device nonce (spec §3.8.5.2 / §3.8.5.3), SZMIC=0."""
    return (
        bytes([_APP_NONCE_TYPE if akf else _DEVICE_NONCE_TYPE, 0x00])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + dst.to_bytes(2, "big")
        + iv_index.to_bytes(4, "big")
    )


def _check_label_uuid(label_uuid: bytes) -> None:
    if label_uuid and len(label_uuid) != 16:
        raise TransportError(
            f"Label UUID must be 16 bytes, got {len(label_uuid)}"
        )


def encrypt_access(
    key: bytes,
    *,
    akf: bool,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    access_pdu: bytes,
    label_uuid: bytes = b"",
) -> bytes:
    """Encrypt an access PDU into an upper-transport PDU (spec §3.6.2.1).

    ``key`` is the AppKey when ``akf`` is True, else the DeviceKey. ``seq`` is
    the SEQ of the (first) network PDU carrying this message. For virtual
    destination addresses pass the Label UUID (CCM additional data).
    """
    _check_label_uuid(label_uuid)
    nonce = _access_nonce(akf, seq, src, dst, iv_index)
    return ccm_encrypt(key, nonce, access_pdu, TRANS_MIC_LEN, aad=label_uuid)


def decrypt_access(
    key: bytes,
    *,
    akf: bool,
    seq: int,
    src: int,
    dst: int,
    iv_index: int,
    upper_pdu: bytes,
    label_uuid: bytes = b"",
) -> bytes:
    """Decrypt an upper-transport PDU back to the access PDU (spec §3.6.2.2).

    Raises :class:`TransportError` if the TransMIC does not verify.
    """
    _check_label_uuid(label_uuid)
    nonce = _access_nonce(akf, seq, src, dst, iv_index)
    try:
        return ccm_decrypt(key, nonce, upper_pdu, TRANS_MIC_LEN, aad=label_uuid)
    except (InvalidTag, ValueError) as exc:
        raise TransportError("upper transport authentication failed") from exc


# ------------------------------------------------------- lower layer, access


def _check_aid(akf: bool, aid: int) -> None:
    if not 0 <= aid <= 0x3F:
        raise TransportError(f"AID out of range: {aid}")
    if not akf and aid != 0:
        raise TransportError("AID must be 0 when AKF=0 (device key)")


def build_unsegmented_access(upper_pdu: bytes, *, akf: bool, aid: int) -> bytes:
    """Build an unsegmented access lower-transport PDU (spec §3.5.2.1)."""
    _check_aid(akf, aid)
    if not 1 <= len(upper_pdu) <= UNSEG_MAX_UPPER_LEN:
        raise TransportError(
            f"upper PDU must be 1..{UNSEG_MAX_UPPER_LEN} bytes for an "
            f"unsegmented message, got {len(upper_pdu)}"
        )
    return bytes([(akf << 6) | aid]) + upper_pdu


def segment_access_message(
    upper_pdu: bytes, *, akf: bool, aid: int, first_seq: int
) -> list[tuple[int, bytes]]:
    """Split an upper-transport PDU into segments (spec §3.5.2.2).

    Returns ``[(seq, lower_transport_pdu), ...]``: each segment MUST go on air
    in a network PDU using exactly its own ``seq``. ``first_seq`` is the SEQ of
    the first segment — the same value used in the upper-transport nonce — and
    its 13 LSBs become SeqZero.
    """
    _check_aid(akf, aid)
    if not upper_pdu:
        raise TransportError("empty upper PDU")
    seg_count = (len(upper_pdu) + SEGMENT_SIZE - 1) // SEGMENT_SIZE
    if seg_count > MAX_SEGMENTS:
        raise TransportError(f"upper PDU needs {seg_count} segments (max {MAX_SEGMENTS})")
    if first_seq + seg_count - 1 > 0xFFFFFF:
        raise TransportError("SEQ would overflow 24 bits during segmentation")

    seq_zero = first_seq & 0x1FFF
    seg_n = seg_count - 1
    octet0 = 0x80 | (akf << 6) | aid
    out: list[tuple[int, bytes]] = []
    for seg_o in range(seg_count):
        # SZMIC(1)=0 | SeqZero(13) | SegO(5) | SegN(5), big-endian 24 bits.
        header = (seq_zero << 10) | (seg_o << 5) | seg_n
        chunk = upper_pdu[seg_o * SEGMENT_SIZE : (seg_o + 1) * SEGMENT_SIZE]
        out.append(
            (first_seq + seg_o, bytes([octet0]) + header.to_bytes(3, "big") + chunk)
        )
    return out


def parse_access_lower(pdu: bytes) -> UnsegmentedAccess | AccessSegment:
    """Parse a lower-transport PDU of an access message (CTL=0)."""
    if not pdu:
        raise TransportError("empty lower transport PDU")
    seg = pdu[0] >> 7
    akf = bool((pdu[0] >> 6) & 1)
    aid = pdu[0] & 0x3F
    if not seg:
        if len(pdu) < 2:
            raise TransportError("unsegmented access message has no upper PDU")
        return UnsegmentedAccess(akf=akf, aid=aid, upper_pdu=pdu[1:])
    if len(pdu) < 5:
        raise TransportError("segmented access message truncated")
    header = int.from_bytes(pdu[1:4], "big")
    return AccessSegment(
        akf=akf,
        aid=aid,
        szmic=header >> 23,
        seq_zero=(header >> 10) & 0x1FFF,
        seg_o=(header >> 5) & 0x1F,
        seg_n=header & 0x1F,
        segment=pdu[4:],
    )


class SegmentAssembler:
    """Reassemble one segmented access message at a time (spec §3.5.3.4).

    One instance tracks one transfer: segments belonging to a different message
    (any change in AKF/AID/SZMIC/SeqZero/SegN) silently restart the assembly.
    Peers are kept apart by holding an instance per source address — that is
    :class:`~btmesh.node.MeshNode`'s job, not this class's.

    The transfer identity, the block-ack bitfield and the completion flag stay
    readable after :meth:`add` so the caller can acknowledge (spec §3.5.3.3).
    A completed transfer is *kept*, not discarded: a peer whose ack we lost
    retransmits, and those segments must be re-acknowledged rather than
    reassembled into a duplicate delivery.
    """

    def __init__(self) -> None:
        self._key: tuple[bool, int, int, int, int] | None = None
        self._parts: dict[int, bytes] = {}
        self._complete = False

    @property
    def seq_zero(self) -> int | None:
        """SeqZero of the transfer being tracked, or ``None`` when idle."""
        return None if self._key is None else self._key[3]

    @property
    def block_ack(self) -> int:
        """Bitfield of the segments received so far; bit *n* = SegO *n*."""
        return sum(1 << seg_o for seg_o in self._parts)

    @property
    def complete(self) -> bool:
        """True once the tracked transfer has been delivered."""
        return self._complete

    @property
    def pending(self) -> bool:
        """True while a transfer is partially received."""
        return self._key is not None and not self._complete

    def reset(self) -> None:
        """Drop whatever is tracked (incomplete timer expiry, spec §3.5.3.4)."""
        self._key = None
        self._parts = {}
        self._complete = False

    def add(self, seg: AccessSegment) -> bytes | None:
        """Add one segment; returns the full upper-transport PDU when complete.

        Returns ``None`` both while segments are still missing and for a
        retransmission of an already-delivered transfer — :attr:`complete`
        tells the two apart.
        """
        if seg.seg_o > seg.seg_n:
            raise TransportError(f"SegO {seg.seg_o} > SegN {seg.seg_n}")
        if seg.seg_o < seg.seg_n and len(seg.segment) != SEGMENT_SIZE:
            raise TransportError(
                f"non-final segment must be {SEGMENT_SIZE} bytes, "
                f"got {len(seg.segment)}"
            )
        key = (seg.akf, seg.aid, seg.szmic, seg.seq_zero, seg.seg_n)
        if key != self._key:
            self._key = key
            self._parts = {}
            self._complete = False
        self._parts[seg.seg_o] = seg.segment
        if self._complete:
            return None
        if len(self._parts) < seg.seg_n + 1:
            return None
        self._complete = True
        return b"".join(self._parts[i] for i in range(seg.seg_n + 1))


# ------------------------------------------------------ lower layer, control


def build_segment_ack(
    *, seq_zero: int, block_ack: int, obo: bool = False
) -> bytes:
    """Build a Segment Acknowledgment lower-transport PDU (spec §3.5.3.3).

    ``seq_zero`` identifies the transfer being acknowledged (the 13 LSBs of its
    first segment's SEQ) and ``block_ack`` is the bitfield of segments received
    so far, bit *n* set meaning SegO *n* arrived. ``obo`` ("on behalf of") is
    set only by a Friend answering for a low-power node, which we never are.

    The result travels in a network PDU with ``CTL=1``, addressed to the
    unicast the segments came from.
    """
    if not 0 <= seq_zero <= 0x1FFF:
        raise TransportError(f"SeqZero out of range: {seq_zero}")
    if not 0 <= block_ack <= 0xFFFFFFFF:
        raise TransportError(f"BlockAck out of range: {block_ack}")
    # SEG(1)=0 | Opcode(7)=0x00, then OBO(1) | SeqZero(13) | RFU(2)=0.
    head = (int(obo) << 15) | (seq_zero << 2)
    return (
        bytes([_SEG_ACK_OPCODE])
        + head.to_bytes(2, "big")
        + block_ack.to_bytes(4, "big")
    )


def parse_control_lower(pdu: bytes) -> SegmentAck | UnknownControl:
    """Parse a lower-transport PDU of a control message (CTL=1).

    Only Segment Acknowledgment (opcode 0x00) is interpreted; other
    unsegmented opcodes are returned raw. Segmented control messages are not
    supported in Phase 0.
    """
    if not pdu:
        raise TransportError("empty control PDU")
    if pdu[0] >> 7:
        raise TransportError("segmented control messages are not supported")
    opcode = pdu[0] & 0x7F
    if opcode != _SEG_ACK_OPCODE:
        return UnknownControl(opcode=opcode, parameters=pdu[1:])
    if len(pdu) != 7:
        raise TransportError(
            f"Segment Ack must be 7 bytes, got {len(pdu)}"
        )
    return SegmentAck(
        obo=bool(pdu[1] >> 7),
        seq_zero=(int.from_bytes(pdu[1:3], "big") >> 2) & 0x1FFF,
        block_ack=int.from_bytes(pdu[3:7], "big"),
    )
