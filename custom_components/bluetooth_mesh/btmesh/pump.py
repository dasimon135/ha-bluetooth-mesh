"""BearerPump: an ordered bridge from sync PDU producers to the async bearer.

The provisioner and :class:`btmesh.node.MeshNode` emit PDUs from synchronous
callbacks; the bearer's ``send()`` is async. This module bridges the two with a
single-consumer queue so emission order (and SAR frame ordering) is preserved.

Lifted verbatim from the Phase 0 hardware harness so both the harness and the
Phase 1 :class:`btmesh.controller.MeshController` share one implementation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .bearer import GattBearer

logger = logging.getLogger(__name__)

__all__ = ["BearerPump"]


class BearerPump:
    """Ordered bridge from sync producers to the async bearer.

    The provisioner and MeshNode emit PDUs from synchronous callbacks; the
    bearer's send() is async. An asyncio.Queue drained by a single task
    preserves emission order (and serializes send(): concurrent sends would
    interleave SAR frames). A send failure kills the pump: it is recorded in
    ``failure``, passed to ``on_error``, and nothing further is transmitted.
    """

    def __init__(self, bearer: GattBearer, msg_type: int) -> None:
        self._bearer = bearer
        self._msg_type = msg_type
        self._queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.failure: BaseException | None = None
        self.on_error: Callable[[BaseException], None] | None = None

    def put(self, pdu: bytes, msg_type: int | None = None) -> None:
        """Queue one PDU, optionally overriding the pump's message type.

        Proxy configuration travels under its own proxy message type but must
        share this queue: a send that bypassed it could interleave its SAR
        frames with a network PDU's and corrupt reassembly at the peer.
        """
        self._queue.put_nowait(
            (self._msg_type if msg_type is None else msg_type, pdu)
        )

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                msg_type, pdu = await self._queue.get()
                await self._bearer.send(msg_type, pdu)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.failure = exc
            logger.error("bearer TX pump died: %s", exc)
            if self.on_error is not None:
                self.on_error(exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
