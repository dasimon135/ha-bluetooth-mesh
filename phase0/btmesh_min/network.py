"""Network layer: encryption, obfuscation and PDU codec (Mesh Profile spec §3.4.4-§3.4.6).

Only master (flooding) security credentials are supported (k2 with P=0x00);
no relay, no IV Update handling — the caller owns a single fixed IV Index.
"""

from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag

from .crypto import aes_ecb, ccm_decrypt, ccm_encrypt, k2
from .errors import BtMeshError

# byte 0 (IVI|NID) + 6 obfuscated header bytes + DST(2) + >=1 byte transport
# + 32-bit NetMIC is the shortest possible network PDU.
_MIN_PDU_LEN = 14


class NetworkError(BtMeshError):
    """A network PDU could not be encoded or decoded."""


@dataclass(frozen=True)
class DecodedNetworkPDU:
    """Fields recovered from an incoming network PDU."""

    ctl: bool
    ttl: int
    seq: int
    src: int
    dst: int
    transport_pdu: bytes


@dataclass
class NetworkContext:
    """Security material and TX sequence state for one subnet.

    ``seq`` is the next sequence number to use; the caller persists it across
    restarts (reusing a SEQ with the same IV Index breaks nonce uniqueness).

    ``net_key`` must not be reassigned after construction: the derived
    NID/EncryptionKey/PrivacyKey are computed once in ``__post_init__``.
    Build a new context for a new key.
    """

    net_key: bytes
    iv_index: int
    seq: int = 0
    nid: int = field(init=False)
    encryption_key: bytes = field(init=False)
    privacy_key: bytes = field(init=False)

    def __post_init__(self) -> None:
        self.nid, self.encryption_key, self.privacy_key = k2(self.net_key, b"\x00")

    def next_seq(self, count: int = 1) -> int:
        """Atomically allocate ``count`` consecutive sequence numbers.

        Returns the first SEQ of the block and advances the counter past it.
        Segmented sends allocate one block (one SEQ per segment) instead of
        doing manual ``seq`` bookkeeping.
        """
        if count < 1:
            raise NetworkError(f"count must be >= 1, got {count}")
        if self.seq + count - 1 > 0xFFFFFF:
            raise NetworkError("SEQ space exhausted (24-bit overflow)")
        seq = self.seq
        self.seq += count
        return seq


def _network_nonce(ctl: bool, ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    """Network nonce (spec §3.8.5.1): 00 || CTL/TTL || SEQ || SRC || 0000 || IV Index."""
    return (
        bytes([0x00, (ctl << 7) | ttl])
        + seq.to_bytes(3, "big")
        + src.to_bytes(2, "big")
        + b"\x00\x00"
        + iv_index.to_bytes(4, "big")
    )


def _pecb(privacy_key: bytes, iv_index: int, privacy_random: bytes) -> bytes:
    """Obfuscation keystream (spec §3.8.7.3): e(PrivacyKey, 0^40 || IV Index || Privacy Random)."""
    plain = bytes(5) + iv_index.to_bytes(4, "big") + privacy_random
    return aes_ecb(privacy_key, plain)[:6]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def encode(
    ctx: NetworkContext,
    *,
    ctl: bool,
    ttl: int,
    seq: int,
    src: int,
    dst: int,
    transport_pdu: bytes,
) -> bytes:
    """Build an on-air network PDU (spec §3.4.4, encrypted per §3.4.6.3)."""
    if not 0 <= ttl <= 0x7F:
        raise NetworkError(f"TTL out of range: {ttl}")
    if not 0 <= seq <= 0xFFFFFF:
        raise NetworkError(f"SEQ out of range: {seq:#x}")
    if not 0 <= src <= 0xFFFF:
        raise NetworkError(f"SRC out of range: {src:#x}")
    if not 0 <= dst <= 0xFFFF:
        raise NetworkError(f"DST out of range: {dst:#x}")
    if not transport_pdu:
        raise NetworkError("empty transport PDU")

    ivi = ctx.iv_index & 1
    header = bytes([(ctl << 7) | ttl]) + seq.to_bytes(3, "big") + src.to_bytes(2, "big")
    nonce = _network_nonce(ctl, ttl, seq, src, ctx.iv_index)
    mic_len = 8 if ctl else 4
    encrypted = ccm_encrypt(
        ctx.encryption_key, nonce, dst.to_bytes(2, "big") + transport_pdu, mic_len
    )
    obfuscated = _xor(header, _pecb(ctx.privacy_key, ctx.iv_index, encrypted[:7]))
    return bytes([(ivi << 7) | ctx.nid]) + obfuscated + encrypted


def decode(ctx: NetworkContext, raw: bytes) -> DecodedNetworkPDU:
    """Deobfuscate and decrypt an incoming network PDU (spec §3.4.6.4).

    Raises :class:`NetworkError` on NID/IVI mismatch, truncation, or bad NetMIC.
    """
    if len(raw) < _MIN_PDU_LEN:
        raise NetworkError(f"network PDU too short: {len(raw)} bytes")
    if raw[0] & 0x7F != ctx.nid:
        raise NetworkError(f"NID mismatch: {raw[0] & 0x7F:#04x} != {ctx.nid:#04x}")
    if raw[0] >> 7 != ctx.iv_index & 1:
        raise NetworkError("IVI does not match the current IV Index")

    encrypted = raw[7:]
    header = _xor(raw[1:7], _pecb(ctx.privacy_key, ctx.iv_index, encrypted[:7]))
    ctl = bool(header[0] >> 7)
    ttl = header[0] & 0x7F
    seq = int.from_bytes(header[1:4], "big")
    src = int.from_bytes(header[4:6], "big")

    nonce = _network_nonce(ctl, ttl, seq, src, ctx.iv_index)
    mic_len = 8 if ctl else 4
    try:
        plaintext = ccm_decrypt(ctx.encryption_key, nonce, encrypted, mic_len)
    except (InvalidTag, ValueError) as exc:
        raise NetworkError("network PDU authentication failed") from exc
    if len(plaintext) < 3:
        raise NetworkError("network PDU has no transport payload")
    return DecodedNetworkPDU(
        ctl=ctl,
        ttl=ttl,
        seq=seq,
        src=src,
        dst=int.from_bytes(plaintext[:2], "big"),
        transport_pdu=plaintext[2:],
    )
