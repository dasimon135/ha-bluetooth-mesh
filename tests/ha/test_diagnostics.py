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
from custom_components.bluetooth_mesh.const import CONF_CONNECT_JSON, DOMAIN
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


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: FIXTURE.read_text(encoding="utf-8")},
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
