"""FanoutBearer tests.

The fan-out exists for meshes whose nodes cannot hear each other: one proxy
link per island, every PDU sent down all of them. What matters is that it
reaches every island, that one island failing does not silence the others,
and that it reports total failure exactly the way a single bearer does so the
controller's dead-link handling still applies.
"""

import os

import pytest

from btmesh.bearer import BearerError
from btmesh.controller import MeshController
from btmesh.fanout import FanoutBearer
from btmesh.network_model import Network
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "sample.connect.json"
)


class FakeBearer:
    """Bearer stand-in: records sends, can fail on start or send."""

    def __init__(self, max_frame=66, *, fail_start=False, fail_send=False):
        self.max_frame = max_frame
        self.fail_start = fail_start
        self.fail_send = fail_send
        self.failure = None
        self.sent = []
        self.on_message = None
        self.stopped = False

    async def start(self, on_message):
        if self.fail_start:
            raise BearerError("subscribe refused")
        self.on_message = on_message

    async def stop(self):
        self.stopped = True

    async def send(self, msg_type, payload):
        if self.fail_send:
            raise BearerError("write failed")
        self.sent.append((msg_type, payload))


def test_needs_at_least_one_bearer():
    with pytest.raises(ValueError):
        FanoutBearer([])


async def test_send_reaches_every_link():
    a, b = FakeBearer(), FakeBearer()
    fanout = FanoutBearer([a, b])
    await fanout.start(lambda *_: None)

    await fanout.send(MSG_TYPE_NETWORK_PDU, b"\x01\x02")

    assert a.sent == b.sent == [(MSG_TYPE_NETWORK_PDU, b"\x01\x02")]


async def test_messages_from_any_link_are_delivered():
    a, b = FakeBearer(), FakeBearer()
    received = []
    fanout = FanoutBearer([a, b])
    await fanout.start(lambda t, p: received.append((t, p)))

    a.on_message(MSG_TYPE_NETWORK_PDU, b"from-a")
    b.on_message(MSG_TYPE_NETWORK_PDU, b"from-b")

    assert received == [
        (MSG_TYPE_NETWORK_PDU, b"from-a"),
        (MSG_TYPE_NETWORK_PDU, b"from-b"),
    ]


def test_max_frame_fits_the_narrowest_link():
    """Segmentation happens once, so frames must fit every link."""
    fanout = FanoutBearer([FakeBearer(66), FakeBearer(20), FakeBearer(100)])
    assert fanout.max_frame == 20


async def test_a_link_that_cannot_start_is_dropped_not_fatal():
    good, bad = FakeBearer(), FakeBearer(fail_start=True)
    fanout = FanoutBearer([bad, good])

    await fanout.start(lambda *_: None)

    assert fanout.bearers == [good]
    assert fanout.failure is None


async def test_start_raises_when_no_link_can_start():
    fanout = FanoutBearer([FakeBearer(fail_start=True), FakeBearer(fail_start=True)])
    with pytest.raises(BearerError):
        await fanout.start(lambda *_: None)


async def test_a_failed_write_drops_that_link_and_keeps_the_rest():
    good, bad = FakeBearer(), FakeBearer(fail_send=True)
    fanout = FanoutBearer([good, bad])
    await fanout.start(lambda *_: None)

    await fanout.send(MSG_TYPE_NETWORK_PDU, b"\x01")  # must not raise

    assert good.sent == [(MSG_TYPE_NETWORK_PDU, b"\x01")]
    assert fanout.bearers == [good]
    assert fanout.failure is None


async def test_send_raises_once_every_link_has_failed():
    """Total failure must surface like a single bearer's, for the pump."""
    fanout = FanoutBearer([FakeBearer(fail_send=True), FakeBearer(fail_send=True)])
    await fanout.start(lambda *_: None)

    with pytest.raises(BearerError):
        await fanout.send(MSG_TYPE_NETWORK_PDU, b"\x01")
    assert fanout.failure is not None


async def test_a_child_with_a_late_subscribe_failure_is_not_counted():
    """A child that lost its Data Out subscribe receives nothing any more."""
    a, b = FakeBearer(), FakeBearer()
    fanout = FanoutBearer([a, b])
    await fanout.start(lambda *_: None)

    a.failure = BearerError("late subscribe failure")
    await fanout.send(MSG_TYPE_NETWORK_PDU, b"\x01")
    assert a.sent == []
    assert b.sent == [(MSG_TYPE_NETWORK_PDU, b"\x01")]
    assert fanout.failure is None

    b.failure = BearerError("late subscribe failure")
    assert fanout.failure is not None


async def test_stop_stops_every_link():
    a, b = FakeBearer(), FakeBearer()
    fanout = FanoutBearer([a, b])
    await fanout.start(lambda *_: None)

    await fanout.stop()

    assert a.stopped and b.stopped


async def test_controller_reports_failed_only_when_every_link_failed():
    """The controller polls bearer.failure; the fan-out must fit that probe."""
    a, b = FakeBearer(), FakeBearer()
    fanout = FanoutBearer([a, b])
    controller = MeshController(Network.from_connect_file(FIXTURE), fanout)
    await controller.start()
    try:
        a.failure = BearerError("gone")
        assert not controller.failed
        b.failure = BearerError("gone")
        assert controller.failed
    finally:
        await controller.stop()
