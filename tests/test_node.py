"""MeshNode facade tests: loopback pair of nodes wired via callbacks.

Integration-style: real access encoders, real network/transport codecs — no
mocks of our own layers. Node A (0x0001) plays provisioner, node B (0x0002)
plays the configured device; both share NetKey/AppKey, and B's device key is
registered on both sides (B as its own, A as B's peer).
"""

import asyncio

import pytest

from btmesh import network
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
from btmesh.transport import SegmentAck, encrypt_access, unsegmented_access

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
        dst=0x0001, transport_pdu=unsegmented_access(bytes(5), akf=False, aid=0),
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
        transport_pdu=unsegmented_access(upper, akf=True, aid=k4(APP_KEY)),
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
        transport_pdu=unsegmented_access(upper, akf=True, aid=wrong_aid),
    )
    a.handle_network_pdu(raw)
    assert not a.received
