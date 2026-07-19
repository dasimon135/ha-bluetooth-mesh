"""Tests for btmesh.proxy_pdu (Mesh Profile spec §6.3 Proxy PDU SAR).

Layout source: Mesh Profile 1.0.1 §6.3.1 — first octet = SAR (2 high bits)
| message type (6 low bits); SAR 0b00 complete, 0b01 first, 0b10
continuation, 0b11 last.  Cross-checked against Zephyr
subsys/bluetooth/mesh/proxy_msg.c.
"""

import pytest

from btmesh.proxy_pdu import (
    MSG_TYPE_MESH_BEACON,
    MSG_TYPE_NETWORK_PDU,
    MSG_TYPE_PROVISIONING_PDU,
    MSG_TYPE_PROXY_CONFIG,
    ProxyPDUError,
    Reassembler,
    segment,
)

MTU = 20  # typical usable ATT payload; each frame carries mtu-1 payload bytes


# ---------------------------------------------------------------------------
# segment()
# ---------------------------------------------------------------------------


def test_message_type_values():
    assert MSG_TYPE_NETWORK_PDU == 0x00
    assert MSG_TYPE_MESH_BEACON == 0x01
    assert MSG_TYPE_PROXY_CONFIG == 0x02
    assert MSG_TYPE_PROVISIONING_PDU == 0x03


def test_segment_payload_smaller_than_mtu_is_complete():
    payload = bytes(range(5))
    frames = segment(MSG_TYPE_PROVISIONING_PDU, payload, mtu=MTU)
    assert frames == [bytes([0b00_000000 | 0x03]) + payload]


def test_segment_payload_exactly_mtu_minus_1_is_complete():
    payload = bytes(MTU - 1)
    frames = segment(MSG_TYPE_NETWORK_PDU, payload, mtu=MTU)
    assert len(frames) == 1
    assert frames[0][0] == 0b00_000000  # SAR complete, type 0x00
    assert frames[0][1:] == payload
    assert len(frames[0]) == MTU


def test_segment_two_frames():
    payload = bytes(range(MTU))  # mtu bytes -> 19 + 1
    frames = segment(MSG_TYPE_PROVISIONING_PDU, payload, mtu=MTU)
    assert len(frames) == 2
    assert frames[0][0] == 0b01_000000 | 0x03  # first
    assert frames[1][0] == 0b11_000000 | 0x03  # last
    assert frames[0][1:] == payload[: MTU - 1]
    assert frames[1][1:] == payload[MTU - 1 :]
    assert all(len(f) <= MTU for f in frames)


def test_segment_three_frames_has_continuation():
    payload = bytes(2 * (MTU - 1) + 3)
    frames = segment(MSG_TYPE_PROXY_CONFIG, payload, mtu=MTU)
    assert len(frames) == 3
    assert frames[0][0] == 0b01_000000 | 0x02  # first
    assert frames[1][0] == 0b10_000000 | 0x02  # continuation
    assert frames[2][0] == 0b11_000000 | 0x02  # last
    assert b"".join(f[1:] for f in frames) == payload


def test_segment_empty_payload_single_frame():
    frames = segment(MSG_TYPE_MESH_BEACON, b"", mtu=MTU)
    assert frames == [bytes([0x01])]


def test_segment_rejects_bad_args():
    with pytest.raises(ProxyPDUError):
        segment(0x40, b"x", mtu=MTU)  # msg_type wider than 6 bits
    with pytest.raises(ProxyPDUError):
        segment(MSG_TYPE_NETWORK_PDU, b"x", mtu=1)  # no room for payload


# ---------------------------------------------------------------------------
# Reassembler + round trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    [0, 1, MTU - 2, MTU - 1, MTU, 2 * (MTU - 1), 2 * (MTU - 1) + 1, 100],
)
def test_segment_reassemble_round_trip(size):
    payload = bytes(i & 0xFF for i in range(size))
    reassembler = Reassembler()
    result = None
    for frame in segment(MSG_TYPE_PROVISIONING_PDU, payload, mtu=MTU):
        assert result is None  # must not complete before the last frame
        result = reassembler.feed(frame)
    assert result == (MSG_TYPE_PROVISIONING_PDU, payload)


def test_reassemble_interleaved_sizes_sequentially():
    """Messages of different sizes and types through one reassembler."""
    reassembler = Reassembler()
    cases = [
        (MSG_TYPE_PROVISIONING_PDU, bytes(65)),  # 4 frames at mtu 20
        (MSG_TYPE_NETWORK_PDU, b"\x01\x02"),  # complete frame
        (MSG_TYPE_PROXY_CONFIG, bytes(range(38))),  # exactly 2 frames
    ]
    for msg_type, payload in cases:
        result = None
        for frame in segment(msg_type, payload, mtu=MTU):
            assert result is None
            result = reassembler.feed(frame)
        assert result == (msg_type, payload)


def test_incomplete_message_returns_none():
    frames = segment(MSG_TYPE_NETWORK_PDU, bytes(50), mtu=MTU)
    reassembler = Reassembler()
    assert reassembler.feed(frames[0]) is None
    assert reassembler.feed(frames[1]) is None


# ---------------------------------------------------------------------------
# Error cases (raise ProxyPDUError and reset state)
# ---------------------------------------------------------------------------


def test_continuation_without_first_raises():
    reassembler = Reassembler()
    with pytest.raises(ProxyPDUError):
        reassembler.feed(bytes([0b10_000000 | 0x03]) + b"abc")


def test_last_without_first_raises():
    reassembler = Reassembler()
    with pytest.raises(ProxyPDUError):
        reassembler.feed(bytes([0b11_000000 | 0x03]) + b"abc")


def test_msg_type_change_mid_message_raises_and_resets():
    frames = segment(MSG_TYPE_PROVISIONING_PDU, bytes(50), mtu=MTU)
    reassembler = Reassembler()
    reassembler.feed(frames[0])
    bad = bytes([0b10_000000 | MSG_TYPE_NETWORK_PDU]) + b"xyz"
    with pytest.raises(ProxyPDUError):
        reassembler.feed(bad)
    # state was reset: a complete frame now goes through cleanly
    assert reassembler.feed(bytes([0x00]) + b"ok") == (MSG_TYPE_NETWORK_PDU, b"ok")


def test_complete_frame_mid_reassembly_raises_and_resets():
    frames = segment(MSG_TYPE_PROVISIONING_PDU, bytes(50), mtu=MTU)
    reassembler = Reassembler()
    reassembler.feed(frames[0])
    with pytest.raises(ProxyPDUError):
        reassembler.feed(bytes([0x03]) + b"oops")
    # state was reset: feeding a fresh full sequence works
    result = None
    for frame in segment(MSG_TYPE_PROVISIONING_PDU, b"hello", mtu=MTU):
        result = reassembler.feed(frame)
    assert result == (MSG_TYPE_PROVISIONING_PDU, b"hello")


def test_first_frame_mid_reassembly_raises_and_resets():
    frames = segment(MSG_TYPE_PROVISIONING_PDU, bytes(50), mtu=MTU)
    reassembler = Reassembler()
    reassembler.feed(frames[0])
    with pytest.raises(ProxyPDUError):
        reassembler.feed(frames[0])  # a second "first" segment


def test_empty_frame_raises():
    reassembler = Reassembler()
    with pytest.raises(ProxyPDUError):
        reassembler.feed(b"")
