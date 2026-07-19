"""Tests for btmesh.network against Mesh Profile 1.0.1 §8.3 message samples.

Vector sources (cross-checked, two independent implementations):
- BlueZ unit/test-mesh-crypto.c (structs s8_3_1, s8_3_6, s8_3_22)
  https://github.com/bluez/bluez/blob/master/unit/test-mesh-crypto.c
- btstack test/mesh/mesh_message_test.py (message #1 network PDU, PECB)
  https://github.com/bluekitchen/btstack/blob/master/test/mesh/mesh_message_test.py
"""

import pytest

from btmesh.crypto import aes_ecb
from btmesh.errors import BtMeshError
from btmesh.network import NetworkContext, NetworkError, decode, encode

# §8.3 sample security material (same NetKey for all message samples).
NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
IV_INDEX = 0x12345678

# §8.3.1 Message #1 (CTL=1 Friend Request, unsegmented control message).
MSG1_TRANSPORT = bytes.fromhex("034b50057e400000010000")
MSG1_PACKET = bytes.fromhex(
    "68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df"
)

# §8.3.6 Message #6 (segmented Config AppKey Add, two network PDUs).
MSG6_SEGMENTS = [
    (0x3129AB, bytes.fromhex("8026ac01ee9dddfd2169326d23f3afdf")),
    (0x3129AC, bytes.fromhex("8026ac21cfdc18c52fdef772e0e17308")),
]
MSG6_PACKETS = [
    bytes.fromhex("68cab5c5348a230afba8c63d4e686364979deaf4fd40961145939cda0e"),
    bytes.fromhex("681615b5dd4a846cae0c032bf0746f44f1b8cc8ce5edc57e55beed49c0"),
]

# §8.3.22 Message #22 (access message, IV Index with LSB=1 → IVI bit set).
MSG22_IV_INDEX = 0x12345677
MSG22_TRANSPORT = bytes.fromhex("663871b904d431526316ca48a0")
MSG22_PACKET = bytes.fromhex(
    "e8d85caecef1e3ed31f3fdcf88a411135fea55df730b6b28e255"
)


def ctx(iv_index: int = IV_INDEX) -> NetworkContext:
    return NetworkContext(net_key=NET_KEY, iv_index=iv_index)


def test_context_derives_master_credentials():
    """§8.3.1: k2(NetKey, 0x00) → NID / EncryptionKey / PrivacyKey."""
    c = ctx()
    assert c.nid == 0x68
    assert c.encryption_key == bytes.fromhex("0953fa93e7caac9638f58820220a398e")
    assert c.privacy_key == bytes.fromhex("8b84eedec100067d670971dd2aa700cf")


def test_next_seq_is_persistable_counter():
    c = NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=5)
    assert c.next_seq() == 5
    assert c.next_seq() == 6
    assert c.seq == 7  # caller can persist the next value to use


def test_next_seq_block_allocation():
    """A segmented send allocates one atomic block of consecutive SEQs."""
    c = NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0x100)
    assert c.next_seq(2) == 0x100
    assert c.seq == 0x102
    assert c.next_seq() == 0x102


def test_next_seq_overflow_raises():
    c = NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0xFFFFFF)
    assert c.next_seq() == 0xFFFFFF  # last valid SEQ is still usable
    with pytest.raises(NetworkError):
        c.next_seq()
    c2 = NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0xFFFFFF)
    with pytest.raises(NetworkError):
        c2.next_seq(2)


def test_next_seq_bad_count_raises():
    with pytest.raises(NetworkError):
        ctx().next_seq(0)


def test_encode_src_dst_out_of_range_raise():
    for kwargs in ({"src": 0x10000, "dst": 0x0002}, {"src": 0x0001, "dst": -1}):
        with pytest.raises(NetworkError):
            encode(
                ctx(), ctl=False, ttl=0, seq=0, transport_pdu=b"\x66\x00",
                **kwargs,
            )


def test_aes_ecb_pecb_vector():
    """btstack message #1 PECB: e(PrivacyKey, 0^40 || IV Index || Privacy Random)."""
    priv_key = bytes.fromhex("8b84eedec100067d670971dd2aa700cf")
    plain = bytes.fromhex("000000000012345678b5e5bfdacbaf6c")
    assert aes_ecb(priv_key, plain)[:6] == bytes.fromhex("6ca487507564")


def test_encode_message1_control():
    """§8.3.1 Message #1: CTL=1, TTL=0, 64-bit NetMIC."""
    raw = encode(
        ctx(),
        ctl=True,
        ttl=0,
        seq=0x000001,
        src=0x1201,
        dst=0xFFFD,
        transport_pdu=MSG1_TRANSPORT,
    )
    assert raw == MSG1_PACKET


def test_decode_message1_control():
    pdu = decode(ctx(), MSG1_PACKET)
    assert pdu.ctl is True
    assert pdu.ttl == 0
    assert pdu.seq == 0x000001
    assert pdu.src == 0x1201
    assert pdu.dst == 0xFFFD
    assert pdu.transport_pdu == MSG1_TRANSPORT


def test_encode_message6_segments():
    """§8.3.6 Message #6: each segment encoded with its own network SEQ."""
    c = ctx()
    for (seq, lower_pdu), expected in zip(MSG6_SEGMENTS, MSG6_PACKETS):
        raw = encode(
            c, ctl=False, ttl=4, seq=seq, src=0x0003, dst=0x1201,
            transport_pdu=lower_pdu,
        )
        assert raw == expected


def test_decode_message6_segments():
    c = ctx()
    for (seq, lower_pdu), packet in zip(MSG6_SEGMENTS, MSG6_PACKETS):
        pdu = decode(c, packet)
        assert pdu.ctl is False
        assert pdu.ttl == 4
        assert pdu.seq == seq
        assert pdu.src == 0x0003
        assert pdu.dst == 0x1201
        assert pdu.transport_pdu == lower_pdu


def test_encode_message22_ivi_bit():
    """§8.3.22 Message #22: IV Index 0x12345677 (LSB=1) sets the IVI bit."""
    raw = encode(
        ctx(MSG22_IV_INDEX),
        ctl=False,
        ttl=3,
        seq=0x07080B,
        src=0x1234,
        dst=0xB529,
        transport_pdu=MSG22_TRANSPORT,
    )
    assert raw == MSG22_PACKET
    assert raw[0] == 0xE8  # IVI=1 | NID=0x68


def test_decode_message22():
    pdu = decode(ctx(MSG22_IV_INDEX), MSG22_PACKET)
    assert pdu.ctl is False
    assert pdu.ttl == 3
    assert pdu.seq == 0x07080B
    assert pdu.src == 0x1234
    assert pdu.dst == 0xB529
    assert pdu.transport_pdu == MSG22_TRANSPORT


def test_decode_wrong_nid_raises():
    raw = bytearray(MSG1_PACKET)
    raw[0] = (raw[0] & 0x80) | ((raw[0] + 1) & 0x7F)  # flip NID, keep IVI
    with pytest.raises(NetworkError):
        decode(ctx(), bytes(raw))


def test_decode_wrong_ivi_raises():
    raw = bytearray(MSG1_PACKET)
    raw[0] ^= 0x80  # flip IVI, keep NID
    with pytest.raises(NetworkError):
        decode(ctx(), bytes(raw))


def test_decode_bad_mic_raises():
    raw = bytearray(MSG1_PACKET)
    raw[-1] ^= 0xFF
    with pytest.raises(NetworkError):
        decode(ctx(), bytes(raw))


def test_decode_too_short_raises():
    with pytest.raises(NetworkError):
        decode(ctx(), MSG1_PACKET[:10])


def test_network_error_is_btmesh_error():
    assert issubclass(NetworkError, BtMeshError)


def test_roundtrip_with_next_seq():
    tx = NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0x1000)
    rx = NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX)
    raw = encode(
        tx, ctl=False, ttl=5, seq=tx.next_seq(), src=0x0001, dst=0x0002,
        transport_pdu=bytes.fromhex("6600112233"),
    )
    pdu = decode(rx, raw)
    assert pdu.seq == 0x1000
    assert pdu.transport_pdu == bytes.fromhex("6600112233")
