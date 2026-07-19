"""Tests for the mesh light platform (Task B4).

A fake coordinator stands in for the real one: it carries a
:class:`btmesh.network_model.Network` parsed from the fabricated sample
``.connect`` fixture and records every ``async_set_*`` call the entity makes, so
no BLE or controller is touched. Run in the daikin_madoka venv (HA + HHCC)::

    PYTHONPATH="tests/ha/_winshims;src" .../daikin_madoka/.venv/Scripts/python.exe \
        -m pytest tests/ha/test_light.py -q
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

# The library venv (uv run pytest) also collects tests/ha but has no HA; this
# skips there and runs for real in the daikin_madoka venv.
pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.light import ColorMode

from custom_components.bluetooth_mesh.btmesh.network_model import Element, Model, Network, Node
from custom_components.bluetooth_mesh.light import MeshLight, async_setup_entry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.connect.json"
UNICAST = 0x000C
MESH_UUID = "0F0E0D0C-0B0A-0908-0706-050403020100"


class FakeCoordinator:
    """Minimal coordinator: a real Network + recorded async_set_* calls."""

    def __init__(self, network: Network, *, available: bool = True) -> None:
        self.network = network
        self.available = available
        self.calls: list[tuple] = []

    async def async_set_onoff(self, unicast: int, on: bool) -> bool:
        self.calls.append(("set_onoff", unicast, on))
        return on

    async def async_set_lightness(self, unicast: int, level_0_1: float) -> int:
        self.calls.append(("set_lightness", unicast, level_0_1))
        return round(level_0_1 * 0xFFFF)

    async def async_set_ctl_temperature(self, unicast: int, kelvin: int) -> int:
        self.calls.append(("set_ctl_temperature", unicast, kelvin))
        return kelvin


def _fixture_network() -> Network:
    """The fabricated sample network (node 0x000C has 0x1000+0x1300+0x1303)."""
    return Network.from_connect_file(str(FIXTURE))


def _onoff_only_network() -> Network:
    """A one-node network whose element 0 hosts ONLY Generic OnOff (0x1000)."""
    element0 = Element(
        index=0,
        unicast=0x0020,
        models=(Model(model_id=0x1000, bound_appkey_indexes=(0,)),),
    )
    node = Node(
        uuid="11112222-3333-4444-5555-666677778888",
        unicast=0x0020,
        device_key=b"\x00" * 16,
        cid=0x07E9,
        name="OnOff Only",
        elements=(element0,),
    )
    base = _fixture_network()
    return replace(base, nodes=(node,))


def _light(network: Network | None = None, unicast: int = UNICAST) -> tuple[
    MeshLight, FakeCoordinator
]:
    """Build a MeshLight for the node at ``unicast`` and its fake coordinator."""
    net = network or _fixture_network()
    coordinator = FakeCoordinator(net)
    node = next(n for n in net.nodes if n.unicast == unicast)
    light = MeshLight(coordinator, node)
    # Entities under test are not added to hass, so bypass the HA state-machine
    # write (we assert on the optimistic cache directly).
    light.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    return light, coordinator


async def test_setup_entry_creates_entity_with_ctl(hass) -> None:
    """The CTL-capable node yields one COLOR_TEMP light with the right unique_id."""
    coordinator = FakeCoordinator(_fixture_network())
    entry = type("Entry", (), {"runtime_data": coordinator})()

    added: list = []
    await async_setup_entry(hass, entry, lambda ents: added.extend(ents))

    assert len(added) == 1
    light = added[0]
    assert light.unique_id == f"{MESH_UUID}_000c"
    assert ColorMode.COLOR_TEMP in light.supported_color_modes
    assert light.color_mode == ColorMode.COLOR_TEMP
    assert light.min_color_temp_kelvin == 2700
    assert light.max_color_temp_kelvin == 6500
    assert light.available is True


async def test_turn_on_brightness(hass) -> None:
    """brightness=128 → set_lightness(0x000C, ~0.5); cached brightness ≈128, on."""
    light, coordinator = _light()

    await light.async_turn_on(brightness=128)

    call = coordinator.calls[-1]
    assert call[0] == "set_lightness"
    assert call[1] == UNICAST
    assert call[2] == pytest.approx(128 / 255, abs=1e-6)
    assert light.brightness == pytest.approx(128, abs=1)
    assert light.is_on is True


async def test_turn_on_color_temp(hass) -> None:
    """color_temp_kelvin=4000 → set_ctl_temperature(0x000C, 4000); cached temp."""
    light, coordinator = _light()

    await light.async_turn_on(color_temp_kelvin=4000)

    assert coordinator.calls[-1] == ("set_ctl_temperature", UNICAST, 4000)
    assert light.color_temp_kelvin == 4000
    assert light.is_on is True


async def test_turn_on_no_args(hass) -> None:
    """A bare turn_on → set_onoff(0x000C, True); is_on True."""
    light, coordinator = _light()

    await light.async_turn_on()

    assert coordinator.calls[-1] == ("set_onoff", UNICAST, True)
    assert light.is_on is True


async def test_turn_off(hass) -> None:
    """turn_off → set_onoff(0x000C, False); is_on False."""
    light, coordinator = _light()
    light._is_on = True

    await light.async_turn_off()

    assert coordinator.calls[-1] == ("set_onoff", UNICAST, False)
    assert light.is_on is False


async def test_onoff_only_node_is_onoff_mode(hass) -> None:
    """A node with only Generic OnOff supports exactly {ColorMode.ONOFF}."""
    light, _ = _light(_onoff_only_network(), unicast=0x0020)

    assert light.supported_color_modes == {ColorMode.ONOFF}
    assert light.color_mode == ColorMode.ONOFF
