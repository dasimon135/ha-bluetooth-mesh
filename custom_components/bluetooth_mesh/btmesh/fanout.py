"""FanoutBearer: one logical bearer over several proxy links at once.

A mesh whose nodes cannot hear each other is several islands that share keys.
Through a single GATT proxy link, everything on the other islands is out of
reach: commands and reads alike time out, and nothing in the stack can tell
that apart from a node that is merely slow. Seen on a two-box Häfele install
on 2026-10-03: the boxes sat either side of a room divider, the Config Relay
Get probe answered only for whichever box held the link, and the vendor app
behaved the same way, so no setting on the nodes could fix it.

Holding one link per island and sending every PDU down all of them reaches
every node. The duplicates this creates are harmless by design: a node that
hears the same network PDU twice drops the second copy through its network
message cache, and :class:`btmesh.node.MeshNode` already drops an immediate
duplicate delivery on the way back up (same source, same SEQ).

It has the :class:`btmesh.bearer.GattBearer` surface that
:class:`btmesh.pump.BearerPump` and :class:`btmesh.controller.MeshController`
use (``max_frame``, ``start``, ``stop``, ``send``, ``failure``), so neither of
them knows it is there.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from .bearer import BearerError

logger = logging.getLogger(__name__)

__all__ = ["FanoutBearer"]


class FanoutBearer:
    """Send on every child bearer; deliver what any of them receives.

    A child that fails is dropped and the others carry on: one island going
    quiet must not take the rest down with it. The fan-out as a whole fails
    only when no child is left, and then reports the way a single bearer does
    (``send`` raises :class:`BearerError`, ``failure`` is set) so the existing
    dead-link handling upstream applies unchanged.
    """

    def __init__(self, bearers: Sequence[Any]) -> None:
        if not bearers:
            raise ValueError("FanoutBearer needs at least one bearer")
        self._bearers = list(bearers)
        self._last_error: BaseException | None = None
        self._on_message: Callable[[int, bytes], None] | None = None

    @property
    def bearers(self) -> list[Any]:
        """The children still in use, in the order they were given."""
        return list(self._bearers)

    @property
    def max_frame(self) -> int:
        """The smallest frame every live child can carry.

        Segmentation happens once per message, here, before the copies go out,
        so it has to fit the narrowest link: a frame sized for a wider one is
        one the narrow link's proxy would refuse.
        """
        return min(bearer.max_frame for bearer in self._live())

    @property
    def failure(self) -> BaseException | None:
        """Set once every child has failed; ``None`` while any is usable.

        A child whose own ``failure`` is set (a subscribe that failed after its
        grace period, see ``GattBearer.failure``) no longer receives, so it is
        not counted as usable even though writes to it may still succeed.
        """
        if self._live():
            return None
        return self._last_error or BearerError("every proxy link has failed")

    def _live(self) -> list[Any]:
        return [b for b in self._bearers if getattr(b, "failure", None) is None]

    async def start(self, on_message: Callable[[int, bytes], None]) -> None:
        """Subscribe every child, dropping those that cannot subscribe.

        Raises only when none of them could: a link that never subscribed
        delivers nothing, but the others still reach their own islands.
        """
        self._on_message = on_message
        started = []
        for bearer in self._bearers:
            try:
                await bearer.start(on_message)
            except Exception as exc:  # noqa: BLE001 - one island, not all
                self._last_error = exc
                logger.warning("dropping a proxy link that failed to start: %s", exc)
                continue
            started.append(bearer)
        if not started:
            raise BearerError(f"no proxy link could start: {self._last_error}")
        self._bearers = started

    async def add(self, bearer: Any) -> None:
        """Start ``bearer`` and send through it too from now on.

        For a node that was not heard when the links went up: after a restart
        the scanner can take a minute to hear a weak node again, and with
        keep-alive 0 nothing reconnects later, so that island stayed out of
        reach until someone reloaded the entry. Joining it here leaves the
        working links alone. A bearer that cannot start raises and is not added.

        The new link's proxy filter is the caller's job (see
        :meth:`btmesh.controller.MeshController.refresh_proxy_filter`): its
        accept list starts empty, so until then it forwards nothing back.
        """
        if self._on_message is None:
            raise BearerError("add() before start(): nowhere to deliver to")
        await bearer.start(self._on_message)
        self._bearers.append(bearer)

    async def stop(self) -> None:
        """Stop every child; one failing to stop does not skip the rest."""
        results = await asyncio.gather(
            *(bearer.stop() for bearer in self._bearers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("proxy link stop failed (ignored): %s", result)

    async def send(self, msg_type: int, payload: bytes) -> None:
        """Write ``payload`` to every live child, concurrently.

        Each child serialises its own frames, and the pump in front of this
        object serialises messages, so concurrency here never interleaves two
        messages' SAR frames on one link. A child whose write fails is dropped;
        the send raises only if it reached no child at all.
        """
        live = self._live()
        if not live:
            raise BearerError(f"every proxy link has failed: {self._last_error}")
        results = await asyncio.gather(
            *(bearer.send(msg_type, payload) for bearer in live),
            return_exceptions=True,
        )
        failed = []
        for bearer, result in zip(live, results):
            if isinstance(result, BaseException):
                self._last_error = result
                failed.append(bearer)
                logger.warning("dropping a proxy link whose write failed: %s", result)
        if failed:
            self._bearers = [b for b in self._bearers if b not in failed]
        if len(failed) == len(live):
            raise BearerError(f"every proxy link has failed: {self._last_error}")
