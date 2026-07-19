"""Runtime coordinator for the Bluetooth Mesh integration (Task B3).

This is the single long-lived object per config entry. It owns the parsed
:class:`btmesh.network_model.Network`, finds and connects the mesh proxy through
the HA-bluetooth bridge (:mod:`.mesh_transport`), drives a
:class:`btmesh.controller.MeshController`, and exposes a handful of best-effort
command coroutines that the light entities call. Everything BLE lives behind
this facade; entities only ever see the network model and these coroutines.

Three cross-cutting concerns live here rather than in the entities:

* **Availability + reconnect.** A mesh node offers a *single* proxy connection
  slot. If no proxy is advertising our Network ID, or a live command hits a
  bearer error, the coordinator marks itself unavailable and retries with a
  simple capped backoff (a single reconnect task, cancelled on stop).
* **A repairs issue.** When the proxy stays unreachable past a threshold, a
  user-facing ``proxy_unreachable`` repair is raised with actionable advice
  (the single-slot problem: close the Häfele app / free the lamp). It clears on
  the first successful connect.
* **SEQ persistence (replay safety).** The mesh silently drops any network PDU
  whose SEQ it has already seen, so the sequence cursor must survive restarts.
  It is mirrored to a :class:`~homeassistant.helpers.storage.Store` after every
  command and seeded back (plus a safety margin) when a controller is rebuilt.
"""

from __future__ import annotations

import asyncio
import json
import logging

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .btmesh.controller import MeshController
from .btmesh.network_model import Network

from .const import CONF_CONNECT_JSON, DOMAIN
from .mesh_transport import async_connect_bearer, find_proxy_address

logger = logging.getLogger(__name__)

__all__ = ["MeshCoordinator"]

# Our provisioner/controller source address on the subnet. 0x7FFF is a spare
# unicast well clear of the app-provisioned nodes (matches the library default
# and the Phase-0 harness).
SRC_ADDR = 0x7FFF

# Consecutive failed connect attempts before we surface the proxy_unreachable
# repair issue. A transient miss (proxy momentarily off the discovery snapshot)
# should not nag the user, so we only raise it once the miss is sustained.
UNREACHABLE_THRESHOLD = 3

# Reconnect backoff: start small, double on each miss, cap it. One task at a
# time; cancelled on stop (YAGNI — no elaborate state machine).
INITIAL_RECONNECT_DELAY = 10.0
MAX_RECONNECT_DELAY = 300.0

# Store schema version and the SEQ safety margin. We persist after every
# command, so a crash can lose at most one command's worth of SEQ; the margin
# jumps the cursor comfortably past anything that might not have been flushed.
STORAGE_VERSION = 1
SEQ_SAFETY_MARGIN = 32


class MeshCoordinator:
    """Own one mesh subnet's proxy connection and controller for a config entry.

    Construct it, ``await async_start()`` in ``async_setup_entry``, and
    ``await async_stop()`` in ``async_unload_entry``. The command coroutines are
    best-effort: they return ``None`` (or the controller's ``None``) when the
    mesh is unavailable or a command times out, never raising to the caller.
    """

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._network = Network.from_connect(
            json.loads(entry.data[CONF_CONNECT_JSON])
        )
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seq"
        )
        self._controller: MeshController | None = None
        self._client = None
        self._available = False
        self._stopped = False
        self._fail_count = 0
        self._issue_active = False
        self._reconnect_unsub: CALLBACK_TYPE | None = None
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        # Serialise commands and reconnects so a reconnect never races a
        # command against a half-built controller.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- properties

    @property
    def network(self) -> Network:
        """The parsed static network model (entities enumerate its nodes)."""
        return self._network

    @property
    def available(self) -> bool:
        """True while a proxy is connected and the controller is started."""
        return self._available

    # -------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Parse done in __init__; connect the proxy and start the controller.

        Never hard-fails: if no proxy is found (or the connect fails) the entry
        still sets up — the coordinator is simply unavailable and retries in the
        background until a proxy appears.
        """
        self._stopped = False
        await self._async_connect()

    async def async_stop(self) -> None:
        """Cancel any pending reconnect and stop the controller."""
        self._stopped = True
        self._cancel_reconnect()
        await self._teardown_controller()
        self._clear_issue()

    async def _async_connect(self) -> None:
        """One connect attempt: find proxy → connect → start controller.

        On any miss/failure it marks unavailable, raises the repair issue once
        the miss is sustained, and schedules a backed-off retry.
        """
        async with self._lock:
            if self._stopped or self._controller is not None:
                return

            address = find_proxy_address(self.hass, self._network.net_key)
            if address is None:
                logger.debug("no mesh proxy advertising our network yet")
                self._note_failure()
                return

            try:
                client, bearer = await async_connect_bearer(self.hass, address)
                seq = await self._load_seq()
                controller = MeshController(
                    self._network, bearer, src_addr=SRC_ADDR, seq=seq
                )
                await controller.start()
            except Exception as exc:  # noqa: BLE001 - MeshTransportError + bearer/GATT
                logger.debug("mesh proxy connect failed: %s", exc)
                self._note_failure()
                return

            self._client = client
            self._controller = controller
            self._available = True
            self._fail_count = 0
            self._reconnect_delay = INITIAL_RECONNECT_DELAY
            self._clear_issue()
            logger.info("mesh proxy %s connected (seq seeded to %d)", address, seq)

    async def _teardown_controller(self) -> None:
        """Stop and drop the controller/client; mark unavailable."""
        self._available = False
        controller, self._controller = self._controller, None
        self._client = None
        if controller is not None:
            try:
                await controller.stop()
            except Exception:  # noqa: BLE001
                logger.debug("controller stop failed", exc_info=True)

    # --------------------------------------------------------------- commands

    async def async_set_onoff(self, unicast: int, on: bool) -> bool | None:
        """Set Generic OnOff on ``unicast``; None if unavailable/timeout."""
        return await self._run(lambda c: c.set_onoff(unicast, on))

    async def async_get_onoff(self, unicast: int) -> bool | None:
        """Read Generic OnOff from ``unicast``; None if unavailable/timeout."""
        return await self._run(lambda c: c.get_onoff(unicast))

    async def async_set_lightness(
        self, unicast: int, level_0_1: float
    ) -> int | None:
        """Set Light Lightness (0..1) on ``unicast``; None if unavailable."""
        return await self._run(lambda c: c.set_lightness(unicast, level_0_1))

    async def async_set_ctl_temperature(
        self, unicast: int, kelvin: int
    ) -> int | None:
        """Set Light CTL Temperature (K) on ``unicast``; None if unavailable."""
        return await self._run(lambda c: c.set_ctl_temperature(unicast, kelvin))

    async def _run(self, call):
        """Delegate one command to the controller under the lock.

        Returns the controller's result (``None`` on the controller's own
        timeout). A *bearer* error — the proxy dropped mid-command — is caught,
        marks the coordinator unavailable, and triggers a reconnect; the caller
        just sees ``None``. SEQ is persisted after every completed command.
        """
        async with self._lock:
            controller = self._controller
            if controller is None or not self._available:
                return None
            try:
                result = await call(controller)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mesh command failed, reconnecting: %s", exc)
                await self._teardown_controller()
                self._schedule_reconnect()
                return None
            await self._save_seq(controller)
            return result

    # ----------------------------------------------------------- seq persistence

    async def _load_seq(self) -> int:
        """Stored SEQ + safety margin, or 0 on a fresh install."""
        data = await self._store.async_load()
        if not data:
            return 0
        return int(data.get("seq", 0)) + SEQ_SAFETY_MARGIN

    async def _save_seq(self, controller: MeshController) -> None:
        """Mirror the controller's SEQ cursor to the Store (replay safety)."""
        await self._store.async_save({"seq": controller.seq})

    # ------------------------------------------------------------- reconnect

    def _note_failure(self) -> None:
        """Common miss handling: unavailable, threshold issue, backoff retry."""
        self._available = False
        self._fail_count += 1
        if self._fail_count >= UNREACHABLE_THRESHOLD:
            self._raise_proxy_issue()
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Arm a single backed-off reconnect (no-op if one is pending)."""
        if self._stopped or self._reconnect_unsub is not None:
            return
        delay = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)
        self._reconnect_unsub = async_call_later(
            self.hass, delay, self._reconnect_callback
        )

    @callback
    def _reconnect_callback(self, _now) -> None:
        self._reconnect_unsub = None
        self.hass.async_create_task(self._async_connect())

    def _cancel_reconnect(self) -> None:
        if self._reconnect_unsub is not None:
            self._reconnect_unsub()
            self._reconnect_unsub = None

    # --------------------------------------------------------------- repairs

    def _raise_proxy_issue(self) -> None:
        """Raise the proxy_unreachable repair (idempotent while active)."""
        if self._issue_active:
            return
        self._issue_active = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"proxy_unreachable_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="proxy_unreachable",
            translation_placeholders={"network": self._network.name or "mesh"},
        )

    def _clear_issue(self) -> None:
        """Delete the repair issue (idempotent, even across a restart)."""
        self._issue_active = False
        ir.async_delete_issue(
            self.hass, DOMAIN, f"proxy_unreachable_{self.entry.entry_id}"
        )
