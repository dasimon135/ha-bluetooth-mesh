"""BearerPump routing tests.

The pump exists to serialize sends: two concurrent ``send()`` calls would
interleave their SAR frames and corrupt reassembly at the peer. Proxy
configuration must therefore go through the SAME pump as network PDUs even
though it travels under a different proxy message type.
"""

import asyncio

from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU, MSG_TYPE_PROXY_CONFIG
from btmesh.pump import BearerPump


class RecordingBearer:
    """Bearer stand-in recording every (msg_type, payload) it is handed."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, bytes]] = []

    async def send(self, msg_type: int, payload: bytes) -> None:
        self.sent.append((msg_type, payload))


async def _drain(bearer: RecordingBearer, expected: int) -> None:
    for _ in range(100):
        if len(bearer.sent) >= expected:
            return
        await asyncio.sleep(0.001)


async def test_put_can_override_the_message_type_preserving_order():
    """A per-PDU message type rides the same queue, so ordering is preserved."""
    bearer = RecordingBearer()
    pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
    pump.start()
    try:
        pump.put(b"\x01", msg_type=MSG_TYPE_PROXY_CONFIG)
        pump.put(b"\x02")  # falls back to the pump's default type
        await _drain(bearer, 2)
    finally:
        await pump.stop()

    assert bearer.sent == [
        (MSG_TYPE_PROXY_CONFIG, b"\x01"),
        (MSG_TYPE_NETWORK_PDU, b"\x02"),
    ]
