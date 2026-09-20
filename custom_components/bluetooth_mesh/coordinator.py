"""Runtime coordinator for the Bluetooth Mesh integration (Task B3).

This is the single long-lived object per config entry. It owns the parsed
:class:`btmesh.network_model.Network`, and drives mesh commands through the
HA-bluetooth bridge (:mod:`.mesh_transport`) over one **held** proxy
connection, serialised by a single lock.

A mesh node offers a *single* proxy connection slot, shared with the vendor
app, so how long the link is held is the user's choice (the keep-alive option).
``0``, the default, keeps it for good and re-establishes it when it drops: every
command is then instant. A positive value hands the slot back after that many
idle seconds, which leaves room for the vendor app at the price of a connect of
several seconds on the next command.

Three cross-cutting concerns live here rather than in the entities:

* **Availability + a periodic probe.** Bringing the proxy link up is the
  availability signal; no GET is involved. While unavailable, or while a
  permanent link is missing, a probe retries on a timer and on every matching
  0x1828 advert, behind a backoff that widens after each failed GATT connect.
  Misses flip availability only once ``UNREACHABLE_THRESHOLD`` of them pile up.
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
from time import monotonic

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .btmesh.controller import MeshController
from .btmesh.crypto import k3
from .btmesh.network_model import UNICAST_MAX, UNICAST_MIN, Network
from .const import (
    CONF_CONNECT_JSON,
    CONF_KEEPALIVE,
    CONF_SRC_ADDR,
    CONTROLLED_MODEL_IDS,
    DEFAULT_KEEPALIVE,
    DEFAULT_SRC_ADDR,
    DOMAIN,
)
from .mesh_transport import (
    PROXY_ADVERT_MAX_AGE,
    ProxyPath,
    async_connect_bearer,
    async_register_proxy_callback,
    connect_paths,
    discovered_proxies,
    find_proxy_address,
    scanner_by_source,
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
# benefit. Anything the debounce loses to a crash has to stay under the margin
# above, and the debounce alone does not guarantee that: every new call pushes
# the pending write back, so a burst with less than this delay between commands
# (eight CTL lamps re-read after a reconnect is forty GETs) wrote nothing at all
# until it was over. `_persist` therefore writes at once whenever the cursor is
# half a margin ahead of what is on disk.
SEQ_SAVE_DELAY = 10.0

# How often to probe the mesh for availability. We no longer hold a connection,
# so this light churn is the only time we take the lamp's single slot; the rest
# of the time it is free for the vendor app. The interval is adaptive: when the
# lamp is reachable we probe rarely (coexistence with the app); when it is NOT,
# we retry quickly to reconnect fast at startup and after a drop (like the
# daikin_madoka integration's short retry).
PROBE_INTERVAL_AVAILABLE = timedelta(minutes=2)
PROBE_INTERVAL_UNAVAILABLE = timedelta(seconds=15)

# A node that is hammered never recovers. 2026-09-10, David's network: a reload
# of the entry dropped the held link, and every reconnect path -- push discovery
# on each 0x1828 advert, the 15 s probe, the drop watchdog -- fired the moment
# the lock was free, each one four GATT connects. Forty minutes and 61 failed
# connects later (BlueSight: `kind: storm`) the node still refused everyone, the
# vendor app included; 130 s of radio silence, and the next connect took 8 s.
# The node was never wedged, it never had a quiet moment -- the BRC1H pairing
# storm in daikin_madoka is the same mechanism. So consecutive GATT failures
# widen the wait before the next attempt, doubling from the retry interval up
# to the cap, and every path honours that wait. A miss that never reached the
# radio (no connectable proxy advertised) does not count: it put no pressure on
# the node, and the advert that ends it is the one to act on at once. While
# backing off each attempt is a single GATT try: bleak-retry-connector's four
# are four times the pressure, on a node that needs less. Since
# ha-bluetooth-mesh#31 an AUTOMATIC try is a single one whether backing off or
# not: on 2026-09-12 the first reconnect after a drop, which no failure had
# preceded, spent its four on a node that had been unplugged, and habluetooth
# charged all four to the proxy that heard it best. Two paths are not gated on
# purpose: the drop watchdog, because a drop follows a success, which cleared
# the wait (if its reconnect fails, the wait is back); and a command from the
# user, which is a deliberate act -- while backing off it gets one try, and a
# failure widens the wait like any other.
CONNECT_BACKOFF_BASE = PROBE_INTERVAL_UNAVAILABLE
CONNECT_BACKOFF_CAP = timedelta(minutes=5)
# bleak-retry-connector's own default, spelled out because the coordinator
# passes one or the other explicitly.
CONNECT_ATTEMPTS = 4

# A proxy can wedge on one address: it answers every connect with an instant
# GATT error while it has a free slot and hears the node perfectly, and only a
# restart of that proxy clears it. Seen on 2026-09-08, 2026-09-12 and again on
# 2026-09-20, where it was provoked on purpose -- unplug the lamp while the
# proxy holds a connection to it, and every attempt after it comes back fails
# in about a second. Nothing here can unwedge it, so the repair names the proxy
# and says to restart it; that is what the three outages each cost an hour to
# work out by hand. Its signature, all four at once:
#   * every attempt failed against the SAME source,
#   * each in under this many seconds (a refusal, not a timeout or a search),
#   * that source hears the node and has a slot free,
#   * and it has happened this many times in a row.
STUCK_PROXY_FAST_FAILURE = 3.0
STUCK_PROXY_FAILURES = 3

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

# How long the diagnostics composition probe waits for a node's Config Server.
# Longer than STATUS_TIMEOUT because a Composition Data Status is segmented, but
# still short: the probe runs once per node inside a diagnostics download, and a
# silent node is itself the answer we are after.
PROBE_TIMEOUT = 3.0

# Default seconds to HOLD the proxy connection open after the last command
# before dropping it to free the lamp's single proxy slot. Opening a proxy
# connection over an ESPHome BLE proxy costs several seconds, so holding it makes
# a burst of commands feel instant instead of paying that cost every time. This
# is the fallback when the config entry has no explicit keep-alive option;
# ``0`` (the shipped default) keeps the connection always open. Overridable per
# entry via the options flow (:data:`.const.CONF_KEEPALIVE`). The default
# itself is :data:`.const.DEFAULT_KEEPALIVE`.


class MeshCoordinator:
    """Own one mesh subnet's network model and its proxy link for an entry.

    Construct it, ``await async_start()`` in ``async_setup_entry``, and
    ``await async_stop()`` in ``async_unload_entry``. The command coroutines are
    best-effort: they return ``None`` when the mesh is unavailable or a command
    times out, never raising to the caller. Once stopped it never connects
    again, whatever is still queued on its lock.
    """

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self._network = Network.from_connect(
            json.loads(entry.data[CONF_CONNECT_JSON])
        )
        # Keyed on the NETWORK, not on the config entry. The nodes remember the
        # highest SEQ they accepted from our address, and they do not forget it
        # when the integration is removed: keyed on the entry id, removing and
        # re-adding the integration (the first thing anyone tries when something
        # is wrong) restarted the cursor at 0, and every command was dropped as
        # a replay, in silence, until it had climbed back past the old value.
        # The file is left behind on removal for the same reason.
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{self._network.identifier.lower()}.seq"
        )
        # Where the cursor lived up to v0.9.0; read once, then deleted.
        self._legacy_store: Store = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seq"
        )
        self._saved_seq = 0
        self._iv_regress_warned = False
        # Consecutive fast refusals, and the source they all came through (see
        # STUCK_PROXY_FAILURES). The scanner object is kept, not just its
        # address: a proxy that restarts registers a NEW one, which is how we
        # notice it happened and stop accusing it.
        self._refusals = 0
        self._refused_path: ProxyPath | None = None
        self._refused_scanner: object | None = None
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
        # BLE address of the proxy node the held link runs through, or None
        # while nothing is connected. Published by the sensor platform, which
        # writes it onto the device as a `connections` entry: the GATT link
        # occupies a connection slot on whichever ESPHome proxy routes it, and
        # without an address on a device nothing outside this integration can
        # tell that slot apart from a stuck one.
        self._proxy_address: str | None = None
        # Last authenticated Secure Network Beacon seen, kept for diagnostics:
        # "does the subnet beacon, and do our keys verify it" answers most
        # support questions in one look.
        self._beacon = None
        # A subnet that never beacons leaves the IV Index unverifiable; say so
        # once rather than on every command (see _adopt_iv_index).
        self._beacon_warned = False
        self._stopped = False
        self._fail_count = 0
        # Current wait after consecutive GATT failures (0 = none) and the
        # monotonic instant before which no automatic connect may start.
        self._backoff = 0.0
        self._next_attempt = 0.0
        # Monotonic instant our last link ended (None = never held one). A node
        # is silent while its slot is held, so until it has had time to
        # advertise again an old advert says nothing about it.
        self._link_ended_at: float | None = None
        # Which repair is on screen, so a plain outage that turns out to be
        # a wedged proxy replaces it instead of being swallowed as "already up".
        self._issue_kind: str | None = None
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
            entry.options.get(CONF_KEEPALIVE, DEFAULT_KEEPALIVE)
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
        configured = self.entry.options.get(CONF_SRC_ADDR, DEFAULT_SRC_ADDR)
        if configured:
            chosen = self._configured_src_addr(int(configured))
            if chosen is not None:
                logger.info("transmitting from the configured %#06x", chosen)
                return chosen
        src_addr = self._network.free_unicast(SRC_ADDR)
        if src_addr != SRC_ADDR:
            logger.info(
                "%#06x already belongs to a node of this network; "
                "transmitting from %#06x instead",
                SRC_ADDR, src_addr,
            )
        return src_addr

    def _configured_src_addr(self, configured: int) -> int | None:
        """Validate an explicitly configured source address, else ``None``.

        Refused rather than honoured when it is not a unicast, or when a node of
        the imported network already owns it — that address is unusable no
        matter what the user meant, and taking it would mute the integration in
        exactly the way this option exists to escape.
        """
        if not UNICAST_MIN <= configured <= UNICAST_MAX:
            logger.warning(
                "configured source address %#06x is not a unicast address "
                "(%#06x..%#06x); deriving one instead",
                configured, UNICAST_MIN, UNICAST_MAX,
            )
            return None
        if configured in self._network.unicast_addresses():
            logger.warning(
                "configured source address %#06x already belongs to a node of "
                "this network; deriving one instead",
                configured,
            )
            return None
        return configured

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
    def proxy_address(self) -> str | None:
        """BLE address of the proxy node the link runs through, if connected.

        Deliberately NOT cleared on teardown. The address is what the device's
        `connections` entry is built from, and a device that drops its BLE
        address whenever the link is idle would be unresolvable exactly when
        somebody is asking who holds the slot. It is replaced when a connect
        lands somewhere else, which is the only moment it becomes wrong.
        """
        return self._proxy_address

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
        # Put the margin on disk before anything is sent. Until the first write
        # the file still holds the OLD cursor, so a crash in that window made
        # the next start land on this same value again and reuse whatever had
        # gone out in between. It also moves a cursor read from the legacy file
        # under its new key.
        await self._flush_state()
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

    def _wants_link(self) -> bool:
        """True when keep-alive 0 promised a held link and there is none.

        Availability is deliberately sticky — a miss only flips it after
        ``UNREACHABLE_THRESHOLD`` in a row, because a single-slot lamp is often
        busy for a moment. That hysteresis was built for probes, and it defeated
        recovery on 2026-09-04: the held link dropped at 09:50, the watchdog's
        one reconnect found no advert yet, the miss left ``_available`` True,
        and both recovery paths (periodic probe, push discovery) only act while
        unavailable. Nobody reconnected for ten hours. A missing permanent link
        is its own reason to recover, whatever the availability flag says.
        """
        return (
            not self._stopped
            and self._idle_timeout <= 0
            and self._controller is None
        )

    def _may_attempt(self) -> bool:
        """True once the wait imposed by the last GATT failure has passed."""
        return monotonic() >= self._next_attempt

    def _back_off(self) -> None:
        """Widen the wait after a GATT failure: base, then doubling, to the cap.

        One line per step at info, so the log tells the climb; at the cap the
        wait no longer changes and the line drops to debug.
        """
        previous = self._backoff
        base = CONNECT_BACKOFF_BASE.total_seconds()
        cap = CONNECT_BACKOFF_CAP.total_seconds()
        self._backoff = base if previous == 0 else min(previous * 2, cap)
        self._next_attempt = monotonic() + self._backoff
        logger.log(
            logging.INFO if self._backoff > previous else logging.DEBUG,
            "mesh proxy backoff: next connect attempt in %d s "
            "(%d consecutive failures)",
            int(self._backoff),
            self._fail_count,
        )

    def _schedule_probe(self) -> None:
        """Arm the next probe: fast while unavailable, slow while reachable.

        Fast, but never inside the backoff: a retry tick that undercut the wait
        would be the storm again, on a timer instead of on adverts.
        """
        if self._stopped or self._probe_unsub is not None:
            return
        if not self._available or self._wants_link():
            delay = max(
                PROBE_INTERVAL_UNAVAILABLE.total_seconds(),
                self._next_attempt - monotonic(),
            )
        else:
            delay = PROBE_INTERVAL_AVAILABLE.total_seconds()
        self._probe_unsub = async_call_later(
            self.hass, delay, self._probe_callback
        )

    def _cancel_probe(self) -> None:
        if self._probe_unsub is not None:
            self._probe_unsub()
            self._probe_unsub = None

    @callback
    def _on_proxy_seen(self, address: str) -> None:
        """A proxy for our network just advertised — try again straight away.

        Only while we believe we are unreachable, and never inside the
        backoff: adverts arrive several times a second, and until 2026-09-10
        each one started a connect the moment the lock was free -- this was
        the dominant path of the storm, the one that made the fixed probe
        interval irrelevant.
        """
        if self._stopped or (self._available and not self._wants_link()):
            return
        if self._lock.locked():
            return  # a connect is already in flight; adverts arrive constantly
        self._proxy_restarted()  # its refusals are what imposed the wait
        if not self._may_attempt():
            return  # the node is being left alone on purpose
        logger.debug("mesh proxy %s advertised; probing now", address)
        self.hass.async_create_background_task(
            self._async_probe(), f"{DOMAIN} discovery probe"
        )

    async def _probe_callback(self, _now) -> None:
        self._probe_unsub = None
        # Only probe to RECOVER when we believe we are unavailable; while
        # available we rely on real commands + the held connection, so we never
        # churn the lamp's slot behind the user's back.
        if (not self._available or self._wants_link()) and self._may_attempt():
            await self._async_probe()
        self._schedule_probe()

    # ------------------------------------------------------- keep-alive core

    async def _ensure_connected(
        self, *, automatic: bool = False
    ) -> "MeshController | None":
        """Return a live controller, reusing the held connection or opening one.

        Opening a proxy connection over an ESPHome BLE proxy costs several
        seconds, so the connection is kept open between commands (see
        :data:`.const.CONF_KEEPALIVE`) and simply reused here when still alive.
        **Bringing the connection up is the availability signal** — the mesh node
        is on the other end of the proxy link, so control will reach it; we do
        not depend on a Status reply for availability. Returns ``None`` (and marks
        unavailable) when no proxy is reachable or the connect fails. Callers hold
        :attr:`_lock`.

        ``automatic`` marks a call from the background recovery machinery (a
        probe, or the reconnect after a dropped link) rather than a command.
        bleak-retry-connector's own retry budget already tries several times
        inside ONE call, and for an automatic try that is pressure of its own:
        several failures charged to whatever proxy habluetooth scores best, in
        the same second, on a node that may simply not be there
        (ha-bluetooth-mesh#31). Such a try is a single attempt. A command keeps
        the full budget, except while backing off, where everything is a single
        attempt (see :data:`CONNECT_BACKOFF_BASE`). "Command" is every caller of
        :meth:`_run_connected`, the lights' own state reads included: they are
        not told apart, which is one more reason the backoff rule covers them.
        """
        if self._stopped:
            # A command queued on the lock while the entry was unloading gets
            # here after async_stop released the link. Connecting now would hand
            # the node's single slot to an object nobody will ever stop again:
            # its idle timer refuses to arm and its drop handler stands down,
            # both on this same flag, and the entry's next coordinator finds the
            # slot taken.
            return None
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

        address = find_proxy_address(
            self.hass,
            self._network.net_key,
            max_age=None if self._held_the_slot_recently() else PROXY_ADVERT_MAX_AGE,
        )
        if address is None:
            seen = discovered_proxies(self.hass)
            self._set_unavailable()
            # A miss here is routine while the lamp is simply unplugged or out
            # of range, and the probe carries on for as long as that lasts: on
            # 2026-09-12, validating v0.7.0 with the lamp unplugged, this line
            # wrote 49 warnings in twelve minutes (07:41-07:53). Only the miss
            # that crosses UNREACHABLE_THRESHOLD is worth one -- it is the miss
            # that takes the integration unavailable -- exactly as for the
            # connect failure below. `_fail_count` is cleared only by a
            # successful connect, so that is once per outage; the misses either
            # side keep the advert diagnostic at debug for whoever needs it.
            logger.log(
                logging.WARNING
                if self._fail_count == UNREACHABLE_THRESHOLD
                else logging.DEBUG,
                "no connectable mesh proxy for network_id %s; "
                "0x1828 adverts HA sees: %s",
                k3(self._network.net_key).hex(),
                seen or "none",
            )
            return None

        # Bound before the try: from the moment async_connect_bearer returns, the
        # client holds the lamp's single proxy slot even if everything after it
        # fails. It is not reachable through self._client until the controller is
        # up, so _teardown() cannot free it — this local reference is the only
        # way back to it, and leaking it would lock out both HA and the app.
        client = None
        started = monotonic()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                client, bearer = await async_connect_bearer(
                    self.hass,
                    address,
                    max_attempts=(
                        1 if automatic or self._backoff else CONNECT_ATTEMPTS
                    ),
                )
                controller = MeshController(
                    self._network, bearer, src_addr=self._src_addr,
                    seq=self._seq, tid=self._tid, iv_index=self._iv_index,
                    app_key=self._app_key.key,
                )
                await controller.start()
        except Exception as exc:  # noqa: BLE001 - transport/GATT/connect
            await self._disconnect(client)
            # Before _set_unavailable: that one raises the repair, and which
            # repair to raise depends on what this failure adds to the streak.
            self._note_refusal(address, monotonic() - started)
            self._set_unavailable()
            self._back_off()
            # `asyncio.timeout` above raises a TimeoutError whose str() is the
            # empty string, so logging the message alone printed "mesh connect
            # failed:" and nothing after it -- blanking out the one failure this
            # line exists to diagnose. The type always names something; the
            # message is appended only when it says something too.
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            # A miss is routine on a single-slot lamp (the vendor app, or the
            # node's own housekeeping, holds the slot for a moment), which is why
            # `_set_unavailable` waits for UNREACHABLE_THRESHOLD of them. The one
            # that crosses it is not debug material: it takes the integration
            # unavailable, like the "no connectable proxy" miss above. Only a
            # successful connect clears `_fail_count`, so this is true exactly
            # once per outage -- probing carries on while we are down, and a
            # warning per retry would bury the first one.
            if self._fail_count == UNREACHABLE_THRESHOLD:
                logger.warning("mesh connect failed: %s", detail)
            else:
                logger.debug("mesh connect failed: %s", detail)
            return None

        self._client = client
        self._controller = controller
        # start() has already spent SEQ numbers: claiming the proxy filter is
        # two network PDUs. Until now the cursor only came back after a command,
        # so a link that carried none (a probe that hands the slot back, a
        # reconnect after a drop) left it where it was, and the next controller
        # sent its own filter setup under the same two numbers, to the same
        # node, which is entitled to drop them as replays.
        self._seq = controller.seq
        self._persist()
        # Learn about a drop when it happens, not at the next click. bleak
        # fires this on OUR disconnects too; the handler tells them apart by
        # identity, since _teardown clears self._client before disconnecting.
        client.set_disconnected_callback(self._on_client_disconnected)
        if address != self._proxy_address:
            self._proxy_address = address
            # `_set_available` below notifies on a transition, which already
            # covers the first connect. Only an address that changes while we
            # ALREADY believe we are available needs its own notification --
            # without it the published address would keep naming a node we are
            # no longer talking to. Kept conditional because a notification is
            # not free: it makes every lamp re-read itself over the single
            # proxy slot, which is what `_set_available` is careful to avoid.
            if self._available:
                self._notify_listeners()
        self._set_available()
        return controller

    def _held_the_slot_recently(self) -> bool:
        """True while an old advert is our own doing rather than the node's.

        A node stops advertising 0x1828 for as long as its slot is held, so when
        a link of ours ends the newest advert is as old as the link was long.
        Reading that as silence made the reconnect after a drop miss every time
        the link had lasted more than the limit, which is every time that
        matters, and the 2026-09-04 fix (reconnect at once) was gone. A live
        node is back on the air within a second or two; once it has had
        :data:`PROXY_ADVERT_MAX_AGE` to do so, silence means silence again.
        """
        return (
            self._link_ended_at is not None
            and monotonic() - self._link_ended_at < PROXY_ADVERT_MAX_AGE
        )

    async def _run_connected(self, call):
        """Reuse (or open) the held proxy connection and run ``call(controller)``.

        Under the lock it (re)establishes the keep-alive connection, runs the
        best-effort command under :data:`COMMAND_TIMEOUT`, persists the SEQ/TID
        the controller consumed — even on a command error, since the Set was
        already emitted and a reused SEQ would be dropped as a replay — and then
        arms the idle timer that eventually frees the lamp's slot. The connection
        itself is NOT dropped here, so the next command within the idle window
        skips the multi-second connect. A command that actually errors (a dead
        link, not a mere unconfirmed Status) tears the connection down so the next
        call reconnects fresh. Returns the command's result, or ``None``.
        """
        async with self._lock:
            self._cancel_idle()
            controller = await self._ensure_connected()
            if controller is None:
                return None

            result = None
            try:
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

    @callback
    def _on_client_disconnected(self, client) -> None:
        """The held proxy link dropped under us.

        With a timed keep-alive the drop is welcome — the option exists to hand
        the slot back — and the next command's ``is_connected`` check already
        copes. With keep-alive 0 the user asked for *always connected*, and
        until 2026-09-04 that only meant "held until it happened to drop": the
        first command after a silent overnight drop then paid the whole connect
        (11 s that morning, through the proxy habluetooth preferred at -94 dBm).
        Re-establish the link in the background instead.
        """
        if self._stopped or client is not self._client:
            return  # our own teardown, or a client we already replaced
        self._link_ended_at = monotonic()
        if not self.hass.is_running:
            # Home Assistant closes the ESPHome API links in its CLOSE stage —
            # after the final writes, before this entry is unloaded — so the
            # drop lands while _stopped is still False and the core state is
            # already not_running. Seen live on 2026-09-04: four connect
            # attempts ending in "Bluetooth is already shutdown", for a link
            # nobody wants back. Not `is_stopping`: that covers only the two
            # earlier stages and let the 08:43 shutdown through.
            return
        logger.debug("mesh proxy link dropped")
        if self._idle_timeout > 0:
            return
        self.hass.async_create_background_task(
            self._async_reconnect(client), f"{DOMAIN} reconnect after drop"
        )

    async def _async_reconnect(self, client) -> None:
        async with self._lock:
            if self._stopped or client is not self._client:
                return  # a command got there first and already reconnected
            await self._teardown()
            await self._ensure_connected(automatic=True)

    # ------------------------------------------------------- connection teardown

    async def _teardown(self) -> None:
        """Stop the controller and disconnect the held client; clear both refs.

        Idempotent and best-effort — teardown failures are logged, not raised —
        so it is safe from the idle timer, an error path, or shutdown. Callers
        hold :attr:`_lock` (except the idle callback, which takes it first).
        """
        controller, client = self._controller, self._client
        self._controller = self._client = None
        if client is not None and getattr(client, "is_connected", True):
            # Still up, so it ends here. A link that already dropped was
            # stamped by the drop handler, at the time it actually ended.
            self._link_ended_at = monotonic()
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

    async def async_get_relay(self, unicast: int):
        """Read ``unicast``'s Relay state under its device key; None if silent.

        The reachability probe to trust: request and answer both fit in a single
        unsegmented message, so silence here is a real silence rather than a
        segmented reply that never reassembled.
        """
        return await self._run_connected(
            lambda c: c.get_relay(unicast, timeout=PROBE_TIMEOUT)
        )

    async def async_get_composition(self, unicast: int):
        """Read ``unicast``'s Composition Data under its device key; None if silent.

        Device-keyed, so it answers regardless of the AppKey binding or of which
        model owns the light — which is what separates "the message never
        reached the node" from "the node received it and did nothing".
        """
        return await self._run_connected(
            lambda c: c.get_composition(unicast, timeout=PROBE_TIMEOUT)
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

    async def async_get_ctl_temperature(self, unicast: int) -> int | None:
        """Read the settled Light CTL temperature (Kelvin) from ``unicast``."""
        return await self._run_connected(
            lambda c: c.get_ctl_temperature(unicast, timeout=STATUS_TIMEOUT)
        )

    async def async_get_ctl(self, unicast: int) -> int | None:
        """Read temperature via Light CTL, for a node with no 0x1306 element."""
        return await self._run_connected(
            lambda c: c.get_ctl(unicast, timeout=STATUS_TIMEOUT)
        )

    async def async_get_ctl_temperature_range(
        self, unicast: int
    ) -> tuple[int, int] | None:
        """Read the lamp's own Kelvin limits; None if it reports none."""
        return await self._run_connected(
            lambda c: c.get_ctl_temperature_range(unicast, timeout=STATUS_TIMEOUT)
        )

    async def async_set_lightness(
        self, unicast: int, level_0_1: float
    ) -> int | None:
        """Set Light Lightness (0..1) on ``unicast`` (fire-and-forget)."""
        return await self._run_connected(
            lambda c: c.set_lightness(unicast, level_0_1, timeout=STATUS_TIMEOUT)
        )

    async def async_set_group_onoff(self, group_address: int, on: bool) -> None:
        """Set Generic OnOff on a mesh group address in one unacknowledged Set.

        Unlike :meth:`async_set_onoff`, there is no Status to settle on — see
        :meth:`btmesh.controller.MeshController.set_group_onoff` — so this
        always returns ``None``. Callers that need the members' displayed
        state to reflect the change update those entities themselves.
        """
        await self._run_connected(lambda c: c.set_group_onoff(group_address, on))

    async def async_set_group_lightness(
        self, group_address: int, level_0_1: float
    ) -> None:
        """Set Light Lightness (0..1) on a mesh group address, unacknowledged."""
        await self._run_connected(
            lambda c: c.set_group_lightness(group_address, level_0_1)
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

    async def _async_probe(self) -> None:
        """Reachability check: bring the proxy connection up, then release it.

        Availability comes purely from whether the proxy link can be established
        (no GET — bringing the link up already proves the node is reachable, and
        a GET would cost a round trip for nothing). With a timed keep-alive this
        does NOT hold the connection: it connects, records availability, and
        disconnects immediately so a background recovery check never keeps the
        lamp's slot. With keep-alive 0 the link is kept: "always connected"
        starts at startup, not at the first click — otherwise that click paid
        the whole connect on top of a probe that had just succeeded. If a
        command connection is already held we are plainly available and do
        nothing.
        """
        if self._stopped:
            return
        async with self._lock:
            if self._controller is not None:
                return  # a held command connection already proves reachability
            if not self._may_attempt():
                # Checked again under the lock, not only at the gate: on
                # 2026-09-12 the probe tick passed the gate while the drop
                # watchdog's reconnect was still in flight, queued here, and
                # connected the second that reconnect failed -- one attempt
                # inside the wait the failure had just imposed.
                return
            controller = await self._ensure_connected(automatic=True)
            if controller is not None and self._idle_timeout > 0:
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
            data = await self._legacy_store.async_load()
            if data:
                await self._legacy_store.async_remove()
        if not data:
            return 0, self._network.iv_index
        return (
            int(data.get("seq", 0)) + SEQ_SAFETY_MARGIN,
            int(data.get("iv_index", self._network.iv_index)),
        )

    def _state_to_save(self) -> dict[str, int]:
        self._saved_seq = self._seq
        return {"seq": self._seq, "iv_index": self._iv_index}

    def _persist(self) -> None:
        """Queue a write of the SEQ cursor and IV Index.

        Debounced, unless the cursor has run half a margin ahead of the disk or
        has been restarted (a new IV Index): then it is written now, so that
        what a crash can lose never reaches :data:`SEQ_SAFETY_MARGIN`.
        """
        ahead = self._seq - self._saved_seq
        urgent = not 0 <= ahead < SEQ_SAFETY_MARGIN // 2
        self._store.async_delay_save(
            self._state_to_save, 0 if urgent else SEQ_SAVE_DELAY
        )

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
        if beacon.iv_index < self._iv_index:
            # An IV Index only ever grows. A node that was switched off at the
            # wall during an IV Update comes back announcing the old one, and
            # adopting it restarted the SEQ cursor at 0 under an index the mesh
            # had left (everything dropped), then again at 0 under the current
            # one on the next connection to a healthy node, this time reusing
            # numbers already spent under it. The spec's IV Update procedure has a
            # node ignore a beacon whose index is behind its own; so do we.
            if not self._iv_regress_warned:
                self._iv_regress_warned = True
                logger.warning(
                    "ignoring IV Index %#x announced through this proxy: it is "
                    "behind the %#x already in use, so that node has missed an "
                    "IV Update and will catch up on its own",
                    beacon.iv_index, self._iv_index,
                )
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
        misses = self._fail_count
        self._available = True
        self._fail_count = 0
        self._forget_refusals()
        self._backoff = 0.0
        self._next_attempt = 0.0
        self._clear_issue()
        # An outage now leaves a single warning behind, so without this line
        # nothing at default level would say it ended. Only an outage that cost
        # availability gets one: the transient misses below the threshold are
        # not logged on the way down either.
        if misses >= UNREACHABLE_THRESHOLD:
            logger.info("mesh proxy reachable again after %d misses", misses)
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

    # ----------------------------------------------------------- stuck proxy

    def _note_refusal(self, address: str, elapsed: float) -> None:
        """Record a failed connect, and tell a refusal from an ordinary miss.

        A wedged proxy refuses in about a second while it hears the node and
        has a slot free. A node that is merely busy, out of range or unplugged
        looks nothing like that: the attempt takes its time, or the node is not
        there to be heard at all.

        Only ever accuses a proxy when the node has exactly ONE connectable
        path. Home Assistant picks the path itself and never reports which one
        it used, so with several in range the failure cannot be pinned on any
        of them, and naming the wrong proxy would send someone to restart a
        healthy one.
        """
        paths = connect_paths(self.hass, address)
        path = paths[0] if len(paths) == 1 else None
        if (
            path is None
            or elapsed > STUCK_PROXY_FAST_FAILURE
            or not path.has_free_slot
        ):
            self._forget_refusals()
            return
        if (
            self._refused_path is not None
            and path.source != self._refused_path.source
        ):
            self._refusals = 0  # another proxy: this streak is not its doing
        self._refusals += 1
        self._refused_path = path
        self._refused_scanner = scanner_by_source(self.hass, path.source)

    def _forget_refusals(self) -> None:
        """Drop the streak: whatever it was tracking is over."""
        self._refusals = 0
        self._refused_path = None
        self._refused_scanner = None

    @property
    def _proxy_is_stuck(self) -> bool:
        return (
            self._refusals >= STUCK_PROXY_FAILURES and self._refused_path is not None
        )

    def _proxy_restarted(self) -> bool:
        """True once the proxy we were accusing has been restarted.

        A restarted proxy registers a NEW scanner object under the same source,
        which is the only signal Home Assistant gives that it happened. The
        wait those refusals imposed was imposed by a condition that no longer
        exists, so it goes with them: on 2026-09-20 the lamp was reachable two
        minutes before the coordinator, still counting down, tried again.
        """
        if self._refused_scanner is None or self._refused_path is None:
            return False
        current = scanner_by_source(self.hass, self._refused_path.source)
        if current is self._refused_scanner:
            return False
        logger.info(
            "mesh proxy %s came back; dropping the wait its refusals imposed",
            self._refused_path.name,
        )
        self._forget_refusals()
        self._backoff = 0.0
        self._next_attempt = 0.0
        return True

    # --------------------------------------------------------------- repairs

    def _raise_proxy_issue(self) -> None:
        """Raise whichever repair fits what is actually wrong.

        ``proxy_stuck`` once one proxy has refused instantly several times in a
        row while hearing the node with a slot free: it names that proxy and
        says to restart it, because nothing here can unwedge it and each of the
        three occurrences so far cost an hour of reading logs by hand.

        ``proxy_unreachable`` otherwise, with a diagnostic of every 0x1828
        advert Home Assistant sees, so "no proxy in range" (``none``) can be
        told apart from "wrong keys" (a foreign ``network_id=...``) from the UI.

        Idempotent per kind: an outage that turns out to be a wedged proxy
        replaces its repair instead of leaving the vaguer one on screen.
        """
        kind = "proxy_stuck" if self._proxy_is_stuck else "proxy_unreachable"
        if self._issue_kind == kind:
            return
        self._clear_issue()
        self._issue_kind = kind
        network = self._network.name or "mesh"
        if kind == "proxy_stuck":
            path = self._refused_path
            assert path is not None  # guaranteed by _proxy_is_stuck
            placeholders = {
                "network": network,
                "proxy": path.name,
                "proxy_address": path.source,
                "failures": str(self._refusals),
            }
        else:
            seen = discovered_proxies(self.hass)
            placeholders = {
                "network": network,
                "network_id": k3(self._network.net_key).hex(),
                "seen": (
                    ", ".join(f"{addr} ({desc})" for addr, desc in seen)
                    if seen
                    else "none"
                ),
            }
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{kind}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders=placeholders,
        )

    def _clear_issue(self) -> None:
        """Delete both repairs (idempotent, even across a restart)."""
        self._issue_kind = None
        for kind in ("proxy_unreachable", "proxy_stuck"):
            ir.async_delete_issue(self.hass, DOMAIN, f"{kind}_{self.entry.entry_id}")
