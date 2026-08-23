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

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .btmesh.controller import MeshController
from .btmesh.crypto import k3
from .btmesh.network_model import Network
from .const import (
    CONF_CONNECT_JSON,
    CONF_KEEPALIVE,
    CONTROLLED_MODEL_IDS,
    DEFAULT_KEEPALIVE,
    DOMAIN,
)
from .mesh_transport import (
    async_connect_bearer,
    async_register_proxy_callback,
    discovered_proxies,
    find_proxy_address,
)

logger = logging.getLogger(__name__)

__all__ = ["MeshCoordinator"]

# Preferred source address for the traffic we originate. 0x7FFF is the top of
# the unicast range, as far as possible from the addresses a provisioner hands
# out (it allocates upwards). It is only a PREFERENCE: an export that already
# gives it to a node makes us step down (see Network.free_unicast), because
# sharing a unicast with a real node means that node's peers already hold a
# replay-protection entry for it and discard everything we send.
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

# The cursor is written through Home Assistant's debounced Store rather than on
# every command: one flash write per button press wears out an SD card for no
# benefit. Anything the debounce loses to a crash is covered by the margin
# above — at one or two SEQ per command, this window cannot burn 32.
SEQ_SAVE_DELAY = 10.0

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

# How long a command waits for the node's Status reply before giving up.
# ``MeshController.start`` now configures the proxy's address filter, so Status
# replies DO come back (a round trip is well under a second) and the confirmed
# value is normally what we cache. It stays short anyway: control is
# fire-and-forget — the Set reaches the node on the wire regardless — so a proxy
# or node that stays silent must not add latency to every button press. Past the
# window the command is simply optimistic, exactly as before.
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
        # Where our traffic comes FROM, and which key it is encrypted WITH —
        # both derived from the export rather than assumed, because getting
        # either wrong fails in complete silence (see the two helpers below).
        self._src_addr = self._resolve_src_addr()
        self._app_key = self._resolve_app_key()
        self._seq = 0
        # The IV Index we encrypt with. The .connect export states it as it was
        # at export time and the mesh moves on without telling the file, so the
        # value discovered from a Secure Network Beacon is persisted and wins
        # (see _adopt_iv_index).
        self._iv_index = self._network.iv_index
        # Persistent TID cursor: carried across on-demand controllers so
        # consecutive Set messages (ON then OFF) never collide on the same TID
        # and get dropped by the node as a retransmit. In-memory is enough — the
        # node's dedup window is seconds, far shorter than a restart.
        self._tid = 0
        self._available = False
        # Last authenticated Secure Network Beacon seen, kept for diagnostics:
        # "does the subnet beacon, and do our keys verify it" answers most
        # support questions in one look.
        self._beacon = None
        # A subnet that never beacons leaves the IV Index unverifiable; say so
        # once rather than on every command (see _adopt_iv_index).
        self._beacon_warned = False
        self._stopped = False
        self._fail_count = 0
        self._issue_active = False
        self._probe_unsub: CALLBACK_TYPE | None = None
        self._discovery_unsub: CALLBACK_TYPE | None = None
        # Entities subscribed to availability transitions (see async_add_listener).
        self._listeners: list[CALLBACK_TYPE] = []
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

    # ------------------------------------------------ derived from the export

    def _resolve_src_addr(self) -> int:
        """The unicast we transmit from: :data:`SRC_ADDR` unless it is taken.

        A collision here is the worst kind of bug this integration can have:
        everything looks healthy — the proxy connects, the frames go out — and
        the lamp simply never reacts, because its peers drop our messages as
        replays of an address they already know. Nothing in any log says so.
        """
        src_addr = self._network.free_unicast(SRC_ADDR)
        if src_addr != SRC_ADDR:
            logger.info(
                "%#06x already belongs to a node of this network; "
                "transmitting from %#06x instead",
                SRC_ADDR, src_addr,
            )
        return src_addr

    def _resolve_app_key(self):
        """The AppKey the models we drive are actually bound to.

        A node matches an incoming message's AID against the keys each of its
        models was bound to and discards anything else at the upper transport
        layer — no error, no Status, no physical effect. Taking the export's
        first key on faith is therefore a silent failure waiting for a network
        that holds more than one.
        """
        app_key = self._network.app_key_for_models(CONTROLLED_MODEL_IDS)
        wanted = self._network.bound_app_key_indexes(CONTROLLED_MODEL_IDS)
        held = {key.index for key in self._network.app_keys}
        if wanted and not held.intersection(wanted):
            logger.warning(
                "the lighting models of this network bind AppKey index(es) %s, "
                "but the export only carries %s; falling back to index %d — "
                "commands are likely to be ignored by the nodes",
                ", ".join(str(index) for index in wanted),
                ", ".join(str(index) for index in sorted(held)) or "none",
                app_key.index,
            )
        elif app_key.index != self._network.default_app_key.index:
            logger.info(
                "using AppKey index %d (bound by the lighting models) rather "
                "than the export's first key, index %d",
                app_key.index, self._network.default_app_key.index,
            )
        return app_key

    @property
    def src_addr(self) -> int:
        """The unicast address this integration transmits from."""
        return self._src_addr

    @property
    def app_key_index(self) -> int:
        """The AppKey Index the commands are encrypted with."""
        return self._app_key.index

    # -------------------------------------------------------------- listeners

    def async_add_listener(self, update_callback: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Subscribe to availability changes; returns the unsubscribe callable.

        Entities read :attr:`available` directly, so without this they would
        only notice a change on Home Assistant's next entity poll. It also tells
        them when the mesh comes BACK, which is the moment to re-read a lamp
        whose state may have been changed from the vendor app meanwhile.
        """
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def _notify_listeners(self) -> None:
        """Fire every listener; one raising must not starve the others."""
        for update_callback in list(self._listeners):
            try:
                update_callback()
            except Exception:  # noqa: BLE001
                logger.exception("coordinator listener raised")

    # ------------------------------------------------------------- properties

    @property
    def network(self) -> Network:
        """The parsed static network model (entities enumerate its nodes)."""
        return self._network

    @property
    def available(self) -> bool:
        """True while the most recent connect/command/probe succeeded."""
        return self._available

    @property
    def connected(self) -> bool:
        """True while a proxy connection is held open."""
        return self._controller is not None

    @property
    def beacon(self):
        """The last authenticated Secure Network Beacon, or ``None``."""
        return self._beacon

    @property
    def iv_index(self) -> int:
        """The IV Index in use (persisted, and adopted from the subnet)."""
        return self._iv_index

    @property
    def seq(self) -> int:
        """The persisted sequence cursor."""
        return self._seq

    @property
    def keepalive_seconds(self) -> int:
        """How long the proxy link is held after the last command (0 = always)."""
        return self._idle_timeout

    # -------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Seed the SEQ cursor once, probe once, then self-schedule probes.

        Never hard-fails: if no proxy is reachable the entry still sets up and
        the coordinator retries quickly in the background until a probe or
        command succeeds. The SEQ safety margin is applied here exactly once.
        """
        self._stopped = False
        self._seq, self._iv_index = await self._load_state()
        # Probe in the BACKGROUND. Awaiting a full connect here — up to
        # CONNECT_TIMEOUT plus bleak's retries — happens inside
        # async_setup_entry, well past the 10s mark where Home Assistant starts
        # warning that an integration is slow to set up, and it delays every
        # other integration behind it. Availability simply arrives a moment
        # later; entities are created unavailable and told when it lands.
        self.hass.async_create_background_task(
            self._async_probe(), f"{DOMAIN} initial probe"
        )
        self._schedule_probe()
        # Push discovery: recover the moment a matching proxy advertises again
        # instead of waiting out the retry tick.
        self._discovery_unsub = async_register_proxy_callback(
            self.hass, self._network.net_key, self._on_proxy_seen
        )

    async def async_stop(self) -> None:
        """Cancel timers, drop the held connection, and clear the repair issue."""
        self._stopped = True
        self._cancel_probe()
        self._cancel_idle()
        if self._discovery_unsub is not None:
            self._discovery_unsub()
            self._discovery_unsub = None
        async with self._lock:
            await self._teardown()
        # Flush the debounced cursor now: the entry is going away, and a SEQ
        # that never reached disk is one the mesh will later drop as a replay.
        await self._flush_state()
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

    @callback
    def _on_proxy_seen(self, address: str) -> None:
        """A proxy for our network just advertised — try again straight away.

        Only while we believe we are unreachable: adverts arrive constantly,
        and probing on each one would take the lamp's single proxy slot for
        nothing.
        """
        if self._stopped or self._available:
            return
        logger.debug("mesh proxy %s advertised; probing now", address)
        self.hass.async_create_background_task(
            self._async_probe(), f"{DOMAIN} discovery probe"
        )

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
            if (
                getattr(self._client, "is_connected", True)
                and not self._controller.failed
            ):
                return self._controller
            # The held link died under us — the GATT link dropped, or the TX
            # pump died on a write and can no longer transmit (which commands
            # cannot report by raising). Drop it and reconnect below.
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

        # Bound before the try: from the moment async_connect_bearer returns, the
        # client holds the lamp's single proxy slot even if everything after it
        # fails. It is not reachable through self._client until the controller is
        # up, so _teardown() cannot free it — this local reference is the only
        # way back to it, and leaking it would lock out both HA and the app.
        client = None
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                client, bearer = await async_connect_bearer(self.hass, address)
                controller = MeshController(
                    self._network, bearer, src_addr=self._src_addr,
                    seq=self._seq, tid=self._tid, iv_index=self._iv_index,
                    app_key=self._app_key.key,
                )
                await controller.start()
        except Exception as exc:  # noqa: BLE001 - transport/GATT/connect
            logger.debug("mesh connect failed: %s", exc)
            await self._disconnect(client)
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
                # Must run BEFORE persisting: adopting a new IV Index restarts
                # the SEQ cursor, and that pair has to be stored together.
                stale_iv_index = self._adopt_iv_index(controller)
                self._persist()

            if stale_iv_index:
                # The live link still encrypts with the old index; drop it so
                # the next command rebuilds a controller on the new one.
                await self._teardown()

            # A dead TX pump does not raise — the command just times out like an
            # unconfirmed Status — so check explicitly and drop the link, else
            # we would hold a controller that can never transmit again.
            if self._controller is not None and self._controller.failed:
                logger.debug("mesh transport died; dropping the held link")
                await self._teardown()

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
        await self._disconnect(client)

    @staticmethod
    async def _disconnect(client) -> None:
        """Best-effort disconnect: freeing the proxy slot must never raise."""
        if client is None:
            return
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
        (no GET — bringing the link up already proves the node is reachable, and
        a GET would cost a round trip for nothing). Unlike a real command
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

    async def _load_state(self) -> tuple[int, int]:
        """Stored (SEQ + safety margin, IV Index); the margin is applied once.

        A fresh install has neither, so the SEQ starts at 0 and the IV Index
        falls back to whatever the ``.connect`` export claimed.
        """
        data = await self._store.async_load()
        if not data:
            return 0, self._network.iv_index
        return (
            int(data.get("seq", 0)) + SEQ_SAFETY_MARGIN,
            int(data.get("iv_index", self._network.iv_index)),
        )

    def _state_to_save(self) -> dict[str, int]:
        return {"seq": self._seq, "iv_index": self._iv_index}

    def _persist(self) -> None:
        """Queue a debounced write of the SEQ cursor and IV Index."""
        self._store.async_delay_save(self._state_to_save, SEQ_SAVE_DELAY)

    async def _flush_state(self) -> None:
        """Write the cursor out now (best-effort), bypassing the debounce."""
        try:
            await self._store.async_save(self._state_to_save())
        except Exception:  # noqa: BLE001
            logger.debug("state persist failed", exc_info=True)

    def _adopt_iv_index(self, controller) -> bool:
        """Take the subnet's announced IV Index; True when it changed.

        A stale IV Index is fatal in silence: the mesh drops every PDU we send
        and we reject every PDU we receive on the IVI check, with nothing to
        show for it. The subnet announces the truth in an authenticated Secure
        Network Beacon on each connection, so adopt it — and restart the SEQ
        cursor, which is only required to be unique *within* an IV Index.
        """
        beacon = getattr(controller, "beacon", None)
        if beacon is None:
            if not self._beacon_warned:
                self._beacon_warned = True
                logger.warning(
                    "no Secure Network Beacon received from this subnet: the IV "
                    "Index %#x comes from the .connect export, which does not "
                    "carry one, and cannot be confirmed. If the mesh has moved "
                    "past it, every message sent is discarded in silence",
                    self._iv_index,
                )
            return False
        self._beacon = beacon
        if beacon.iv_index == self._iv_index:
            return False
        logger.warning(
            "adopting IV Index %#x announced by the subnet (was %#x); "
            "restarting the SEQ cursor",
            beacon.iv_index, self._iv_index,
        )
        self._iv_index = beacon.iv_index
        self._seq = 0
        return True

    # --------------------------------------------------------------- availability

    def _set_available(self) -> None:
        """Mark reachable: clear the fail count and any active repair issue.

        Only an actual transition notifies listeners — this runs on every
        successful connect, and re-reading every lamp each time would churn the
        single proxy slot for nothing.
        """
        was_available = self._available
        self._available = True
        self._fail_count = 0
        self._clear_issue()
        if not was_available:
            self._notify_listeners()

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
            was_available = self._available
            self._available = False
            self._raise_proxy_issue()
            if was_available:
                self._notify_listeners()

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
