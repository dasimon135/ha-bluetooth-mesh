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

import contextlib
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

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index):
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

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index):
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

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index):
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

    def ctor(network, bearer, *, src_addr, seq, tid, iv_index):
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
