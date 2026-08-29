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

from custom_components.bluetooth_mesh.btmesh.network_model import (
    Element,
    Model,
    Network,
    Node,
)
from custom_components.bluetooth_mesh.const import CONF_INVERTED_CTL
from custom_components.bluetooth_mesh.light import MeshLight, async_setup_entry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.connect.json"
UNICAST = 0x000C
MESH_UUID = "0F0E0D0C-0B0A-0908-0706-050403020100"


class FakeCoordinator:
    """Minimal coordinator: a real Network + recorded async_set_* calls."""

    def __init__(
        self,
        network: Network,
        *,
        available: bool = True,
        onoff: bool | None = True,
        lightness: int | None = 0x8000,
    ) -> None:
        self.network = network
        self.available = available
        self.calls: list[tuple] = []
        # What the lamp reports when asked (None = it stayed silent).
        self.onoff = onoff
        self.lightness = lightness
        self.listeners: list = []

    def async_add_listener(self, callback_):
        """Mirror the real coordinator: notified on availability changes."""
        self.listeners.append(callback_)
        return lambda: self.listeners.remove(callback_)

    def fire(self) -> None:
        for callback_ in list(self.listeners):
            callback_()

    async def async_set_onoff(self, unicast: int, on: bool) -> bool:
        self.calls.append(("set_onoff", unicast, on))
        return on

    async def async_set_lightness(self, unicast: int, level_0_1: float) -> int:
        self.calls.append(("set_lightness", unicast, level_0_1))
        return round(level_0_1 * 0xFFFF)

    async def async_get_lightness(self, unicast: int) -> int:
        self.calls.append(("get_lightness", unicast))
        # Pretend the lamp reports half brightness (0x8000 → ~128/255).
        return self.lightness

    async def async_get_onoff(self, unicast: int) -> bool | None:
        self.calls.append(("get_onoff", unicast))
        return self.onoff

    async def async_set_ctl(
        self, unicast: int, level_0_1: float, kelvin: int
    ) -> int:
        self.calls.append(("set_ctl", unicast, level_0_1, kelvin))
        return kelvin

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


def _light(
    network: Network | None = None,
    unicast: int = UNICAST,
    *,
    invert_ctl: bool = False,
) -> tuple[MeshLight, FakeCoordinator]:
    """Build a MeshLight for the node at ``unicast`` and its fake coordinator."""
    net = network or _fixture_network()
    coordinator = FakeCoordinator(net)
    node = next(n for n in net.nodes if n.unicast == unicast)
    light = MeshLight(coordinator, node, invert_ctl=invert_ctl)
    # Entities under test are not added to hass, so bypass the HA state-machine
    # write (we assert on the optimistic cache directly).
    light.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    return light, coordinator


def _entry(coordinator: FakeCoordinator, options: dict | None = None):
    """A minimal config-entry stand-in: runtime data plus the stored options."""
    return type(
        "Entry", (), {"runtime_data": coordinator, "options": options or {}}
    )()


async def test_setup_entry_creates_entity_with_ctl(hass) -> None:
    """The CTL-capable node yields one COLOR_TEMP light with the right unique_id."""
    coordinator = FakeCoordinator(_fixture_network())
    entry = _entry(coordinator)

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


async def test_turn_on_color_temp_while_off_also_turns_on(hass) -> None:
    """color_temp on an OFF lamp → CTL Temperature Set (temp element) + OnOff ON.

    The fixture node 0x000C hosts the Light CTL Temperature server (0x1306) on
    element 1 (unicast 0x000D), so a temperature change is routed there and
    carries NO lightness — brightness is untouched. That message does not switch
    the light on, so a bare temperature turn-on of an off lamp also sends OnOff
    ON. The lamp is unmarked, so the Kelvin goes out as requested; the mirror
    has its own tests.
    """
    light, coordinator = _light()  # a fresh entity is off

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl_temperature", 0x000D, 4000) in coordinator.calls
    assert ("set_onoff", UNICAST, True) in coordinator.calls  # actually lit up
    assert light.color_temp_kelvin == 4000  # display keeps the requested value
    assert light.is_on is True
    # Brightness was not sent (no set_lightness / set_ctl), so it stays unknown.
    assert not any(c[0] in ("set_ctl", "set_lightness") for c in coordinator.calls)


async def test_turn_on_color_temp_while_on_skips_redundant_onoff(hass) -> None:
    """Changing temperature on an already-on lamp sends ONLY the Temperature Set.

    No redundant OnOff, since the lamp is already lit and the temperature message
    leaves brightness alone.
    """
    light, coordinator = _light()
    light._is_on = True  # already on

    await light.async_turn_on(color_temp_kelvin=4000)

    assert coordinator.calls == [("set_ctl_temperature", 0x000D, 4000)]
    assert light.color_temp_kelvin == 4000
    assert light.is_on is True


async def test_turn_on_color_temp_without_temp_element_uses_ctl_set(hass) -> None:
    """A CTL node WITHOUT a 0x1306 element falls back to Light CTL Set.

    Then temperature must carry a lightness (full when none cached). The lamp is
    unmarked, so the Kelvin itself goes out as requested.
    """
    # element 0 has the CTL server (0x1303) but there is NO 0x1306 element.
    element0 = Element(
        index=0,
        unicast=0x0030,
        models=(
            Model(model_id=0x1000, bound_appkey_indexes=(0,)),
            Model(model_id=0x1300, bound_appkey_indexes=(0,)),
            Model(model_id=0x1303, bound_appkey_indexes=(0,)),
        ),
    )
    node = Node(
        uuid="99998888-7777-6666-5555-444433332222",
        unicast=0x0030,
        device_key=b"\x00" * 16,
        cid=0x07E9,
        name="CTL no temp element",
        elements=(element0,),
    )
    net = replace(_fixture_network(), nodes=(node,))
    light, coordinator = _light(network=net, unicast=0x0030)

    await light.async_turn_on(color_temp_kelvin=4000)

    assert coordinator.calls[-1] == ("set_ctl", 0x0030, 1.0, 4000)
    assert light.color_temp_kelvin == 4000


async def test_turn_on_no_args(hass) -> None:
    """A bare turn_on → set_onoff(0x000C, True); is_on True.

    Brightness is NOT read back (that caught mid-fade values); the lamp restores
    its own last level and the cache already tracks it across off/on.
    """
    light, coordinator = _light()

    await light.async_turn_on()

    assert coordinator.calls == [("set_onoff", UNICAST, True)]
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


# --------------------------------------------------- real state at startup


async def test_refresh_state_reads_the_lamp(hass) -> None:
    """Startup state comes from the lamp, not from an empty optimistic cache.

    After a Home Assistant restart the cache starts blank, so a lamp that is
    physically lit showed as off until someone touched it. Now that the proxy
    filter lets Status replies through, the entity can simply ask.
    """
    light, coordinator = _light()
    assert light.is_on is None  # unknown before asking

    await light.async_refresh_state()

    assert ("get_onoff", UNICAST) in coordinator.calls
    assert light.is_on is True
    assert light.brightness == 128  # 0x8000 of 0xFFFF


async def test_refresh_state_keeps_the_cache_when_the_mesh_is_silent(hass) -> None:
    """An unanswered GET must not invent a state."""
    light, coordinator = _light()
    coordinator.onoff = None
    light._is_on = True
    light._brightness = 42

    await light.async_refresh_state()

    assert light.is_on is True  # untouched
    assert light.brightness == 42
    assert ("get_lightness", UNICAST) not in coordinator.calls


async def test_refresh_state_does_not_read_brightness_of_an_off_lamp(hass) -> None:
    """An off lamp reports lightness 0; HA wants no brightness at all then."""
    light, coordinator = _light()
    coordinator.onoff = False

    await light.async_refresh_state()

    assert light.is_on is False
    assert light.brightness is None
    assert ("get_lightness", UNICAST) not in coordinator.calls


async def test_refresh_state_skips_brightness_for_an_onoff_only_node(hass) -> None:
    """A Generic OnOff node has no lightness server to ask."""
    light, coordinator = _light(_onoff_only_network(), unicast=0x0020)

    await light.async_refresh_state()

    assert light.is_on is True
    assert [c[0] for c in coordinator.calls] == ["get_onoff"]


async def test_added_to_hass_refreshes_in_the_background(hass) -> None:
    """Setup must not block on a mesh round trip, but must still refresh."""
    light, coordinator = _light()
    light.hass = hass
    light.entity_id = "light.mesh_test"

    await light.async_added_to_hass()
    await hass.async_block_till_done()

    assert ("get_onoff", UNICAST) in coordinator.calls
    assert light.is_on is True


async def test_no_refresh_while_the_mesh_is_unreachable(hass) -> None:
    """Asking an unreachable mesh is pointless — and the answer would be None."""
    light, coordinator = _light()
    coordinator.available = False
    light.hass = hass
    light.entity_id = "light.mesh_test"

    await light.async_added_to_hass()
    await hass.async_block_till_done()

    assert coordinator.calls == []


async def test_refreshes_when_the_mesh_becomes_reachable(hass) -> None:
    """The startup race is why this exists.

    At Home Assistant startup the entity is added before the ESPHome proxies
    have finished registering their scanners, so the mesh is not yet reachable
    and a one-shot read finds no proxy, returns None and never retries — the
    lamp stayed shown as off (observed live 2026-07-26). Refreshing when the
    coordinator BECOMES available fixes that, and re-reads after every
    reconnection too, catching whatever changed while we were away.
    """
    light, coordinator = _light()
    coordinator.available = False
    light.hass = hass
    light.entity_id = "light.mesh_test"
    await light.async_added_to_hass()
    await hass.async_block_till_done()
    assert coordinator.calls == []  # nothing asked yet

    coordinator.available = True
    coordinator.fire()
    await hass.async_block_till_done()

    assert ("get_onoff", UNICAST) in coordinator.calls
    assert light.is_on is True
    assert light.brightness == 128


async def test_availability_change_is_pushed_not_polled(hass) -> None:
    """The entity must not rely on HA's 30 s poll to notice it went stale."""
    light, coordinator = _light()

    assert light.should_poll is False


# ------------------------------------------------- never fabricate a state


async def test_a_fresh_entity_reports_unknown_not_off(hass) -> None:
    """Never assert a state that has not been read.

    A blank cache claiming *off* is not merely cosmetic: another integration
    acting on that fabricated value — a light group syncing its members, say —
    can physically switch the lamp off, and the lie becomes true. Until the
    first read answers, the honest answer is "unknown".
    """
    light, _ = _light()

    assert light.is_on is None
    assert light.state is None  # the state machine renders this as "unknown"


async def test_turn_on_from_an_unknown_state_still_switches_the_lamp_on(hass) -> None:
    """Not knowing must never be mistaken for knowing it is already on."""
    light, coordinator = _light()
    assert light.is_on is None

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_onoff", UNICAST, True) in coordinator.calls
    assert light.is_on is True


# ------------------------------------------- vendor colour-temperature quirk


def _standard_ctl_network() -> Network:
    """A spec-conformant CTL node from another vendor (not Häfele)."""
    element0 = Element(
        index=0,
        unicast=0x0040,
        models=(
            Model(model_id=0x1000, bound_appkey_indexes=(0,)),
            Model(model_id=0x1300, bound_appkey_indexes=(0,)),
            Model(model_id=0x1303, bound_appkey_indexes=(0,)),
        ),
    )
    node = Node(
        uuid="99998888-7777-6666-5555-444433332222",
        unicast=0x0040,
        device_key=bytes(16),
        cid=0x0059,  # Nordic Semiconductor, i.e. not the Häfele quirk
        name="Standard CTL",
        elements=(element0,),
    )
    return replace(_fixture_network(), nodes=(node,))


async def test_an_unmarked_lamp_is_not_mirrored(hass) -> None:
    """The mirror is a per-lamp quirk, not the spec.

    Some lamps map Light CTL temperature inversely, so the value is mirrored
    around the exposed range before sending. Applying that to a spec-conformant
    lamp inverts warm and cool end to end — and the README advertises standard
    SIG mesh lights.
    """
    light, coordinator = _light(_standard_ctl_network(), unicast=0x0040)

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl", 0x0040, 1.0, 4000) in coordinator.calls
    assert light.color_temp_kelvin == 4000


async def test_a_marked_lamp_is_mirrored(hass) -> None:
    """Mirrored around the exposed range: 2700 + 6500 - 4000 = 5200."""
    light, coordinator = _light(invert_ctl=True)

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl_temperature", 0x000D, 5200) in coordinator.calls


# The two below invert the rule this integration used to apply. Until 0.5.1 the
# mirror was gated on the company identifier alone, and issue #7 produced the
# lamp that disproves it: a Häfele node whose colour temperature came out
# backwards *because we mirrored it*. The quirk varies within a vendor, by model
# or firmware, so the CID must now decide nothing at all — asserted in both
# directions, because a half-removed rule would still pass one of them.


async def test_a_hafele_lamp_left_unmarked_is_not_mirrored(hass) -> None:
    """The fixture node is Häfele (CID 0x07E9) and must still pass through."""
    light, coordinator = _light()

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl_temperature", 0x000D, 4000) in coordinator.calls


async def test_a_non_hafele_lamp_marked_inverted_is_mirrored(hass) -> None:
    """Node 0x0040 is Nordic (CID 0x0059), and marked, so it is mirrored."""
    light, coordinator = _light(
        _standard_ctl_network(), unicast=0x0040, invert_ctl=True
    )

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl", 0x0040, 1.0, 5200) in coordinator.calls
    assert light.color_temp_kelvin == 4000


# --------------------------------- addressing the element that hosts a model


def _split_element_network() -> Network:
    """A node whose lighting servers do NOT all sit on element 0.

    Element 0 hosts Generic OnOff only; Light Lightness / Light CTL live on
    element 1 and the CTL Temperature server on element 2. A real composition
    is free to be laid out this way, and the entity must follow it.
    """
    node = Node(
        uuid="99998888-7777-6666-5555-444433332222",
        unicast=0x0030,
        device_key=b"\x00" * 16,
        cid=0x07E9,
        name="Split Lamp",
        elements=(
            Element(
                index=0,
                unicast=0x0030,
                models=(Model(model_id=0x1000, bound_appkey_indexes=(0,)),),
            ),
            Element(
                index=1,
                unicast=0x0031,
                models=(
                    Model(model_id=0x1300, bound_appkey_indexes=(0,)),
                    Model(model_id=0x1303, bound_appkey_indexes=(0,)),
                ),
            ),
            Element(
                index=2,
                unicast=0x0032,
                models=(Model(model_id=0x1306, bound_appkey_indexes=(0,)),),
            ),
        ),
    )
    return replace(_fixture_network(), nodes=(node,))


def _element0_has_no_lighting_network() -> Network:
    """A node whose element 0 hosts no lighting server at all."""
    node = Node(
        uuid="12345678-1234-1234-1234-123456789abc",
        unicast=0x0040,
        device_key=b"\x00" * 16,
        cid=0x07E9,
        name="Secondary Only",
        elements=(
            Element(
                index=0,
                unicast=0x0040,
                models=(Model(model_id=0x1201, bound_appkey_indexes=()),),
            ),
            Element(
                index=1,
                unicast=0x0041,
                models=(Model(model_id=0x1300, bound_appkey_indexes=(0,)),),
            ),
        ),
    )
    return replace(_fixture_network(), nodes=(node,))


async def test_brightness_is_addressed_to_the_element_hosting_lightness(hass) -> None:
    """A Set aimed at an element that does not host the model is ignored.

    Silently: the element has nothing to handle the opcode, so it neither acts
    nor answers. Sending to the node's primary address regardless of where the
    Light Lightness server actually lives is exactly that mistake.
    """
    light, coordinator = _light(_split_element_network(), unicast=0x0030)

    await light.async_turn_on(brightness=128)

    call = next(c for c in coordinator.calls if c[0] == "set_lightness")
    assert call[1] == 0x0031


async def test_onoff_is_addressed_to_the_element_hosting_generic_onoff(hass) -> None:
    light, coordinator = _light(_split_element_network(), unicast=0x0030)

    await light.async_turn_off()

    assert ("set_onoff", 0x0030, False) in coordinator.calls


async def test_ctl_is_addressed_to_the_element_hosting_light_ctl(hass) -> None:
    """Light CTL Set goes to the CTL server's element, not the node address."""
    network = _split_element_network()
    # Drop the dedicated temperature element so turn_on falls back to CTL Set.
    node = network.nodes[0]
    node = replace(node, elements=node.elements[:2])
    light, coordinator = _light(replace(network, nodes=(node,)), unicast=0x0030)

    await light.async_turn_on(color_temp_kelvin=4000)

    call = next(c for c in coordinator.calls if c[0] == "set_ctl")
    assert call[1] == 0x0031


async def test_refresh_reads_each_model_on_its_own_element(hass) -> None:
    light, coordinator = _light(_split_element_network(), unicast=0x0030)

    await light.async_refresh_state()

    assert ("get_onoff", 0x0030) in coordinator.calls
    assert ("get_lightness", 0x0031) in coordinator.calls


async def test_setup_creates_a_light_for_lighting_on_a_secondary_element(
    hass,
) -> None:
    """Gating entity creation on element 0 hid such a node entirely.

    Capability detection already scans every element (``node.has_model``), so
    refusing to create the entity unless element 0 carried the server was an
    inconsistency, not a policy.
    """
    coordinator = FakeCoordinator(_element0_has_no_lighting_network())
    entry = _entry(coordinator)

    added: list = []
    await async_setup_entry(hass, entry, lambda ents: added.extend(ents))

    assert len(added) == 1
    # The identity stays the NODE address; only the addressing follows elements.
    assert added[0].unique_id == f"{MESH_UUID}_0040"


async def test_a_node_with_no_lighting_server_gets_no_entity(hass) -> None:
    """Widening the gate must not start inventing lights for every node."""
    node = Node(
        uuid="dead0000-0000-0000-0000-000000000000",
        unicast=0x0050,
        device_key=b"\x00" * 16,
        cid=0x07E9,
        name="Remote",
        # 0x1001 is the Generic OnOff *Client* — a remote, not a light.
        elements=(
            Element(
                index=0,
                unicast=0x0050,
                models=(Model(model_id=0x1001, bound_appkey_indexes=(0,)),),
            ),
        ),
    )
    coordinator = FakeCoordinator(replace(_fixture_network(), nodes=(node,)))
    entry = _entry(coordinator)

    added: list = []
    await async_setup_entry(hass, entry, lambda ents: added.extend(ents))

    assert added == []


async def test_setup_entry_marks_only_the_lamps_listed_in_the_option(hass) -> None:
    """The stored option, not the company identifier, decides who is mirrored.

    The fixture node is Häfele, so under the pre-0.5.1 rule it would be
    mirrored either way. Asserting it from the option means listing it and
    seeing the mirror, then not listing it and seeing the value pass through.
    """
    coordinator = FakeCoordinator(_fixture_network())
    added: list = []
    await async_setup_entry(
        hass,
        _entry(coordinator, {CONF_INVERTED_CTL: [UNICAST]}),
        lambda ents: added.extend(ents),
    )
    light = added[0]
    light.async_write_ha_state = lambda: None  # type: ignore[method-assign]

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl_temperature", 0x000D, 5200) in coordinator.calls


async def test_setup_entry_leaves_a_lamp_absent_from_the_option_alone(hass) -> None:
    """Same node, same vendor, not listed: the Kelvin goes out untouched."""
    coordinator = FakeCoordinator(_fixture_network())
    added: list = []
    await async_setup_entry(
        hass,
        _entry(coordinator, {CONF_INVERTED_CTL: []}),
        lambda ents: added.extend(ents),
    )
    light = added[0]
    light.async_write_ha_state = lambda: None  # type: ignore[method-assign]

    await light.async_turn_on(color_temp_kelvin=4000)

    assert ("set_ctl_temperature", 0x000D, 4000) in coordinator.calls
