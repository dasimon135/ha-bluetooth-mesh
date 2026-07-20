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
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .btmesh.controller import MeshController
from .btmesh.crypto import k3
from .btmesh.network_model import Network

from .const import (
    CONF_CONNECT_JSON,
    CONF_KEEPALIVE,
    DEFAULT_KEEPALIVE,
    DOMAIN,
)
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
# of the time it is free for the vendor app. The interval is adaptive: when the
# lamp is reachable we probe rarely (coexistence with the app); when it is NOT,
# we retry quickly to reconnect fast at startup and after a drop (like the
# daikin_madoka integration's short retry).
PROBE_INTERVAL_AVAILABLE = timedelta(minutes=2)
PROBE_INTERVAL_UNAVAILABLE = timedelta(seconds=15)

# Hard ceiling on establishing the proxy connection so a hung connect can never
# wedge the lock forever.
CONNECT_TIMEOUT = 20.0

# Hard ceiling on a single command once connected — a safety net around the
# per-command status wait below.
COMMAND_TIMEOUT = 8.0

# How long a command waits for the node's Status reply before giving up. This is
# deliberately SHORT: control is fire-and-forget (the Set reaches the node on the
# wire regardless), and the node does not currently forward Status replies back
# over the proxy connection (its proxy filter is not configured), so a long wait
# would only add latency to every button press. If a Status does arrive within
# the window we still use its confirmed value; otherwise the command is optimistic.
STATUS_TIMEOUT = 1.5

# Default seconds to HOLD the proxy connection open after the last command
# before dropping it to free the lamp's single proxy slot. Opening a proxy
# connection over an ESPHome BLE proxy costs several seconds, so holding it makes
# a burst of commands feel instant instead of paying that cost every time. This
# is the fallback when the config entry has no explicit keep-alive option;
# ``0`` (the shipped default) keeps the connection always open. Overridable per
# entry via the options flow (:data:`.const.CONF_KEEPALIVE`).
DEFAULT_IDLE_DISCONNECT = DEFAULT_KEEPALIVE


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
        # Persistent TID cursor: carried across on-demand controllers so
        # consecutive Set messages (ON then OFF) never collide on the same TID
        # and get dropped by the node as a retransmit. In-memory is enough — the
        # node's dedup window is seconds, far shorter than a restart.
        self._tid = 0
        self._available = False
        self._stopped = False
        self._fail_count = 0
        self._issue_active = False
        self._probe_unsub: CALLBACK_TYPE | None = None
        # The HELD proxy connection (keep-alive): reused across commands and
        # dropped after _idle_timeout seconds of inactivity (0 = never drop).
        # None while disconnected.
        self._client = None
        self._controller: MeshController | None = None
        self._idle_unsub: CALLBACK_TYPE | None = None
        self._idle_timeout: int = int(
            entry.options.get(CONF_KEEPALIVE, DEFAULT_IDLE_DISCONNECT)
        )
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
        """Seed the SEQ cursor once, probe once, then self-schedule probes.

        Never hard-fails: if no proxy is reachable the entry still sets up and
        the coordinator retries quickly in the background until a probe or
        command succeeds. The SEQ safety margin is applied here exactly once.
        """
        self._stopped = False
        self._seq = await self._load_seq()
        # Fire one probe now so availability reflects reality without waiting;
        # awaited (not backgrounded) so setup sees the result.
        await self._async_probe()
        self._schedule_probe()

    async def async_stop(self) -> None:
        """Cancel timers, drop the held connection, and clear the repair issue."""
        self._stopped = True
        self._cancel_probe()
        self._cancel_idle()
        async with self._lock:
            await self._teardown()
        self._available = False
        self._clear_issue()

    def _schedule_probe(self) -> None:
        """Arm the next probe: fast while unavailable, slow while reachable."""
        if self._stopped or self._probe_unsub is not None:
            return
        delay = (
            PROBE_INTERVAL_AVAILABLE if self._available
            else PROBE_INTERVAL_UNAVAILABLE
        )
        self._probe_unsub = async_call_later(
            self.hass, delay.total_seconds(), self._probe_callback
        )

    def _cancel_probe(self) -> None:
        if self._probe_unsub is not None:
            self._probe_unsub()
            self._probe_unsub = None

    async def _probe_callback(self, _now) -> None:
        self._probe_unsub = None
        # Only probe to RECOVER when we believe we are unavailable; while
        # available we rely on real commands + the held connection, so we never
        # churn the lamp's slot behind the user's back.
        if not self._available:
            await self._async_probe()
        self._schedule_probe()

    # ------------------------------------------------------- keep-alive core

    async def _ensure_connected(self) -> "MeshController | None":
        """Return a live controller, reusing the held connection or opening one.

        Opening a proxy connection over an ESPHome BLE proxy costs several
        seconds, so the connection is kept open between commands (see
        :data:`IDLE_DISCONNECT`) and simply reused here when still alive.
        **Bringing the connection up is the availability signal** — the mesh node
        is on the other end of the proxy link, so control will reach it; we do
        not depend on a Status reply for availability. Returns ``None`` (and marks
        unavailable) when no proxy is reachable or the connect fails. Callers hold
        :attr:`_lock`.
        """
        if self._controller is not None:
            if getattr(self._client, "is_connected", True):
                return self._controller
            # The held link died under us; drop it and reconnect below.
            await self._teardown()

        address = find_proxy_address(self.hass, self._network.net_key)
        if address is None:
            seen = discovered_proxies(self.hass)
            logger.warning(
                "no connectable mesh proxy for network_id %s; "
                "0x1828 adverts HA sees: %s",
                k3(self._network.net_key).hex(),
                seen or "none",
            )
            self._set_unavailable()
            return None

        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                client, bearer = await async_connect_bearer(self.hass, address)
                controller = MeshController(
                    self._network, bearer, src_addr=SRC_ADDR,
                    seq=self._seq, tid=self._tid,
                )
                await controller.start()
        except Exception as exc:  # noqa: BLE001 - transport/GATT/connect
            logger.debug("mesh connect failed: %s", exc)
            await self._teardown()
            self._set_unavailable()
            return None

        self._client = client
        self._controller = controller
        self._set_available()
        return controller

    async def _run_connected(self, call=None):
        """Reuse (or open) the held proxy connection and run ``call(controller)``.

        Under the lock it (re)establishes the keep-alive connection, runs the
        best-effort command under :data:`COMMAND_TIMEOUT`, persists the SEQ/TID
        the controller consumed — even on a command error, since the Set was
        already emitted and a reused SEQ would be dropped as a replay — and then
        arms the idle timer that eventually frees the lamp's slot. The connection
        itself is NOT dropped here, so the next command within the idle window
        skips the multi-second connect. A command that actually errors (a dead
        link, not a mere unconfirmed Status) tears the connection down so the next
        call reconnects fresh. When ``call`` is ``None`` this is a pure
        reachability check. Returns the command's result, or ``None``.
        """
        async with self._lock:
            self._cancel_idle()
            controller = await self._ensure_connected()
            if controller is None:
                return None

            result = None
            try:
                if call is not None:
                    async with asyncio.timeout(COMMAND_TIMEOUT):
                        result = await call(controller)
            except Exception as exc:  # noqa: BLE001 - dead link / command timeout
                # A raised error (not a mere unconfirmed Status — those return
                # None without raising) means the link is likely bad: drop it so
                # the next command reconnects rather than reusing a stale handle.
                logger.debug("mesh command failed on held link: %s", exc)
                await self._teardown()
            finally:
                # Persist whatever SEQ/TID the controller consumed, even on a
                # failure, so a later command never reuses a SEQ (dropped as a
                # replay) nor a TID (dropped as a retransmit).
                self._seq = controller.seq
                self._tid = controller.tid
                try:
                    await self._store.async_save({"seq": self._seq})
                except Exception:  # noqa: BLE001
                    logger.debug("seq persist failed", exc_info=True)

            # Hold the link briefly for the next command; freed on idle timeout.
            if self._controller is not None:
                self._arm_idle()
            return result

    # ------------------------------------------------------- connection teardown

    async def _teardown(self) -> None:
        """Stop the controller and disconnect the held client; clear both refs.

        Idempotent and best-effort — teardown failures are logged, not raised —
        so it is safe from the idle timer, an error path, or shutdown. Callers
        hold :attr:`_lock` (except the idle callback, which takes it first).
        """
        controller, client = self._controller, self._client
        self._controller = self._client = None
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

    def _arm_idle(self) -> None:
        """(Re)start the idle timer that drops the held connection.

        A non-positive :attr:`_idle_timeout` means keep-alive is permanent — the
        connection is never dropped for inactivity (only on stop or a dead link).
        """
        self._cancel_idle()
        if self._stopped or self._controller is None or self._idle_timeout <= 0:
            return
        self._idle_unsub = async_call_later(
            self.hass, self._idle_timeout, self._idle_callback
        )

    def _cancel_idle(self) -> None:
        if self._idle_unsub is not None:
            self._idle_unsub()
            self._idle_unsub = None

    async def _idle_callback(self, _now) -> None:
        self._idle_unsub = None
        async with self._lock:
            await self._teardown()

    # --------------------------------------------------------------- commands

    async def async_set_onoff(self, unicast: int, on: bool) -> bool | None:
        """Set Generic OnOff on ``unicast`` (fire-and-forget; short status wait)."""
        return await self._run_connected(
            lambda c: c.set_onoff(
                unicast, on, timeout=STATUS_TIMEOUT, retries=0
            )
        )

    async def async_get_onoff(self, unicast: int) -> bool | None:
        """Read Generic OnOff from ``unicast``; None if unconfirmed."""
        return await self._run_connected(
            lambda c: c.get_onoff(unicast, timeout=STATUS_TIMEOUT)
        )

    async def async_get_lightness(self, unicast: int) -> int | None:
        """Read Light Lightness (0..0xFFFF) from ``unicast``; None if unconfirmed."""
        return await self._run_connected(
            lambda c: c.get_lightness(unicast, timeout=STATUS_TIMEOUT)
        )

    async def async_set_lightness(
        self, unicast: int, level_0_1: float
    ) -> int | None:
        """Set Light Lightness (0..1) on ``unicast`` (fire-and-forget)."""
        return await self._run_connected(
            lambda c: c.set_lightness(unicast, level_0_1, timeout=STATUS_TIMEOUT)
        )

    async def async_set_ctl(
        self, unicast: int, level_0_1: float, kelvin: int
    ) -> int | None:
        """Set Light CTL (lightness + temperature) on ``unicast`` (fire-and-forget)."""
        return await self._run_connected(
            lambda c: c.set_ctl(
                unicast, level_0_1, kelvin, timeout=STATUS_TIMEOUT
            )
        )

    async def async_set_ctl_temperature(
        self, unicast: int, kelvin: int
    ) -> int | None:
        """Set Light CTL Temperature only (K) on ``unicast`` (fire-and-forget)."""
        return await self._run_connected(
            lambda c: c.set_ctl_temperature(
                unicast, kelvin, timeout=STATUS_TIMEOUT
            )
        )

    # ---------------------------------------------------------------- probe

    async def _async_probe(self, now=None) -> None:
        """Reachability check: bring the proxy connection up, then release it.

        Availability comes purely from whether the proxy link can be established
        (no GET — the node does not forward a Status back). Unlike a real command
        this does NOT hold the connection: if we were not already connected it
        connects, records availability, and disconnects immediately so a
        background recovery check never keeps the lamp's slot. If a command
        connection is already held we are plainly available and do nothing.
        """
        if self._stopped:
            return
        async with self._lock:
            if self._controller is not None:
                return  # a held command connection already proves reachability
            controller = await self._ensure_connected()
            if controller is not None:
                # Probe only — hand the slot straight back to the vendor app.
                await self._teardown()

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
        """Count a miss; flip to unavailable only once misses are sustained.

        A single-slot mesh lamp is frequently busy for a moment (a probe lands
        while the app or the node's own housekeeping holds the slot). Flipping
        the entity to unavailable on the first miss would make it un-clickable
        far too often, so we keep it available through a few transient misses
        and only give up — marking it unavailable and raising the repair — once
        the miss count reaches the threshold.
        """
        self._fail_count += 1
        if self._fail_count >= UNREACHABLE_THRESHOLD:
            self._available = False
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
