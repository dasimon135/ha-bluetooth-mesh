"""Diagnostics tests.

The config entry stores a whole ``.connect`` export verbatim — NetKey, AppKey
and every node's DeviceKey. A diagnostics dump is meant to be pasted into a
public issue, so the first thing these tests assert is that none of it leaks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bluetooth_mesh.btmesh.network_model import Network
from custom_components.bluetooth_mesh.const import (
    CONF_CONNECT_JSON,
    CONF_INVERTED_CTL,
    DOMAIN,
)
from custom_components.bluetooth_mesh.diagnostics import (
    async_get_config_entry_diagnostics,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.connect.json"
UNICAST = 0x000C


class FakeCoordinator:
    def __init__(self, network: Network) -> None:
        self.network = network
        self.available = True
        self.iv_index = 0
        self.seq = 0x1234
        self.keepalive_seconds = 0
        self.connected = True
        self.beacon = None
        self.src_addr = 0x7FFF
        self.app_key_index = 0
        # What each node answers to the device-keyed probe (None = silence).
        self.compositions: dict[int, object] = {}
        self.relays: dict[int, object] = {}
        self.probed: list[int] = []

    async def async_get_composition(self, unicast: int):
        self.probed.append(unicast)
        return self.compositions.get(unicast)

    async def async_get_relay(self, unicast: int):
        self.probed.append(unicast)
        return self.relays.get(unicast)


def _entry(hass, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: FIXTURE.read_text(encoding="utf-8")},
        options=options or {},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = FakeCoordinator(Network.from_connect_file(str(FIXTURE)))
    return entry


async def test_no_key_material_is_ever_dumped(hass) -> None:
    """The one requirement that matters: a dump is safe to paste in an issue."""
    entry = _entry(hass)
    network = entry.runtime_data.network

    dump = json.dumps(await async_get_config_entry_diagnostics(hass, entry))

    assert network.net_key.hex() not in dump.lower()
    assert network.app_key.hex() not in dump.lower()
    for node in network.nodes:
        assert node.device_key.hex() not in dump.lower()
    # The raw export that holds all three must not be echoed either.
    assert CONF_CONNECT_JSON not in dump
    assert "deviceKey" not in dump


async def test_dump_carries_what_support_actually_needs(hass) -> None:
    """Network ID, IV Index, SEQ and the proxy view — the recurring questions."""
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["network"]["network_id"]  # k3(NetKey): identifies the subnet
    assert dump["network"]["iv_index"] == 0
    assert dump["state"]["seq"] == 0x1234
    assert dump["state"]["available"] is True
    assert dump["state"]["connected"] is True
    assert dump["state"]["keepalive_seconds"] == 0
    assert "proxies_seen" in dump


async def test_dump_lists_the_node_composition(hass) -> None:
    """Which models a node exposes is what decides the entity's capabilities."""
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    node = next(n for n in dump["nodes"] if n["unicast"] == "0x000c")
    assert node["cid"] == "0x07e9"
    models = {
        m["id"]: m["bind"] for element in node["elements"] for m in element["models"]
    }
    assert "0x1300" in models  # Light Lightness server
    assert "0x1303" in models  # Light CTL server


async def test_dump_shows_which_app_key_each_model_binds(hass) -> None:
    """The binding decides whether a node can understand us at all.

    A model only accepts messages encrypted with a key it was bound to, so
    "which key does the export hold, and which one do the models ask for" is
    the question behind an integration that transmits perfectly and is ignored.
    """
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["network"]["app_key_indexes"] == [0]  # what the export carries
    assert dump["network"]["app_key_index"] == 0  # what we encrypt with
    assert dump["network"]["bound_app_key_indexes"] == [0]  # what models want
    node = next(n for n in dump["nodes"] if n["unicast"] == "0x000c")
    lightness = next(
        m for m in node["elements"][0]["models"] if m["id"] == "0x1300"
    )
    assert lightness["bind"] == [0]


async def test_dump_names_the_address_we_transmit_from(hass) -> None:
    """Sharing it with a node silently mutes us; it must be visible."""
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["state"]["src_addr"] == "0x7fff"


def _composition():
    """A CompositionData as the library would return it."""
    from custom_components.bluetooth_mesh.btmesh.access import (
        CompositionData,
        CompositionElement,
    )

    return CompositionData(
        page=0,
        cid=0x07E9,
        pid=0x1510,
        vid=0x3005,
        crpl=200,
        features=0x0003,
        elements=(
            CompositionElement(
                loc=0x0000,
                sig_models=(0x0000, 0x1300),
                vendor_models=((0x07E9, 0x1000),),
            ),
        ),
    )


def _relay():
    from custom_components.bluetooth_mesh.btmesh.access import RelayStatus

    return RelayStatus(
        enabled=True, supported=True,
        retransmit_count=2, retransmit_interval_steps=5,
    )


async def test_probe_reachability_comes_from_the_unsegmented_request(hass) -> None:
    """``answered`` must not depend on a segmented reply reassembling.

    The Config Relay round trip is four bytes in each direction; a Composition
    Data Status is segmented and has more ways to fail. Keeping them apart is
    the whole point of asking twice.
    """
    entry = _entry(hass)
    entry.runtime_data.relays = {UNICAST: _relay()}
    entry.runtime_data.compositions = {}  # segmented status never completes

    dump = await async_get_config_entry_diagnostics(hass, entry)

    node = dump["probe"]["nodes"][0]
    assert node["answered"] is True
    assert node["composition"] is None
    assert node["relay"]["enabled"] is True
    assert node["relay"]["retransmit_count"] == 2


async def test_probe_reports_what_the_node_says_it_is(hass) -> None:
    """The export is the vendor app's account of the node; this is the node's."""
    entry = _entry(hass)
    entry.runtime_data.relays = {UNICAST: _relay()}
    entry.runtime_data.compositions = {UNICAST: _composition()}

    dump = await async_get_config_entry_diagnostics(hass, entry)

    node = dump["probe"]["nodes"][0]
    assert node["unicast"] == "0x000c"
    assert node["composition"]["cid"] == "0x07e9"
    assert node["composition"]["elements"][0]["sig_models"] == ["0x0000", "0x1300"]
    assert node["composition"]["elements"][0]["vendor_models"] == ["0x07e9:0x1000"]


async def test_probe_records_an_unreachable_node_as_unanswered(hass) -> None:
    """Silence on the *unsegmented* request is the finding."""
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    node = dump["probe"]["nodes"][0]
    assert node["answered"] is False
    assert node["relay"] is None
    assert node["composition"] is None


async def test_probe_note_no_longer_disowns_the_composition_field(hass) -> None:
    """The caveat outlived the defect it described (#9).

    v0.4.4 shipped a note telling the reader that a null composition proves
    nothing, because the stack sent no Segment Acks and a segmented Status
    could never complete. It sends them now, so that sentence would hand the
    reader a reason to discard the one field that has become informative.
    """
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)
    note = dump["probe"]["note"]

    assert "proves nothing" not in note
    assert "no Segment Acks" not in note
    assert "acknowledge" in note  # says what changed, rather than going silent


async def test_probe_is_skipped_while_disconnected(hass) -> None:
    """Downloading diagnostics must not spend 20s dialling an absent proxy."""
    entry = _entry(hass)
    entry.runtime_data.connected = False

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["probe"]["ran"] is False
    assert entry.runtime_data.probed == []


async def test_dump_names_the_lamps_whose_temperature_is_mirrored(hass) -> None:
    """A dump has to be able to answer "is the inversion ours?".

    Issue #7 cost a round trip on exactly that question: a lamp showed warm
    when Home Assistant said cool, and nothing in the dump distinguished a
    natively inverted lamp from one this integration was inverting itself.
    """
    entry = _entry(hass, {CONF_INVERTED_CTL: [UNICAST]})

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["state"]["inverted_ctl"] == ["0x000c"]


async def test_dump_reports_no_mirrored_lamp_as_an_empty_list(hass) -> None:
    """Empty is an answer, and a different one from the key being missing."""
    entry = _entry(hass)

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["state"]["inverted_ctl"] == []
