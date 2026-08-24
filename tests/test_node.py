"""MeshNode facade tests: loopback pair of nodes wired via callbacks.

Integration-style: real access encoders, real network/transport codecs — no
mocks of our own layers. Node A (0x0001) plays provisioner, node B (0x0002)
plays the configured device; both share NetKey/AppKey, and B's device key is
registered on both sides (B as its own, A as B's peer).
"""

import asyncio

import pytest

from btmesh import network
from btmesh import node as node_module
from btmesh.access import (
    OP_CONFIG_APPKEY_ADD,
    OP_CONFIG_APPKEY_STATUS,
    OP_GENERIC_ONOFF_SET,
    OP_GENERIC_ONOFF_STATUS,
    config_appkey_add,
    generic_onoff_set,
)
from btmesh.crypto import k4
from btmesh.errors import BtMeshError
from btmesh.node import MeshNode, ReceivedMessage
from btmesh.transport import SegmentAck, build_unsegmented_access, encrypt_access

NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")
DEV_KEY_B = bytes.fromhex("9d6dd0e96eb25dc19a40ed9914f8f03f")
IV_INDEX = 0x12345678

ONOFF_STATUS_ON = bytes.fromhex("8204" "01")


def make_pair(a_send_filter=None):
    """Two MeshNodes wired back-to-back through their send callbacks.

    ``a_send_filter(pdu, attempt_no)`` may return False to drop A's PDU.
    """
    nodes: dict[str, MeshNode] = {}
    a_sent: list[bytes] = []

    def a_send(pdu: bytes) -> None:
        a_sent.append(pdu)
        if a_send_filter is None or a_send_filter(pdu, len(a_sent)):
            nodes["b"].handle_network_pdu(pdu)

    def b_send(pdu: bytes) -> None:
        nodes["a"].handle_network_pdu(pdu)

    nodes["a"] = MeshNode(
        netkey=NET_KEY, appkey=APP_KEY, iv_index=IV_INDEX,
        src_addr=0x0001, send_network_pdu=a_send,
    )
    nodes["b"] = MeshNode(
        netkey=NET_KEY, appkey=APP_KEY, iv_index=IV_INDEX,
        src_addr=0x0002, send_network_pdu=b_send, seq=0x1000,
    )
    nodes["a"].add_device(0x0002, DEV_KEY_B)  # A knows B's device key
    nodes["b"].add_device(0x0002, DEV_KEY_B)  # B knows its own device key
    return nodes["a"], nodes["b"], a_sent


# ------------------------------------------------------- proxy configuration


def test_proxy_config_pdu_round_trip():
    """A proxy config message survives the network layer between two nodes.

    Proxy configuration rides in a network PDU with CTL=1 and TTL=0 addressed
    to the unassigned address (spec §6.5) — a different shape from the access
    traffic every other path builds, and one the peer must recover verbatim.
    """
    a, b, sent = make_pair()
    message = bytes([0x00, 0x00])  # Set Filter Type: accept list

    pdu = a.build_proxy_config_pdu(message)
    decoded = network.decode(b.ctx, pdu)

    assert decoded.ctl is True
    assert decoded.ttl == 0
    assert decoded.dst == 0x0000  # unassigned: this is for the proxy itself
    assert decoded.src == 0x0001
    assert b.parse_proxy_config_pdu(pdu) == message
    assert sent == []  # built, not sent: the caller routes it (proxy PDU type)


def test_proxy_config_pdu_consumes_a_seq():
    """It is a real network PDU, so it must burn a SEQ like any other."""
    a, _, _ = make_pair()
    before = a.ctx.seq
    a.build_proxy_config_pdu(bytes([0x00, 0x00]))
    assert a.ctx.seq == before + 1


def test_parse_proxy_config_pdu_ignores_foreign_traffic():
    """A PDU from another network must be dropped, not raised on."""
    foreign = MeshNode(
        netkey=bytes(16), appkey=APP_KEY, iv_index=IV_INDEX,
        src_addr=0x0009, send_network_pdu=lambda pdu: None,
    )
    a, _, _ = make_pair()
    assert a.parse_proxy_config_pdu(
        foreign.build_proxy_config_pdu(bytes([0x00, 0x00]))
    ) is None


def onoff_responder(node: MeshNode):
    """on_message handler answering every OnOff Set with an OnOff Status."""

    def handler(msg: ReceivedMessage) -> None:
        if msg.opcode == OP_GENERIC_ONOFF_SET:
            node.send_access(msg.src, ONOFF_STATUS_ON)

    return handler


# ------------------------------------------------------------- send/receive


def test_unsegmented_round_trip_appkey():
    a, b, _ = make_pair()
    a.send_access(0x0002, generic_onoff_set(True, 0x01))
    assert list(b.received) == [
        ReceivedMessage(src=0x0001, opcode=OP_GENERIC_ONOFF_SET, params=b"\x01\x01")
    ]


def test_segmented_round_trip_device_key():
    """Config AppKey Add (24-byte upper PDU, 2 segments) under B's device key."""
    a, b, a_sent = make_pair()
    payload = config_appkey_add(0x456, 0x123, APP_KEY)
    a.send_access(0x0002, payload, dev_key=True)
    assert len(a_sent) == 2  # segmented into two network PDUs
    assert list(b.received) == [
        ReceivedMessage(
            src=0x0001, opcode=OP_CONFIG_APPKEY_ADD, params=payload[1:]
        )
    ]


def test_on_message_callback_fires():
    a, b, _ = make_pair()
    seen: list[ReceivedMessage] = []
    b.on_message = seen.append
    a.send_access(0x0002, generic_onoff_set(False, 0x02))
    assert seen == [
        ReceivedMessage(src=0x0001, opcode=OP_GENERIC_ONOFF_SET, params=b"\x00\x02")
    ]


def test_send_access_without_device_key_raises():
    a, _, _ = make_pair()
    with pytest.raises(BtMeshError):
        a.send_access(0x0005, b"\x00", dev_key=True)


def test_raising_on_message_does_not_break_rx_path():
    a, b, _ = make_pair()

    def bad_handler(msg: ReceivedMessage) -> None:
        raise RuntimeError("buggy user handler")

    b.on_message = bad_handler
    a.send_access(0x0002, generic_onoff_set(True, 0x0A))  # must not raise
    a.send_access(0x0002, generic_onoff_set(False, 0x0B))
    assert [m.params for m in b.received] == [b"\x01\x0a", b"\x00\x0b"]


# ---------------------------------------------------------- request/response


async def test_request_success():
    a, b, a_sent = make_pair()
    b.on_message = onoff_responder(b)
    msg = await a.request(
        0x0002, generic_onoff_set(True, 0x03), OP_GENERIC_ONOFF_STATUS
    )
    assert msg == ReceivedMessage(
        src=0x0002, opcode=OP_GENERIC_ONOFF_STATUS, params=b"\x01"
    )
    assert len(a_sent) == 1  # no retransmission needed


async def test_request_retransmits_once_on_timeout():
    # Drop A's first network PDU: the request must retransmit and succeed.
    a, b, a_sent = make_pair(a_send_filter=lambda pdu, n: n > 1)
    b.on_message = onoff_responder(b)
    msg = await a.request(
        0x0002, generic_onoff_set(True, 0x04), OP_GENERIC_ONOFF_STATUS,
        timeout=0.05,
    )
    assert msg.opcode == OP_GENERIC_ONOFF_STATUS
    assert len(a_sent) == 2


async def test_request_timeout_after_retries_raises():
    a, _, a_sent = make_pair(a_send_filter=lambda pdu, n: False)  # black hole
    with pytest.raises(TimeoutError):
        await a.request(
            0x0002, generic_onoff_set(True, 0x05), OP_GENERIC_ONOFF_STATUS,
            timeout=0.01, retries=1,
        )
    assert len(a_sent) == 2  # original + one retransmission
    # White-box cleanup check: the internal waiter list must not leak entries.
    assert not a._waiters


async def test_concurrent_same_opcode_requests_matched_fifo():
    """No correlation ID in mesh: two same-(dst, opcode) requests pair FIFO."""
    a, b, _ = make_pair()
    t1 = asyncio.create_task(
        a.request(0x0002, generic_onoff_set(True, 0x10),
                  OP_GENERIC_ONOFF_STATUS, timeout=1.0)
    )
    t2 = asyncio.create_task(
        a.request(0x0002, generic_onoff_set(True, 0x11),
                  OP_GENERIC_ONOFF_STATUS, timeout=1.0)
    )
    await asyncio.sleep(0)  # let both tasks send and register their waiters
    b.send_access(0x0001, bytes.fromhex("8204" "00"))  # first response: off
    b.send_access(0x0001, bytes.fromhex("8204" "01"))  # second response: on
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.params == b"\x00"  # oldest waiter got the first response
    assert r2.params == b"\x01"


async def test_request_cancellation_cleans_up_waiter():
    a, _, _ = make_pair(a_send_filter=lambda pdu, n: False)  # black hole
    task = asyncio.create_task(
        a.request(0x0002, generic_onoff_set(True, 0x12),
                  OP_GENERIC_ONOFF_STATUS, timeout=5.0)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # White-box cleanup check: cancellation must not leak the waiter.
    assert not a._waiters


async def test_request_with_device_key():
    """Segmented Config AppKey Add request answered under B's device key."""
    a, b, a_sent = make_pair()

    def handler(msg: ReceivedMessage) -> None:
        if msg.opcode == OP_CONFIG_APPKEY_ADD:
            # B answers with its own device key (AKF=0, own-address fallback).
            b.send_access(msg.src, bytes.fromhex("8003" "00" "563412"),
                          dev_key=True)

    b.on_message = handler
    msg = await a.request(
        0x0002, config_appkey_add(0x456, 0x123, APP_KEY),
        OP_CONFIG_APPKEY_STATUS, dev_key=True,
    )
    assert msg == ReceivedMessage(
        src=0x0002, opcode=OP_CONFIG_APPKEY_STATUS, params=bytes.fromhex("00563412")
    )
    assert len(a_sent) == 2  # the request itself went out as two segments


# ------------------------------------------------------------------ control


def test_completed_segmented_message_is_acknowledged():
    """A peer that segments its reply waits to be acked before it moves on (#9).

    Config AppKey Add is 20 bytes of access payload — two segments — so this is
    the smallest real exchange that exercises the SAR handshake end to end.
    """
    a, b, a_sent = make_pair()

    a.send_access(0x0002, config_appkey_add(0x456, 0x123, APP_KEY), dev_key=True)

    assert len(a_sent) == 2  # went out segmented
    assert len(b.received) == 1  # and B reassembled it
    # A's SEQ started at 0, so the transfer's SeqZero is 0 and both segments
    # are acknowledged.
    assert a.last_ack(0) == SegmentAck(obo=False, seq_zero=0, block_ack=0b11)


def test_segments_to_a_group_address_are_not_acknowledged():
    """Only unicast-addressed transfers are acked (spec §3.5.3.3).

    A group message reaches every subscriber; acking it would have all of them
    answer the sender at once.
    """
    a, b, a_sent = make_pair()
    payload = bytes.fromhex("8204") + bytes(20)  # 22 bytes → three segments

    a.send_access(0xC000, payload)

    assert len(a_sent) == 3
    assert len(b.received) == 1  # still reassembled and delivered
    assert a.last_ack(0) is None  # but answered by nobody


def _fast_sar_timers(monkeypatch, *, incomplete=1.0):
    """Compress the §3.5.3.3 timers so the real ones can be exercised in a test."""
    monkeypatch.setattr(node_module, "SEG_ACK_TIMEOUT_BASE", 0.01)
    monkeypatch.setattr(node_module, "SEG_ACK_TIMEOUT_PER_TTL", 0.0)
    monkeypatch.setattr(node_module, "SEG_INCOMPLETE_TIMEOUT", incomplete)


async def test_ack_timer_reports_which_segments_are_missing(monkeypatch):
    """A gap must not go silent (#9).

    The peer needs to be told what arrived, or it cannot know which segment to
    retransmit — it just times out as if we had never heard it.
    """
    _fast_sar_timers(monkeypatch)
    a, b, a_sent = make_pair(a_send_filter=lambda pdu, n: n != 2)  # drop SegO=1

    a.send_access(0x0002, config_appkey_add(0x456, 0x123, APP_KEY), dev_key=True)

    assert not b.received  # incomplete, so nothing delivered
    assert a.last_ack(0) is None  # and nothing acked yet either

    await asyncio.sleep(0.05)

    assert a.last_ack(0) == SegmentAck(obo=False, seq_zero=0, block_ack=0b01)
    b.close()  # the incomplete timer is still armed, as the spec requires


async def test_incomplete_timer_abandons_a_stranded_transfer(monkeypatch):
    """A half-received transfer must not linger and complete minutes later."""
    _fast_sar_timers(monkeypatch, incomplete=0.03)
    a, b, a_sent = make_pair(a_send_filter=lambda pdu, n: n != 2)  # drop SegO=1
    a.send_access(0x0002, config_appkey_add(0x456, 0x123, APP_KEY), dev_key=True)

    await asyncio.sleep(0.1)
    b.handle_network_pdu(a_sent[1])  # the missing segment, far too late

    assert not b.received  # it starts a new transfer instead of finishing the old
    b.close()  # that late segment armed a fresh pair of timers


async def test_completing_a_transfer_stops_its_timers(monkeypatch):
    """Once acked on completion, nothing further may be emitted for the transfer."""
    _fast_sar_timers(monkeypatch)
    a, b, _ = make_pair()
    a.send_access(0x0002, config_appkey_add(0x456, 0x123, APP_KEY), dev_key=True)
    seq_after_ack = b.ctx.seq

    await asyncio.sleep(0.05)

    assert b.ctx.seq == seq_after_ack  # no second ack burned a SEQ


async def test_close_cancels_a_pending_ack_timer(monkeypatch):
    """A torn-down node must not put an ack on air through a stopped bearer."""
    _fast_sar_timers(monkeypatch)
    a, b, _ = make_pair(a_send_filter=lambda pdu, n: n != 2)  # drop SegO=1
    a.send_access(0x0002, config_appkey_add(0x456, 0x123, APP_KEY), dev_key=True)

    b.close()
    await asyncio.sleep(0.05)

    assert a.last_ack(0) is None


def test_segment_ack_is_stored_and_queryable():
    a, _, _ = make_pair()
    ctx = network.NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0x500)
    ack = (
        bytes([0x00])  # unsegmented control, opcode 0x00 = Segment Ack
        + (0x09AB << 2).to_bytes(2, "big")  # OBO=0 | SeqZero | RFU(2)
        + (0x0003).to_bytes(4, "big")  # BlockAck: segments 0 and 1
    )
    raw = network.encode(
        ctx, ctl=True, ttl=3, seq=ctx.next_seq(), src=0x0002, dst=0x0001,
        transport_pdu=ack,
    )
    a.handle_network_pdu(raw)
    assert a.last_ack(0x09AB) == SegmentAck(obo=False, seq_zero=0x09AB, block_ack=3)
    assert a.last_ack(0x0001) is None
    assert not a.received  # control traffic is not an access message


# ------------------------------------------------------------------ ignoring


def test_foreign_netkey_pdu_ignored():
    a, _, _ = make_pair()
    foreign = network.NetworkContext(net_key=bytes(range(16)), iv_index=IV_INDEX)
    raw = network.encode(
        foreign, ctl=False, ttl=3, seq=foreign.next_seq(), src=0x0003,
        dst=0x0001, transport_pdu=build_unsegmented_access(bytes(5), akf=False, aid=0),
    )
    a.handle_network_pdu(raw)  # must not raise
    assert not a.received


def test_self_echo_ignored():
    """A valid PDU whose SRC is our own address is dropped (bearer echo)."""
    a, _, _ = make_pair()
    ctx = network.NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0x600)
    seq = ctx.next_seq()
    upper = encrypt_access(
        APP_KEY, akf=True, seq=seq, src=0x0001, dst=0x0001,
        iv_index=IV_INDEX, access_pdu=generic_onoff_set(True, 0x06),
    )
    raw = network.encode(
        ctx, ctl=False, ttl=3, seq=seq, src=0x0001, dst=0x0001,
        transport_pdu=build_unsegmented_access(upper, akf=True, aid=k4(APP_KEY)),
    )
    a.handle_network_pdu(raw)
    assert not a.received


def test_wrong_aid_ignored():
    """AKF=1 with an AID that does not match k4(AppKey) is dropped."""
    a, _, _ = make_pair()
    ctx = network.NetworkContext(net_key=NET_KEY, iv_index=IV_INDEX, seq=0x700)
    seq = ctx.next_seq()
    upper = encrypt_access(
        APP_KEY, akf=True, seq=seq, src=0x0002, dst=0x0001,
        iv_index=IV_INDEX, access_pdu=generic_onoff_set(True, 0x07),
    )
    wrong_aid = (k4(APP_KEY) + 1) & 0x3F
    raw = network.encode(
        ctx, ctl=False, ttl=3, seq=seq, src=0x0002, dst=0x0001,
        transport_pdu=build_unsegmented_access(upper, akf=True, aid=wrong_aid),
    )
    a.handle_network_pdu(raw)
    assert not a.received
