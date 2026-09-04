"""Tests for the mesh runtime coordinator (Task B3, on-demand model).

The coordinator now connects to the proxy *per command*, runs, and disconnects
again — freeing the lamp's single proxy slot after every command. Here the two
BLE seams are mocked — ``find_proxy_address`` and ``async_connect_bearer`` from
:mod:`.mesh_transport` — and a ``FakeController`` is injected in place of the
real :class:`btmesh.controller.MeshController`, so no radios are touched. Run in
the daikin_madoka venv (HA + HHCC)::

    PYTHONPATH="tests/ha/_winshims;src" .../daikin_madoka/.venv/Scripts/python.exe \
        -m pytest tests/ha/test_coordinator.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The library venv (uv run pytest) also collects tests/ha but has no HA; this
# skips there and runs for real in the daikin_madoka venv.
pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bluetooth_mesh import coordinator as coordinator_mod
from custom_components.bluetooth_mesh.const import (
    CONF_CONNECT_JSON,
    CONF_KEEPALIVE,
    CONF_SRC_ADDR,
    DOMAIN,
)
from custom_components.bluetooth_mesh.coordinator import (
    SEQ_SAFETY_MARGIN,
    STORAGE_VERSION,
    UNREACHABLE_THRESHOLD,
    MeshCoordinator,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.connect.json"
PROXY_ADDR = "AA:BB:CC:DD:EE:FF"
# A second proxy for the tests that assert the held address can move.
OTHER_PROXY_ADDR = "C3:EB:49:65:67:55"
UNICAST = 0x000C


class FakeController:
    """Stand-in for MeshController: records lifecycle + moving seq/tid cursors.

    The coordinator now builds ``MeshController(..., seq=self._seq,
    tid=self._tid)`` and reads back both ``controller.seq`` and
    ``controller.tid`` after each command, so this fake must expose a ``tid``
    attribute and advance it (like ``seq``) whenever it emits a Set message.
    """

    def __init__(self, seq: int = 0x100, tid: int = 0) -> None:
        self.started = False
        self.stopped = False
        self.seq = seq
        self.tid = tid
        self.calls: list[tuple] = []
        # Mirrors MeshController.failed: True once the TX pump died, i.e. the
        # controller can no longer transmit anything (commands still return
        # None rather than raising, so this flag is the only signal).
        self.failed = False
        # Mirrors MeshController: the last authenticated Secure Network Beacon
        # and the IV Index this controller encrypts with.
        self.beacon = None
        self.iv_index = 0
        # What the node answers to a Composition Data Get (None = silence).
        self.composition = "composition-sentinel"

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def set_onoff(
        self, unicast: int, on: bool, *, timeout: float = 5.0, retries: int = 1
    ) -> bool:
        self.calls.append(("set_onoff", unicast, on))
        self.seq += 1
        self.tid = (self.tid + 1) & 0xFF
        return on

    async def get_onoff(self, unicast: int, *, timeout: float = 5.0) -> bool:
        self.calls.append(("get_onoff", unicast))
        return True

    async def set_lightness(
        self, unicast: int, level_0_1: float, *, timeout: float = 5.0
    ) -> int:
        self.calls.append(("set_lightness", unicast, level_0_1))
        self.seq += 1
        self.tid = (self.tid + 1) & 0xFF
        return round(level_0_1 * 0xFFFF)

    async def set_ctl(
        self, unicast: int, level_0_1: float, kelvin: int, *, timeout: float = 5.0
    ) -> int:
        self.calls.append(("set_ctl", unicast, level_0_1, kelvin))
        self.seq += 1
        self.tid = (self.tid + 1) & 0xFF
        return kelvin

    async def set_ctl_temperature(
        self, unicast: int, kelvin: int, *, timeout: float = 5.0
    ) -> int:
        self.calls.append(("set_ctl_temperature", unicast, kelvin))
        self.seq += 1
        self.tid = (self.tid + 1) & 0xFF
        return kelvin

    async def get_composition(
        self, unicast: int, *, page: int = 0, timeout: float = 5.0
    ):
        self.calls.append(("get_composition", unicast))
        self.seq += 1
        return self.composition


def _make_entry(hass) -> MockConfigEntry:
    """A config entry carrying the sanitized sample .connect JSON."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: FIXTURE.read_text(encoding="utf-8")},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)
    return entry


@contextlib.contextmanager
def _patch_transport(controller, *, address=PROXY_ADDR, ctor_side_effect=None):
    """Patch the BLE seams (transport + discovery) and MeshController.

    Yields the mocked BLE *client* so tests can assert it was disconnected after
    every command (the core requirement of the on-demand model). Its
    ``disconnect`` is an ``AsyncMock`` so ``await client.disconnect()`` works.
    """
    kwargs = (
        {"side_effect": ctor_side_effect}
        if ctor_side_effect is not None
        else {"return_value": controller}
    )
    client = MagicMock()
    client.disconnect = AsyncMock()
    with (
        patch.object(coordinator_mod, "find_proxy_address", return_value=address),
        patch.object(
            coordinator_mod,
            "async_connect_bearer",
            new=AsyncMock(return_value=(client, MagicMock())),
        ),
        patch.object(coordinator_mod, "MeshController", **kwargs),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
        # Push discovery goes through HA's bluetooth manager, which no test
        # here sets up; the dedicated test re-patches this with its own stub.
        patch.object(
            coordinator_mod,
            "async_register_proxy_callback",
            return_value=lambda: None,
        ),
    ):
        yield client


async def test_command_reuses_held_connection_persists_seq_frees_on_stop(hass) -> None:
    """A command runs on the kept-alive connection, saves seq, holds the link,
    and only frees the lamp's slot on stop."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()

        # The initial probe made us available.
        assert coord.available is True
        assert coord.network.nodes[0].unicast == UNICAST

        result = await coord.async_set_onoff(UNICAST, True)
        assert result is True
        assert fake.calls[-1] == ("set_onoff", UNICAST, True)

        # Keep-alive: the connection is HELD after the command, not dropped, so
        # the next command skips the multi-second connect.
        assert coord._controller is fake

        # SEQ (and the IV Index it is only unique within) mirrored to the Store.
        stored = await coord._store.async_load()
        assert stored == {"seq": fake.seq, "iv_index": 0}

        # No repair issue while healthy.
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"proxy_unreachable_{entry.entry_id}"
            )
            is None
        )

    await coord.async_stop()
    # Stop frees the slot: controller stopped, client disconnected, ref cleared.
    assert coord._controller is None
    assert fake.stopped is True
    assert client.disconnect.await_count >= 1


async def test_keepalive_permanent_by_default_never_arms_idle(hass) -> None:
    """Default keep-alive (0) holds the connection with NO idle-drop timer."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        assert coord._idle_timeout == 0  # shipped default: always connected
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        assert coord._controller is fake  # held
        assert coord._idle_unsub is None  # but never dropped for inactivity
        await coord.async_stop()


async def test_keepalive_timeout_arms_and_stop_cancels_idle_drop(hass) -> None:
    """A positive keep-alive option arms the idle-drop timer; stop cancels it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: FIXTURE.read_text(encoding="utf-8")},
        options={CONF_KEEPALIVE: 30},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)
    fake = FakeController()
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        assert coord._idle_timeout == 30
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        assert coord._controller is fake
        assert coord._idle_unsub is not None  # idle drop scheduled
    await coord.async_stop()
    assert coord._idle_unsub is None  # cancelled on stop


async def test_failed_controller_start_disconnects_the_client(hass) -> None:
    """A connect that dies after the BLE link is up must still free the slot.

    ``async_connect_bearer`` returns a CONNECTED client; if bringing the mesh
    controller up on top of it then fails (a GATT subscribe error, or the
    connect timeout firing during ``start()``), that client is the only thing
    holding the lamp's single proxy slot. Leaking it locks out Home Assistant
    AND the vendor app — and the coordinator then blames an unreachable proxy
    while itself holding the slot.
    """
    entry = _make_entry(hass)

    class FailingStartController(FakeController):
        async def start(self) -> None:
            raise RuntimeError("could not subscribe to the proxy Data Out")

    with _patch_transport(FailingStartController()) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()  # probes once, and the connect fails

        assert coord._controller is None
        assert coord._client is None
        assert client.disconnect.await_count == 1  # the slot was handed back
    await coord.async_stop()


async def test_failed_controller_construction_disconnects_the_client(hass) -> None:
    """Same guarantee when the controller cannot even be built."""
    entry = _make_entry(hass)

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index, app_key):
        raise ValueError("bad network model")

    with _patch_transport(None, ctor_side_effect=ctor) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()

        assert coord._controller is None
        assert client.disconnect.await_count == 1
    await coord.async_stop()


async def test_dead_transport_drops_the_held_connection(hass) -> None:
    """A controller whose TX pump died is torn down, not held for reuse.

    Commands are best-effort: a dead transport makes them return None, exactly
    like an unconfirmed Status, so nothing raises and the error path never runs.
    Without an explicit check the coordinator would keep the wedged controller
    forever — the entity stays available while every command silently does
    nothing until the entry is reloaded.
    """
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        assert coord._controller is fake  # healthy: held for the next command

        fake.failed = True  # the GATT write failed; the pump is dead
        await coord.async_set_onoff(UNICAST, False)

        assert coord._controller is None  # dropped, so the next call reconnects
        assert fake.stopped is True
        assert client.disconnect.await_count >= 1
    await coord.async_stop()


async def test_dead_transport_is_never_reused_by_the_next_command(hass) -> None:
    """A held controller that died between commands is replaced, not reused.

    The pump dies asynchronously, so it can fail just after a command returned
    and the post-command check saw a healthy controller. The next command must
    still notice before reusing the link.
    """
    entry = _make_entry(hass)
    built: list[FakeController] = []

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index, app_key):
        built.append(FakeController(seq=seq, tid=tid))
        return built[-1]

    with _patch_transport(None, ctor_side_effect=ctor):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        held = coord._controller

        # It died after the command returned, so the coordinator still holds it.
        held.failed = True
        await coord.async_set_onoff(UNICAST, False)

        assert coord._controller is not held  # reconnected instead of reusing
        assert held.stopped is True
        assert coord._controller.calls[-1] == ("set_onoff", UNICAST, False)
    await coord.async_stop()


async def test_the_proxy_address_is_published_once_connected(hass) -> None:
    """The address is what makes the held slot resolvable outside this
    integration: the sensor platform writes it onto the device as a
    `connections` entry. Before the first connect there is none to publish, and
    inventing one would claim a slot nobody holds."""
    entry = _make_entry(hass)

    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)
        assert coord.proxy_address is None

        await coord.async_start()
        assert coord.proxy_address == PROXY_ADDR
    await coord.async_stop()


async def test_the_proxy_address_survives_a_teardown(hass) -> None:
    """Dropping it whenever the link goes idle would make the device
    unresolvable at exactly the moment somebody asks who holds the slot -- and
    the link being idle is the normal state of an on-demand connection."""
    entry = _make_entry(hass)

    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord._teardown()

        assert coord.proxy_address == PROXY_ADDR
    await coord.async_stop()


async def test_a_reconnect_to_another_proxy_republishes_the_address(hass) -> None:
    """A mesh proxy address is *random static* and the mesh may be reached
    through a different node entirely, so the published one can go stale while
    we still believe we are available.

    `_set_available` stays silent without a transition, so this change has to
    announce itself -- otherwise the device would keep claiming a BLE
    connection it no longer has, and a slot reader resolving that address would
    name this device for somebody else's slot.
    """
    entry = _make_entry(hass)
    events: list[str | None] = []

    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        coord.async_add_listener(lambda: events.append(coord.proxy_address))

        # Still available, so only the address change can speak here.
        # Deliberately not PROXY_ADDR: the whole assertion is that a DIFFERENT
        # address speaks for itself.
        with patch.object(
            coordinator_mod, "find_proxy_address", return_value=OTHER_PROXY_ADDR
        ):
            await coord._teardown()
            await coord.async_set_onoff(UNICAST, True)

        assert coord.proxy_address == OTHER_PROXY_ADDR
        assert events == [OTHER_PROXY_ADDR]
    await coord.async_stop()


async def test_listeners_fire_only_on_an_availability_transition(hass) -> None:
    """Entities are told when the mesh comes back, not on every connect.

    They cannot poll for it — the light platform reads availability straight
    off the coordinator — and re-reading the lamp on every successful command
    would churn its single proxy slot for nothing.
    """
    entry = _make_entry(hass)
    fake = FakeController()
    events: list[bool] = []

    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        coord.async_add_listener(lambda: events.append(coord.available))

        await coord.async_start()  # first successful connect: unavailable -> available
        assert events == [True]

        await coord.async_set_onoff(UNICAST, True)  # still available, no event
        assert events == [True]

        # Now make every connect fail until the threshold flips us to stale.
        client.is_connected = False
        with patch.object(coordinator_mod, "find_proxy_address", return_value=None):
            for _ in range(UNREACHABLE_THRESHOLD):
                await coord.async_set_onoff(UNICAST, True)

        assert events == [True, False]
    await coord.async_stop()


async def test_removing_a_listener_stops_the_notifications(hass) -> None:
    entry = _make_entry(hass)
    events: list[bool] = []
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)
        remove = coord.async_add_listener(lambda: events.append(coord.available))
        remove()
        await coord.async_start()
        assert events == []
    await coord.async_stop()


class _Beacon:
    """Stand-in for btmesh.beacon.SecureNetworkBeacon."""

    def __init__(self, iv_index: int, iv_update: bool = False) -> None:
        self.iv_index = iv_index
        self.iv_update = iv_update


async def test_iv_index_is_seeded_from_the_store(hass) -> None:
    """The persisted IV Index wins over the one frozen in the .connect export."""
    entry = _make_entry(hass)
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seq")
    await store.async_save({"seq": 0x100, "iv_index": 7})

    seen: list[int] = []

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index, app_key):
        seen.append(iv_index)
        return FakeController(seq=seq, tid=tid)

    with _patch_transport(None, ctor_side_effect=ctor):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        assert seen[0] == 7
    await coord.async_stop()


async def test_a_new_iv_index_is_adopted_persisted_and_restarts_the_seq(hass) -> None:
    """An IV Update the export knows nothing about must not silence us.

    Our IV Index going stale is fatal in silence: every PDU we send is dropped
    by the mesh and every PDU we receive fails the IVI check. The subnet
    announces the truth in its Secure Network Beacon, so adopt it — and restart
    the SEQ cursor, which is only unique per IV Index.
    """
    entry = _make_entry(hass)
    fake = FakeController(seq=0x500)
    fake.beacon = _Beacon(iv_index=9)
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)

        stored = await coord._store.async_load()
        assert stored["iv_index"] == 9
        assert stored["seq"] == 0  # a fresh IV Index restarts the SEQ space
        # The live link still encrypts with the old index, so it is dropped and
        # the next command reconnects on the new one.
        assert coord._controller is None
        assert client.disconnect.await_count >= 1
    await coord.async_stop()


async def test_a_matching_beacon_changes_nothing(hass) -> None:
    """The normal case: the beacon confirms what we already use."""
    entry = _make_entry(hass)
    fake = FakeController(seq=0x500)
    fake.beacon = _Beacon(iv_index=0)  # the fixture network's IV Index
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)

        stored = await coord._store.async_load()
        assert stored["seq"] == fake.seq  # untouched
        assert coord._controller is fake  # link kept
    await coord.async_stop()


async def test_the_authenticated_beacon_is_kept_for_diagnostics(hass) -> None:
    """Whether the subnet beacons, and whether it authenticates, is the fastest
    way to tell a key mismatch from a silent node — so surface it."""
    entry = _make_entry(hass)
    fake = FakeController()
    fake.beacon = _Beacon(iv_index=0)
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        assert coord.beacon is None  # nothing seen yet
        await coord.async_set_onoff(UNICAST, True)
        assert coord.beacon is not None
        assert coord.beacon.iv_index == 0
    await coord.async_stop()


async def test_seq_is_written_with_a_delay_not_on_every_command(hass) -> None:
    """One flash write per button press is not acceptable on an SD card.

    Home Assistant debounces Store writes for exactly this, and flushes them on
    shutdown; the SEQ safety margin applied at startup already covers whatever
    a crash leaves unflushed.
    """
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        with patch.object(
            coord._store, "async_delay_save"
        ) as delayed, patch.object(coord._store, "async_save") as immediate:
            await coord.async_start()
            await coord.async_set_onoff(UNICAST, True)
            assert delayed.called
            assert not immediate.called
    await coord.async_stop()


async def test_stop_flushes_the_cursor_immediately(hass) -> None:
    """Debouncing must not lose the cursor when the entry is unloaded."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        await coord.async_stop()

    stored = await coord._store.async_load()
    assert stored["seq"] == fake.seq


async def test_setup_does_not_block_on_the_first_connect(hass) -> None:
    """A cold proxy must not hold up Home Assistant's startup.

    async_start awaited a full connect — up to CONNECT_TIMEOUT plus bleak's
    retries — inside async_setup_entry, well past the 10s mark where Home
    Assistant starts warning that an integration is slow to set up, delaying
    everything queued behind it.
    """
    entry = _make_entry(hass)
    fake = FakeController()

    slow_connect = AsyncMock()

    async def _slow(*args, **kwargs):
        await asyncio.sleep(0.4)
        return MagicMock(), MagicMock()

    slow_connect.side_effect = _slow

    with (
        _patch_transport(fake),
        patch.object(coordinator_mod, "async_connect_bearer", new=slow_connect),
    ):
        coord = MeshCoordinator(hass, entry)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await coord.async_start()
        elapsed = loop.time() - started

        assert elapsed < 0.2, f"async_start blocked for {elapsed:.2f}s"

        # Background tasks are deliberately NOT awaited by async_block_till_done
        # — that is exactly what makes them background — so wait on the outcome.
        for _ in range(200):
            if coord.available:
                break
            await asyncio.sleep(0.01)
        assert coord.available is True  # the probe landed on its own
    await coord.async_stop()


async def test_a_matching_proxy_advert_triggers_a_reconnect(hass) -> None:
    """Recovery should not wait out the retry tick when the proxy reappears.

    mesh_transport.async_register_proxy_callback has existed and been tested
    since the first release without ever being called.
    """
    entry = _make_entry(hass)
    fake = FakeController()
    callbacks: list = []

    def register(hass_, net_key, on_found):
        callbacks.append(on_found)
        return lambda: None

    with (
        _patch_transport(fake),
        patch.object(
            coordinator_mod, "async_register_proxy_callback", side_effect=register
        ),
    ):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await hass.async_block_till_done()
        assert callbacks, "no push-discovery callback registered"

        # Lose the link and know it, then have the proxy re-appear. Both halves
        # matter: with keep-alive 0 the startup probe now HOLDS the link, and a
        # held link is itself proof of reachability the probe will not repeat.
        async with coord._lock:
            await coord._teardown()
        coord._available = False
        callbacks[0](PROXY_ADDR)
        await hass.async_block_till_done()
        assert coord.available is True
    await coord.async_stop()


async def test_no_proxy_stays_unavailable_and_raises_issue(hass) -> None:
    """No proxy → unavailable (no raise); a repair appears past the threshold."""
    entry = _make_entry(hass)
    with (
        patch.object(coordinator_mod, "find_proxy_address", return_value=None),
        patch.object(
            coordinator_mod,
            "async_connect_bearer",
            new=AsyncMock(return_value=(MagicMock(), MagicMock())),
        ),
        patch.object(coordinator_mod, "MeshController", return_value=FakeController()),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
    ):
        coord = MeshCoordinator(hass, entry)
        # async_start fires one probe (miss #1); drive commands up to threshold.
        await coord.async_start()
        for _ in range(UNREACHABLE_THRESHOLD - 1):
            assert await coord.async_set_onoff(UNICAST, True) is None

        assert coord.available is False  # did not raise to the caller
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"proxy_unreachable_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.translation_key == "proxy_unreachable"
    await coord.async_stop()


async def test_seq_margin_applied_once_not_per_command(hass) -> None:
    """Seed = stored + margin on the first command; the next reuses the advanced
    seq WITHOUT re-adding the margin (the historical per-command inflation bug)."""
    entry = _make_entry(hass)
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seq")
    await store.async_save({"seq": 0x5000})

    ctor_seqs: list[int] = []

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index, app_key):
        ctor_seqs.append(seq)
        assert src_addr == coordinator_mod.SRC_ADDR
        return FakeController(seq=seq, tid=tid)

    with _patch_transport(None, ctor_side_effect=ctor) as client:
        coord = MeshCoordinator(hass, entry)
        # async_start awaits one probe (a pure connect — sends no command, so it
        # does not advance seq), so the first controller is built at the seeded
        # cursor and the seed is NOT consumed by the probe.
        await coord.async_start()

        await coord.async_set_onoff(UNICAST, True)  # first real command (+1)
        # Force the second command to RECONNECT rather than reuse the held link,
        # so we can assert a fresh controller seeds from the ADVANCED cursor (the
        # margin is never re-added on reconnect).
        client.is_connected = False
        await coord.async_set_onoff(UNICAST, True)  # second real command

    seeded = 0x5000 + SEQ_SAFETY_MARGIN
    # Three controllers were built: the startup probe, the first command, and —
    # after the forced reconnect — the second. The probe sends no command so it
    # does not advance seq, so the first *command* still sees the seeded cursor;
    # the reconnecting second sees it advanced by exactly 1 (margin applied once,
    # never re-added on reconnect).
    assert ctor_seqs[-2] == seeded  # first command: stored + margin (once)
    assert ctor_seqs[-1] == seeded + 1  # second: advanced by 1, margin NOT re-added
    await coord.async_stop()


async def test_stop_cancels_periodic_probe(hass) -> None:
    """async_stop cancels the periodic availability probe and goes unavailable."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        assert coord.available is True
        assert coord._probe_unsub is not None

        await coord.async_stop()

    assert coord.available is False
    assert coord._probe_unsub is None


# ------------------------------- source address / application key resolution

_APP_KEY_0 = "63964771734fbd76e3b40519d1d94a48"
_APP_KEY_1 = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _doc(nodes, app_keys=None):
    """A minimal connect document (the fixture, reshaped for one assertion)."""
    return {
        "meshName": "T",
        "meshUUID": "0F0E0D0C-0B0A-0908-0706-050403020100",
        "netKeys": [{"key": "7dd7364cd842ad18c17c2b820c84c3d6", "index": 0}],
        "appKeys": app_keys or [{"key": _APP_KEY_0, "index": 0}],
        "nodes": nodes,
    }


def _lamp(unicast, *, bind=(0,), elements=1):
    return {
        "UUID": f"n{unicast}",
        "unicastAddress": f"{unicast:04X}",
        "deviceKey": "9d6dd0e96eb25dc19a40ed9914f8f03f",
        "cid": "07E9",
        "elements": [
            {
                "index": index,
                "models": [{"modelId": "1300", "bind": list(bind)}],
            }
            for index in range(elements)
        ],
    }


def _entry_for(hass, doc) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: json.dumps(doc)},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)
    return entry


async def test_source_address_defaults_to_7fff_when_nothing_owns_it(hass) -> None:
    entry = _entry_for(hass, _doc([_lamp(0x000C)]))
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.src_addr == 0x7FFF


async def test_source_address_steps_off_an_address_the_export_owns(hass) -> None:
    """Sharing a unicast with a real node mutes us for good.

    That node's peers hold a replay-protection entry for the address; our
    sequence cursor starts far below whatever it reached, so every message we
    send is discarded as a replay — no error, no Status, no physical effect,
    and re-importing the export changes nothing.
    """
    # Two elements at 0x7FFE → the node owns 0x7FFE and 0x7FFF.
    entry = _entry_for(hass, _doc([_lamp(0x7FFE, elements=2)]))
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.src_addr == 0x7FFD


async def test_app_key_follows_what_the_driven_models_bind(hass) -> None:
    """Encrypting under a key the models never bound is silently fatal."""
    entry = _entry_for(
        hass,
        _doc(
            [_lamp(0x000C, bind=(4,))],
            app_keys=[
                {"key": _APP_KEY_0, "index": 0},
                {"key": _APP_KEY_1, "index": 4},
            ],
        ),
    )
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.app_key_index == 4


async def test_the_resolved_key_and_address_reach_the_controller(hass) -> None:
    """Resolving them is worthless if the controller is built with the others."""
    entry = _entry_for(
        hass,
        _doc(
            [_lamp(0x7FFF, bind=(4,))],
            app_keys=[
                {"key": _APP_KEY_0, "index": 0},
                {"key": _APP_KEY_1, "index": 4},
            ],
        ),
    )
    fake = FakeController()
    with _patch_transport(fake):
        with patch.object(
            coordinator_mod, "MeshController", return_value=fake
        ) as ctor:
            coord = MeshCoordinator(hass, entry)
            await coord.async_start()
            kwargs = ctor.call_args.kwargs
            await coord.async_stop()

    assert kwargs["src_addr"] == 0x7FFE
    assert kwargs["app_key"] == bytes.fromhex(_APP_KEY_1)


async def test_a_silent_subnet_is_reported_once(hass, caplog) -> None:
    """No beacon means the IV Index is a guess, and a wrong guess is invisible.

    The export carries no IV Index at all, so 0 is an assumption; the subnet's
    Secure Network Beacon is the only thing that can confirm or correct it. A
    node that never beacons leaves us encrypting with an index the mesh may
    have moved past, discarding everything we send — worth saying once.
    """
    entry = _make_entry(hass)
    fake = FakeController()  # .beacon stays None
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        with caplog.at_level("WARNING"):
            await coord.async_set_onoff(UNICAST, True)
            await coord.async_set_onoff(UNICAST, False)
        await coord.async_stop()

    assert coord.beacon is None
    assert caplog.text.count("no Secure Network Beacon") == 1


async def test_composition_probe_reaches_the_controller(hass) -> None:
    """The device-keyed probe is what tells "never arrived" from "ignored"."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake):
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()

        result = await coord.async_get_composition(UNICAST)

        assert result == "composition-sentinel"
        assert ("get_composition", UNICAST) in fake.calls
        await coord.async_stop()


async def test_a_configured_source_address_overrides_the_derived_one(hass) -> None:
    """The last hypothesis an export cannot rule out needs a way to be tested.

    An export lists no provisioner node, so the address the vendor app gave
    itself is invisible. If that address is ours, every message we send is
    dropped as a replay before any model sees it, and nothing anywhere says so.
    Moving off it is the only way to find out.
    """
    entry = _entry_for(hass, _doc([_lamp(0x000C)]))
    hass.config_entries.async_update_entry(entry, options={CONF_SRC_ADDR: 0x0030})
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.src_addr == 0x0030


async def test_a_configured_address_a_node_already_owns_is_refused(hass) -> None:
    """Deliberate or not, that address is unusable — fall back rather than mute."""
    entry = _entry_for(hass, _doc([_lamp(0x000C, elements=2)]))
    hass.config_entries.async_update_entry(entry, options={CONF_SRC_ADDR: 0x000D})
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.src_addr == 0x7FFF


async def test_a_configured_address_outside_the_unicast_range_is_refused(hass) -> None:
    entry = _entry_for(hass, _doc([_lamp(0x000C)]))
    hass.config_entries.async_update_entry(entry, options={CONF_SRC_ADDR: 0xC000})
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.src_addr == 0x7FFF


async def test_no_configured_address_keeps_the_derived_default(hass) -> None:
    entry = _entry_for(hass, _doc([_lamp(0x000C)]))
    with _patch_transport(FakeController()):
        coord = MeshCoordinator(hass, entry)

    assert coord.src_addr == 0x7FFF


# ---------------------------------------------------------------------------
# keep-alive 0 means "always connected", not "held until it happens to drop"
#
# 2026-09-04, David's network: with the link released, the morning's first
# command paid an 11 s connect through the proxy habluetooth preferred that
# minute (atomesalon at -94 dBm). Holding the link only helps if something
# re-establishes it when it drops, and if the startup probe keeps it.


async def _wait_for(predicate, *, tries: int = 200) -> None:
    """Background tasks are NOT awaited by async_block_till_done; poll instead."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)


async def test_an_unexpected_drop_reconnects_when_keepalive_is_permanent(hass) -> None:
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        connects_before = coordinator_mod.async_connect_bearer.await_count
        assert coord._controller is fake

        # The proxy drops the link under us: bleak fires the callback we gave it.
        client.set_disconnected_callback.assert_called()
        on_drop = client.set_disconnected_callback.call_args.args[0]
        client.is_connected = False
        on_drop(client)

        await _wait_for(
            lambda: coordinator_mod.async_connect_bearer.await_count > connects_before
        )
        await _wait_for(lambda: coord._controller is not None)
        assert coord._controller is not None  # re-established and HELD
    await coord.async_stop()


async def test_an_unexpected_drop_is_left_alone_when_keepalive_is_timed(hass) -> None:
    """A timed keep-alive exists to hand the slot back: never reconnect unasked."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: FIXTURE.read_text(encoding="utf-8")},
        options={CONF_KEEPALIVE: 30},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        connects_before = coordinator_mod.async_connect_bearer.await_count

        on_drop = client.set_disconnected_callback.call_args.args[0]
        client.is_connected = False
        on_drop(client)
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()

        assert coordinator_mod.async_connect_bearer.await_count == connects_before
    await coord.async_stop()


async def test_our_own_teardown_never_triggers_a_reconnect(hass) -> None:
    """bleak fires the callback on OUR disconnect too; that is not a drop."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        on_drop = client.set_disconnected_callback.call_args.args[0]
        await coord.async_stop()
        connects_before = coordinator_mod.async_connect_bearer.await_count

        client.is_connected = False
        on_drop(client)
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()

        assert coordinator_mod.async_connect_bearer.await_count == connects_before
        assert coord._controller is None


async def test_the_startup_probe_holds_the_link_when_keepalive_is_permanent(hass) -> None:
    """Always-connected starts at startup, not at the first click."""
    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await _wait_for(lambda: coord.available)
        assert coord._controller is fake
        client.disconnect.assert_not_awaited()
        await coord.async_stop()


async def test_the_startup_probe_releases_the_link_when_keepalive_is_timed(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: FIXTURE.read_text(encoding="utf-8")},
        options={CONF_KEEPALIVE: 30},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await _wait_for(lambda: coord.available)
        await _wait_for(lambda: client.disconnect.await_count >= 1)
        assert coord._controller is None
        await coord.async_stop()


async def test_a_drop_during_shutdown_is_not_reconnected(hass) -> None:
    """Home Assistant stops Bluetooth before unloading this entry.

    Seen live on 2026-09-04 07:53: the link dropped as the proxies went away,
    the watchdog fired before async_stop had set _stopped, and four connect
    attempts failed with "Bluetooth is already shutdown". Harmless, but it is
    work and log noise on every shutdown for a link nobody wants back.
    """
    from homeassistant.core import CoreState

    entry = _make_entry(hass)
    fake = FakeController()
    with _patch_transport(fake) as client:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        await coord.async_set_onoff(UNICAST, True)
        on_drop = client.set_disconnected_callback.call_args.args[0]
        connects_before = coordinator_mod.async_connect_bearer.await_count

        hass.set_state(CoreState.stopping)
        try:
            client.is_connected = False
            on_drop(client)
            await asyncio.sleep(0.05)
            await hass.async_block_till_done()
        finally:
            hass.set_state(CoreState.running)

        assert coordinator_mod.async_connect_bearer.await_count == connects_before
        await coord.async_stop()
