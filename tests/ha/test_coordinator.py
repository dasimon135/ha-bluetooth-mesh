"""Tests for the mesh runtime coordinator (Task B3).

The coordinator owns the proxy connection, the controller, and SEQ persistence.
Here the two BLE seams are mocked — ``find_proxy_address`` and
``async_connect_bearer`` from :mod:`.mesh_transport` — and a ``FakeController``
is injected in place of the real :class:`btmesh.controller.MeshController`, so
no radios are touched. Run in the daikin_madoka venv (HA + HHCC)::

    PYTHONPATH="tests/ha/_winshims;src" .../daikin_madoka/.venv/Scripts/python.exe \
        -m pytest tests/ha/test_coordinator.py -q
"""

from __future__ import annotations

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
    """Stand-in for MeshController: records lifecycle + exposes a moving seq."""

    def __init__(self, seq: int = 0x100) -> None:
        self.started = False
        self.stopped = False
        self.seq = seq
        self.calls: list[tuple] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def set_onoff(self, unicast: int, on: bool) -> bool:
        self.calls.append(("set_onoff", unicast, on))
        self.seq += 1
        return on

    async def get_onoff(self, unicast: int) -> bool:
        self.calls.append(("get_onoff", unicast))
        return True

    async def set_lightness(self, unicast: int, level_0_1: float) -> int:
        self.calls.append(("set_lightness", unicast, level_0_1))
        self.seq += 1
        return round(level_0_1 * 0xFFFF)

    async def set_ctl_temperature(self, unicast: int, kelvin: int) -> int:
        self.calls.append(("set_ctl_temperature", unicast, kelvin))
        self.seq += 1
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


def _patch_transport(controller, *, address=PROXY_ADDR, ctor_side_effect=None):
    """Patch the two BLE seams and MeshController for one connect attempt."""
    kwargs = (
        {"side_effect": ctor_side_effect}
        if ctor_side_effect is not None
        else {"return_value": controller}
    )
    return (
        patch.object(coordinator_mod, "find_proxy_address", return_value=address),
        patch.object(
            coordinator_mod,
            "async_connect_bearer",
            new=AsyncMock(return_value=(MagicMock(), MagicMock())),
        ),
        patch.object(coordinator_mod, "MeshController", **kwargs),
    )


async def test_start_connects_delegates_and_persists_seq(hass) -> None:
    """Found proxy → available, controller started, command delegated, seq saved."""
    entry = _make_entry(hass)
    fake = FakeController()
    find, connect, ctor = _patch_transport(fake)
    with find, connect, ctor:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()

        assert coord.available is True
        assert fake.started is True
        assert coord.network.nodes[0].unicast == UNICAST

        result = await coord.async_set_onoff(UNICAST, True)
        assert result is True
        assert fake.calls[-1] == ("set_onoff", UNICAST, True)

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
    with patch.object(coordinator_mod, "find_proxy_address", return_value=None):
        coord = MeshCoordinator(hass, entry)
        # First attempt (via start) then drive up to the failure threshold.
        await coord.async_start()
        for _ in range(UNREACHABLE_THRESHOLD - 1):
            await coord._async_connect()

        assert coord.available is False  # did not raise
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"proxy_unreachable_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.translation_key == "proxy_unreachable"
    await coord.async_stop()  # cancels the pending reconnect timer


async def test_stored_seq_is_seeded_into_controller(hass) -> None:
    """A persisted SEQ is seeded (plus the safety margin) when building the controller."""
    entry = _make_entry(hass)
    store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seq")
    await store.async_save({"seq": 0x5000})

    captured: dict[str, int] = {}

    def ctor(network, bearer, *, src_addr, seq):
        captured["seq"] = seq
        captured["src_addr"] = src_addr
        return FakeController()

    find, connect, ctor_patch = _patch_transport(None, ctor_side_effect=ctor)
    with find, connect, ctor_patch:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()

    assert captured["seq"] == 0x5000 + SEQ_SAFETY_MARGIN
    assert captured["src_addr"] == coordinator_mod.SRC_ADDR
    await coord.async_stop()


async def test_stop_stops_controller_and_cancels_reconnect(hass) -> None:
    """async_stop tears down the controller and cancels a pending reconnect."""
    entry = _make_entry(hass)
    fake = FakeController()
    find, connect, ctor = _patch_transport(fake)
    with find, connect, ctor:
        coord = MeshCoordinator(hass, entry)
        await coord.async_start()
        assert coord.available is True

        # Arm a reconnect so we can prove async_stop cancels it.
        coord._schedule_reconnect()
        assert coord._reconnect_unsub is not None

        await coord.async_stop()

    assert fake.stopped is True
    assert coord.available is False
    assert coord._reconnect_unsub is None
