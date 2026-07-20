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

import json
from pathlib import Path
import contextlib
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
from custom_components.bluetooth_mesh.const import CONF_CONNECT_JSON, DOMAIN
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


async def test_command_connects_delegates_persists_seq_and_disconnects(hass) -> None:
    """A command connects on-demand, delegates, saves seq, then disconnects."""
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

        # The slot was freed: the controller was stopped and the client
        # disconnected after the command.
        assert fake.stopped is True
        assert client.disconnect.await_count >= 1

        # SEQ mirrored to the Store after the command.
        stored = await coord._store.async_load()
        assert stored == {"seq": fake.seq}

        # No repair issue while healthy.
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"proxy_unreachable_{entry.entry_id}"
            )
            is None
        )
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

    def ctor(network, bearer, *, src_addr, seq, tid):
        ctor_seqs.append(seq)
        assert src_addr == coordinator_mod.SRC_ADDR
        return FakeController(seq=seq, tid=tid)

    with _patch_transport(None, ctor_side_effect=ctor):
        coord = MeshCoordinator(hass, entry)
        # async_start awaits one probe (a pure connect — sends no command, so it
        # does not advance seq), so the first controller is built at the seeded
        # cursor and the seed is NOT consumed by the probe.
        await coord.async_start()

        await coord.async_set_onoff(UNICAST, True)  # first real command (+1)
        await coord.async_set_onoff(UNICAST, True)  # second real command

    seeded = 0x5000 + SEQ_SAFETY_MARGIN
    # Three controllers were built: the startup probe, then the two commands.
    # The probe sends no command so it does not advance seq, so the first
    # *command* still sees the seeded cursor; the second sees it advanced by
    # exactly 1 (margin applied once, never re-added per command).
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
