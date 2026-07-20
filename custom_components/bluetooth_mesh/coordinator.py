"""Runtime coordinator for the Bluetooth Mesh integration (Task B3).

This is the single long-lived object per config entry. It owns the parsed
:class:`btmesh.network_model.Network`, and drives mesh commands through the
HA-bluetooth bridge (:mod:`.mesh_transport`) using an **on-demand** connection
model: every command opens a fresh proxy connection, runs, and closes it again.

Why on-demand rather than a persistent connection: a mesh node offers a *single*
proxy connection slot. Holding it open forever means a HA restart or a dropped
link leaves a zombie connection pinned at the ESPHome proxy that blocks every
reconnection, and it prevents the vendor app from ever talking to the lamp. By
connecting per command and disconnecting cleanly in a ``finally`` block, the
slot is free the moment the command returns.

Three cross-cutting concerns live here rather than in the entities:

* **Availability + a periodic probe.** Because we no longer hold a connection,
  availability is refreshed by a light periodic probe (a Generic OnOff GET on
  the first node) plus the outcome of every real command. A miss marks the
  coordinator unavailable.
* **A repairs issue.** When the proxy stays unreachable past a threshold, a
  user-facing ``proxy_unreachable`` repair is raised with actionable advice
  (the single-slot problem: close the Häfele app / free the lamp). It clears on
  the first successful connect.
* **SEQ persistence (replay safety).** The mesh silently drops any network PDU
  whose SEQ it has already seen, so the sequence cursor must survive restarts.
  It is kept in-memory as ``self._seq`` (seeded once from the Store plus a
  safety margin at start) and mirrored back to the Store after every command.
  Crucially the margin is added **once**, not per command, so the cursor does
  not inflate on every call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .btmesh.controller import MeshController
from .btmesh.crypto import k3
from .btmesh.network_model import Network

from .const import CONF_CONNECT_JSON, DOMAIN
from .mesh_transport import (
    async_connect_bearer,
    discovered_proxies,
    find_proxy_address,
)

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

# Store schema version and the SEQ safety margin. The margin is applied ONCE at
# start (seeded cursor = stored seq + margin), jumping comfortably past anything
# that might not have been flushed before a crash. It is NOT re-added per command
# — each command advances the cursor by exactly what the controller consumed.
STORAGE_VERSION = 1
SEQ_SAFETY_MARGIN = 32

# How often to probe the mesh for availability. We no longer hold a connection,
# so this light churn is the only time we take the lamp's single slot; the rest
# of the time it is free for the vendor app.
PROBE_INTERVAL = timedelta(minutes=2)

# Hard ceiling on a single connect+command cycle so a hung proxy connection can
# never wedge the lock forever.
CONNECT_TIMEOUT = 20.0


class MeshCoordinator:
    """Own one mesh subnet's network model and on-demand commands for an entry.

    Construct it, ``await async_start()`` in ``async_setup_entry``, and
    ``await async_stop()`` in ``async_unload_entry``. The command coroutines are
    best-effort: they return ``None`` when the mesh is unavailable or a command
    times out, never raising to the caller. Each command connects to the proxy,
    runs, and disconnects again — the lamp's single proxy slot is only ever held
    for the duration of one command.
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
        self._seq = 0
        self._available = False
        self._stopped = False
        self._fail_count = 0
        self._issue_active = False
        self._probe_unsub: CALLBACK_TYPE | None = None
        # Serialise everything through a single connection at a time: two
        # commands must never contend for the lamp's single proxy slot.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- properties

    @property
    def network(self) -> Network:
        """The parsed static network model (entities enumerate its nodes)."""
        return self._network

    @property
    def available(self) -> bool:
        """True while the most recent connect/command/probe succeeded."""
        return self._available

    # -------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Seed the SEQ cursor once, then arm the periodic availability probe.

        Never hard-fails: if no proxy is reachable the entry still sets up and
        the coordinator is simply unavailable until a probe or command succeeds.
        The SEQ safety margin is applied here exactly once.
        """
        self._stopped = False
        self._seq = await self._load_seq()
        self._probe_unsub = async_track_time_interval(
            self.hass, self._async_probe, PROBE_INTERVAL
        )
        # Fire one probe now so availability reflects reality without waiting a
        # full interval; awaited (not backgrounded) so setup sees the result.
        await self._async_probe()

    async def async_stop(self) -> None:
        """Cancel the periodic probe and clear the repair issue."""
        self._stopped = True
        if self._probe_unsub is not None:
            self._probe_unsub()
            self._probe_unsub = None
        self._available = False
        self._clear_issue()

    # ---------------------------------------------------------- on-demand core

    async def _run_connected(self, call):
        """Connect on-demand, run ``call(controller)``, then always disconnect.

        This is the heart of the on-demand model. Under the lock it finds the
        proxy, opens a bearer, starts a controller seeded from the in-memory SEQ
        cursor, runs the command, persists the advanced SEQ, and — no matter what
        — stops the controller and disconnects the client in the ``finally``
        block so the lamp's single proxy slot is freed after every command.

        Returns the command's result, or ``None`` when the mesh is unreachable
        or the command times out.
        """
        async with self._lock:
            address = find_proxy_address(self.hass, self._network.net_key)
            if address is None:
                seen = discovered_proxies(self.hass)
                logger.debug(
                    "no mesh proxy advertising our network; "
                    "0x1828 adverts HA sees: %s",
                    seen or "none",
                )
                self._set_unavailable()
                return None

            client = bearer = controller = None
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    client, bearer = await async_connect_bearer(
                        self.hass, address
                    )
                    controller = MeshController(
                        self._network, bearer, src_addr=SRC_ADDR, seq=self._seq
                    )
                    await controller.start()
                    result = await call(controller)
                # Advance and persist the cursor by exactly what was consumed —
                # no margin re-added here (that is applied once, at start).
                self._seq = controller.seq
                await self._store.async_save({"seq": self._seq})
                self._set_available()
                return result
            except Exception as exc:  # noqa: BLE001 - transport/GATT/timeout
                logger.debug("mesh command failed: %s", exc)
                self._set_unavailable()
                return None
            finally:
                # Clean disconnect after EVERY command — the whole point of the
                # on-demand model. Failures here must not mask the result.
                if controller is not None:
                    try:
                        await controller.stop()
                    except Exception:  # noqa: BLE001
                        logger.debug("controller stop failed", exc_info=True)
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        logger.debug("client disconnect failed", exc_info=True)

    # --------------------------------------------------------------- commands

    async def async_set_onoff(self, unicast: int, on: bool) -> bool | None:
        """Set Generic OnOff on ``unicast``; None if unavailable/timeout."""
        return await self._run_connected(lambda c: c.set_onoff(unicast, on))

    async def async_get_onoff(self, unicast: int) -> bool | None:
        """Read Generic OnOff from ``unicast``; None if unavailable/timeout."""
        return await self._run_connected(lambda c: c.get_onoff(unicast))

    async def async_set_lightness(
        self, unicast: int, level_0_1: float
    ) -> int | None:
        """Set Light Lightness (0..1) on ``unicast``; None if unavailable."""
        return await self._run_connected(
            lambda c: c.set_lightness(unicast, level_0_1)
        )

    async def async_set_ctl(
        self, unicast: int, level_0_1: float, kelvin: int
    ) -> int | None:
        """Set Light CTL (lightness + temperature) on ``unicast``; None if unavailable."""
        return await self._run_connected(
            lambda c: c.set_ctl(unicast, level_0_1, kelvin)
        )

    async def async_set_ctl_temperature(
        self, unicast: int, kelvin: int
    ) -> int | None:
        """Set Light CTL Temperature only (K) on ``unicast``; None if unavailable."""
        return await self._run_connected(
            lambda c: c.set_ctl_temperature(unicast, kelvin)
        )

    # ---------------------------------------------------------------- probe

    async def _async_probe(self, now=None) -> None:
        """Periodic availability probe: a cheap GET on the primary node.

        Runs through :meth:`_run_connected`, so it refreshes availability (and
        the repair issue) exactly like a real command, while holding the lamp's
        slot only briefly.
        """
        if self._stopped:
            return
        unicast = self._primary_unicast()
        if unicast is None:
            return
        await self._run_connected(lambda c: c.get_onoff(unicast))

    def _primary_unicast(self) -> int | None:
        """Unicast of the first lighting node, or None if the network is empty."""
        nodes = self._network.nodes
        if not nodes:
            return None
        return nodes[0].unicast

    # ----------------------------------------------------------- seq persistence

    async def _load_seq(self) -> int:
        """Stored SEQ + safety margin, or 0 on a fresh install (applied once)."""
        data = await self._store.async_load()
        if not data:
            return 0
        return int(data.get("seq", 0)) + SEQ_SAFETY_MARGIN

    # --------------------------------------------------------------- availability

    def _set_available(self) -> None:
        """Mark reachable: clear the fail count and any active repair issue."""
        self._available = True
        self._fail_count = 0
        self._clear_issue()

    def _set_unavailable(self) -> None:
        """Mark unreachable; raise the repair once the miss is sustained."""
        self._available = False
        self._fail_count += 1
        if self._fail_count >= UNREACHABLE_THRESHOLD:
            self._raise_proxy_issue()

    # --------------------------------------------------------------- repairs

    def _raise_proxy_issue(self) -> None:
        """Raise the proxy_unreachable repair (idempotent while active).

        Includes a diagnostic of every 0x1828 mesh-proxy advert HA currently
        sees, so a "no proxy in range" miss (``none``) can be told apart from a
        "wrong keys" one (a foreign ``network_id=...``) straight from the UI.
        """
        if self._issue_active:
            return
        self._issue_active = True
        seen = discovered_proxies(self.hass)
        seen_text = (
            ", ".join(f"{addr} ({desc})" for addr, desc in seen) if seen else "none"
        )
        network_id = k3(self._network.net_key).hex()
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"proxy_unreachable_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="proxy_unreachable",
            translation_placeholders={
                "network": self._network.name or "mesh",
                "network_id": network_id,
                "seen": seen_text,
            },
        )

    def _clear_issue(self) -> None:
        """Delete the repair issue (idempotent, even across a restart)."""
        self._issue_active = False
        ir.async_delete_issue(
            self.hass, DOMAIN, f"proxy_unreachable_{self.entry.entry_id}"
        )
