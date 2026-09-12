"""GattBearer: the subscribe grace period must not hide a real failure.

Background (2026-09-04, on the author's own network): the proxy node answered
the Data Out CCCD write with ``Insufficient authorization (8)`` — seven seconds
after ``start()`` had already given up waiting and returned. The pending
subscribe task swallowed the error, the controller came up, the coordinator
marked itself available, and every Set went into a link that was already gone.
"""

from __future__ import annotations

import asyncio

import pytest

from btmesh import bearer as bearer_mod
from btmesh.bearer import GattBearer


class SlowClient:
    """A GATT client whose ``start_notify`` only settles after ``start()`` returned.

    ``outcome`` is awaited once the grace period has elapsed: an exception
    instance is raised, anything else resolves the subscribe normally.
    """

    def __init__(self, outcome: BaseException | None = None) -> None:
        self.mtu_size = 23
        self._release = asyncio.Event()
        self._outcome = outcome

    def release(self) -> None:
        self._release.set()

    async def start_notify(self, char, callback) -> None:
        await self._release.wait()
        if self._outcome is not None:
            raise self._outcome

    async def stop_notify(self, char) -> None:
        pass


@pytest.fixture
def short_grace(monkeypatch):
    monkeypatch.setattr(bearer_mod, "START_NOTIFY_TIMEOUT", 0.01)


async def test_a_late_subscribe_error_is_reported_as_a_failure(short_grace):
    error = RuntimeError("Insufficient authorization (8)")
    client = SlowClient(outcome=error)
    bearer = GattBearer(client)

    await bearer.start(lambda _t, _p: None)  # grace period elapses, proceeds
    assert bearer.failure is None

    client.release()
    await asyncio.sleep(0)  # let the pending subscribe task settle
    await asyncio.sleep(0)

    assert bearer.failure is error
    await bearer.stop()


async def test_a_late_subscribe_that_confirms_leaves_no_failure(short_grace):
    client = SlowClient(outcome=None)
    bearer = GattBearer(client)

    await bearer.start(lambda _t, _p: None)
    client.release()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert bearer.failure is None
    await bearer.stop()
