"""Tests for btmesh.transport against Mesh Profile 1.0.1 §8.3 message samples.

Vector sources:
- BlueZ unit/test-mesh-crypto.c (structs s8_3_1, s8_3_6, s8_3_7, s8_3_22)
  https://github.com/bluez/bluez/blob/master/unit/test-mesh-crypto.c
- Cross-check of the network-PDU end of message #6 also covered by
  tests/test_network.py (same BlueZ struct + btstack mesh_message_test.py).

Message #22 is addressed to a virtual address, so its TransMIC covers the
Label UUID as CCM additional data — passed via ``label_uuid``.
"""

import pytest

from btmesh import network
from btmesh.crypto import k4
from btmesh.errors import BtMeshError
from btmesh.transport import (
    AccessSegment,
    SegmentAck,
    SegmentAssembler,
    TransportError,
    UnknownControl,
    UnsegmentedAccess,
    build_segment_ack,
    build_unsegmented_access,
    decrypt_access,
    encrypt_access,
    parse_access_lower,
    parse_control_lower,
    segment_access_message,
)

# §8.3 sample security material.
DEV_KEY = bytes.fromhex("9d6dd0e96eb25dc19a40ed9914f8f03f")
APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
IV_INDEX = 0x12345678

# §8.3.6 Message #6: Config AppKey Add (device key, segmented, 2 segments).
MSG6_ACCESS = bytes.fromhex("0056341263964771734fbd76e3b40519d1d94a48")
MSG6_UPPER = bytes.fromhex(
    "ee9dddfd2169326d23f3afdfcfdc18c52fdef772" + "e0e17308"
)
MSG6_FIRST_SEQ = 0x3129AB
MSG6_SEGMENTS = [
    (0x3129AB, bytes.fromhex("8026ac01ee9dddfd2169326d23f3afdf")),
    (0x3129AC, bytes.fromhex("8026ac21cfdc18c52fdef772e0e17308")),
]
MSG6_PACKETS = [
    bytes.fromhex("68cab5c5348a230afba8c63d4e686364979deaf4fd40961145939cda0e"),
    bytes.fromhex("681615b5dd4a846cae0c032bf0746f44f1b8cc8ce5edc57e55beed49c0"),
]

# §8.3.22 Message #22: vendor access message (app key, virtual dst).
MSG22_ACCESS = bytes.fromhex("d50a0048656c6c6f")
MSG22_UPPER = bytes.fromhex("3871b904d4315263" + "16ca48a0")
MSG22_LABEL_UUID = bytes.fromhex("0073e7e4d8b9440faf8415df4c56c0e1")
MSG22_LOWER = bytes.fromhex("663871b904d431526316ca48a0")

# §8.3.7 Message #7: Segment Acknowledgment (OBO=1, SeqZero=0x09AB).
MSG7_LOWER = bytes.fromhex("00a6ac00000002")

# §8.3.1 Message #1: unsegmented control message, opcode 0x03 (Friend Request).
MSG1_LOWER = bytes.fromhex("034b50057e400000010000")


# ---------------------------------------------------------------- upper layer


def test_encrypt_access_device_key_message6():
    upper = encrypt_access(
        DEV_KEY, akf=False, seq=MSG6_FIRST_SEQ, src=0x0003, dst=0x1201,
        iv_index=IV_INDEX, access_pdu=MSG6_ACCESS,
    )
    assert upper == MSG6_UPPER


def test_decrypt_access_device_key_message6():
    access = decrypt_access(
        DEV_KEY, akf=False, seq=MSG6_FIRST_SEQ, src=0x0003, dst=0x1201,
        iv_index=IV_INDEX, upper_pdu=MSG6_UPPER,
    )
    assert access == MSG6_ACCESS


def test_encrypt_access_app_key_message22():
    upper = encrypt_access(
        APP_KEY, akf=True, seq=0x07080B, src=0x1234, dst=0xB529,
        iv_index=0x12345677, access_pdu=MSG22_ACCESS,
        label_uuid=MSG22_LABEL_UUID,
    )
    assert upper == MSG22_UPPER


def test_decrypt_access_app_key_message22():
    access = decrypt_access(
        APP_KEY, akf=True, seq=0x07080B, src=0x1234, dst=0xB529,
        iv_index=0x12345677, upper_pdu=MSG22_UPPER,
        label_uuid=MSG22_LABEL_UUID,
    )
    assert access == MSG22_ACCESS


def test_decrypt_access_bad_mic_raises():
    tampered = bytearray(MSG6_UPPER)
    tampered[-1] ^= 0xFF
    with pytest.raises(TransportError):
        decrypt_access(
            DEV_KEY, akf=False, seq=MSG6_FIRST_SEQ, src=0x0003, dst=0x1201,
            iv_index=IV_INDEX, upper_pdu=bytes(tampered),
        )


def test_transport_error_is_btmesh_error():
    assert issubclass(TransportError, BtMeshError)


def test_bad_label_uuid_length_raises():
    for bad_uuid in (b"\x00", MSG22_LABEL_UUID + b"\x00"):
        with pytest.raises(TransportError):
            encrypt_access(
                APP_KEY, akf=True, seq=0, src=0x0001, dst=0x8000,
                iv_index=IV_INDEX, access_pdu=b"\x00", label_uuid=bad_uuid,
            )
        with pytest.raises(TransportError):
            decrypt_access(
                APP_KEY, akf=True, seq=0, src=0x0001, dst=0x8000,
                iv_index=IV_INDEX, upper_pdu=bytes(5), label_uuid=bad_uuid,
            )


# ------------------------------------------------------- lower layer, access


def test_build_unsegmented_access_message22():
    """§8.3.22: AKF=1, AID=k4(AppKey)=0x26 → header 0x66."""
    aid = k4(APP_KEY)
    assert aid == 0x26
    assert build_unsegmented_access(MSG22_UPPER, akf=True, aid=aid) == MSG22_LOWER


def test_build_unsegmented_access_too_long_raises():
    with pytest.raises(TransportError):
        build_unsegmented_access(bytes(16), akf=False, aid=0)


def test_segment_access_message6():
    """§8.3.6: SeqZero from the FIRST segment's SEQ; per-segment SEQs."""
    segments = segment_access_message(
        MSG6_UPPER, akf=False, aid=0, first_seq=MSG6_FIRST_SEQ
    )
    assert segments == MSG6_SEGMENTS


def test_segment_seq_zero_is_13_lsbs():
    segments = segment_access_message(
        bytes(13), akf=False, aid=0, first_seq=0x3129AB
    )
    header = int.from_bytes(segments[0][1][1:4], "big")
    assert (header >> 10) & 0x1FFF == 0x3129AB & 0x1FFF == 0x09AB


def test_segment_max_segments_exceeded_raises():
    with pytest.raises(TransportError):
        segment_access_message(
            bytes(12 * 32 + 1), akf=False, aid=0, first_seq=0
        )


def test_segment_seq_overflow_raises():
    with pytest.raises(TransportError):
        segment_access_message(
            bytes(13), akf=False, aid=0, first_seq=0xFFFFFF  # needs 2 SEQs
        )


def test_aid_out_of_range_raises():
    with pytest.raises(TransportError):
        build_unsegmented_access(b"\x00", akf=True, aid=0x40)
    with pytest.raises(TransportError):
        segment_access_message(bytes(13), akf=True, aid=0x40, first_seq=0)


def test_nonzero_aid_with_device_key_raises():
    with pytest.raises(TransportError):
        build_unsegmented_access(b"\x00", akf=False, aid=1)
    with pytest.raises(TransportError):
        segment_access_message(bytes(13), akf=False, aid=1, first_seq=0)


def test_parse_unsegmented_access_message22():
    parsed = parse_access_lower(MSG22_LOWER)
    assert parsed == UnsegmentedAccess(akf=True, aid=0x26, upper_pdu=MSG22_UPPER)


def test_parse_segment_message6():
    parsed = parse_access_lower(MSG6_SEGMENTS[1][1])
    assert parsed == AccessSegment(
        akf=False, aid=0, szmic=0, seq_zero=0x09AB, seg_o=1, seg_n=1,
        segment=MSG6_UPPER[12:],
    )


def test_parse_access_lower_truncated_raises():
    with pytest.raises(TransportError):
        parse_access_lower(bytes.fromhex("8026ac"))
    with pytest.raises(TransportError):
        parse_access_lower(b"")


def test_reassemble_message6():
    assembler = SegmentAssembler()
    seg0 = parse_access_lower(MSG6_SEGMENTS[0][1])
    seg1 = parse_access_lower(MSG6_SEGMENTS[1][1])
    assert assembler.add(seg0) is None
    assert assembler.add(seg1) == MSG6_UPPER


def test_reassemble_out_of_order_and_duplicates():
    assembler = SegmentAssembler()
    seg0 = parse_access_lower(MSG6_SEGMENTS[0][1])
    seg1 = parse_access_lower(MSG6_SEGMENTS[1][1])
    assert assembler.add(seg1) is None
    assert assembler.add(seg1) is None  # duplicate is harmless
    assert assembler.add(seg0) == MSG6_UPPER
    # assembler resets after completion
    assert assembler.add(seg1) is None


def test_reassemble_short_non_final_segment_raises():
    assembler = SegmentAssembler()
    short = AccessSegment(
        akf=False, aid=0, szmic=0, seq_zero=0x09AB, seg_o=0, seg_n=1,
        segment=bytes(11),  # non-final segments must be exactly 12 bytes
    )
    with pytest.raises(TransportError):
        assembler.add(short)


def test_reassemble_restarts_on_new_seq_zero():
    assembler = SegmentAssembler()
    seg0 = parse_access_lower(MSG6_SEGMENTS[0][1])
    assert assembler.add(seg0) is None
    other = AccessSegment(
        akf=False, aid=0, szmic=0, seq_zero=0x0001, seg_o=0, seg_n=1,
        segment=bytes(12),
    )
    assert assembler.add(other) is None
    # the partial message #6 state was discarded
    assert assembler.add(parse_access_lower(MSG6_SEGMENTS[1][1])) is None


def test_assembler_tracks_the_block_ack_bitfield():
    """The ack bitfield names the segments that arrived, bit n = SegO n."""
    assembler = SegmentAssembler()
    assert assembler.block_ack == 0
    assert assembler.seq_zero is None

    assembler.add(parse_access_lower(MSG6_SEGMENTS[1][1]))  # SegO=1 first
    assert assembler.block_ack == 0b10
    assert assembler.seq_zero == MSG6_FIRST_SEQ & 0x1FFF
    assert assembler.pending is True

    assembler.add(parse_access_lower(MSG6_SEGMENTS[0][1]))
    assert assembler.block_ack == 0b11
    assert assembler.pending is False


def test_assembler_does_not_redeliver_a_completed_transfer():
    """A retransmission after our ack was lost must be re-acked, not re-delivered."""
    assembler = SegmentAssembler()
    seg0 = parse_access_lower(MSG6_SEGMENTS[0][1])
    seg1 = parse_access_lower(MSG6_SEGMENTS[1][1])
    assembler.add(seg0)
    assert assembler.add(seg1) == MSG6_UPPER

    assert assembler.add(seg1) is None  # same transfer, already delivered
    assert assembler.complete is True
    assert assembler.block_ack == 0b11  # still ackable
    assert assembler.seq_zero == MSG6_FIRST_SEQ & 0x1FFF


def test_assembler_reset_clears_a_partial_transfer():
    """The incomplete timer drops a partial transfer; the next one starts clean."""
    assembler = SegmentAssembler()
    assembler.add(parse_access_lower(MSG6_SEGMENTS[0][1]))
    assembler.reset()
    assert assembler.pending is False
    assert assembler.block_ack == 0
    assert assembler.seq_zero is None
    assert assembler.add(parse_access_lower(MSG6_SEGMENTS[1][1])) is None


# ------------------------------------------------------ lower layer, control


def test_parse_segment_ack_message7():
    """§8.3.7: OBO=1, SeqZero=0x09AB, BlockAck acknowledges segment 1."""
    parsed = parse_control_lower(MSG7_LOWER)
    assert parsed == SegmentAck(obo=True, seq_zero=0x09AB, block_ack=0x00000002)


def test_build_segment_ack_message7():
    """§8.3.7 on the TX side: the same fields must re-encode to the same bytes."""
    assert (
        build_segment_ack(seq_zero=0x09AB, block_ack=0x00000002, obo=True)
        == MSG7_LOWER
    )


def test_build_segment_ack_round_trips_through_the_parser():
    pdu = build_segment_ack(seq_zero=0x1FFF, block_ack=0xFFFFFFFF)
    assert parse_control_lower(pdu) == SegmentAck(
        obo=False, seq_zero=0x1FFF, block_ack=0xFFFFFFFF
    )


def test_build_segment_ack_rejects_out_of_range_fields():
    with pytest.raises(TransportError):
        build_segment_ack(seq_zero=0x2000, block_ack=0)
    with pytest.raises(TransportError):
        build_segment_ack(seq_zero=0, block_ack=0x1_0000_0000)


def test_parse_unknown_control_message1():
    """§8.3.1: Friend Request (opcode 0x03) is returned raw."""
    parsed = parse_control_lower(MSG1_LOWER)
    assert parsed == UnknownControl(
        opcode=0x03, parameters=bytes.fromhex("4b50057e400000010000")
    )


def test_parse_control_segmented_raises():
    with pytest.raises(TransportError):
        parse_control_lower(bytes.fromhex("8000000000"))


def test_parse_control_bad_length_raises():
    with pytest.raises(TransportError):
        parse_control_lower(b"")
    with pytest.raises(TransportError):
        parse_control_lower(bytes.fromhex("00a6ac000000"))  # ack needs 7 bytes


# ------------------------------------------------------------- end to end


def test_end_to_end_message6_on_air():
    """§8.3.6 full TX chain: upper encrypt → segment → network encode."""
    ctx = network.NetworkContext(
        net_key=NET_KEY, iv_index=IV_INDEX, seq=MSG6_FIRST_SEQ
    )
    first_seq = ctx.next_seq(2)  # one atomic block, one SEQ per segment
    assert first_seq == MSG6_FIRST_SEQ
    upper = encrypt_access(
        DEV_KEY, akf=False, seq=first_seq, src=0x0003, dst=0x1201,
        iv_index=ctx.iv_index, access_pdu=MSG6_ACCESS,
    )
    packets = [
        network.encode(
            ctx, ctl=False, ttl=4, seq=seq, src=0x0003, dst=0x1201,
            transport_pdu=lower_pdu,
        )
        for seq, lower_pdu in segment_access_message(
            upper, akf=False, aid=0, first_seq=first_seq
        )
    ]
    assert packets == MSG6_PACKETS
    assert ctx.seq == MSG6_FIRST_SEQ + 2  # 2-segment send advanced SEQ by 2


def test_end_to_end_message6_receive():
    """§8.3.6 full RX chain: network decode → reassemble → upper decrypt."""
    ctx = network.NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX)
    assembler = SegmentAssembler()
    upper = None
    first_seq = None
    for packet in MSG6_PACKETS:
        pdu = network.decode(ctx, packet)
        seg = parse_access_lower(pdu.transport_pdu)
        if seg.seg_o == 0:
            first_seq = pdu.seq  # SeqZero SEQ: nonce uses the first segment's SEQ
        upper = assembler.add(seg)
    access = decrypt_access(
        DEV_KEY, akf=False, seq=first_seq, src=0x0003, dst=0x1201,
        iv_index=IV_INDEX, upper_pdu=upper,
    )
    assert access == MSG6_ACCESS
