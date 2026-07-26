"""Tests for the bluetooth_mesh config flow (Task B2).

Drive the ``.connect`` import flow end-to-end against a real ``hass``. Run in
the daikin_madoka venv (HA + pytest-homeassistant-custom-component)::

    PYTHONPATH="tests/ha/_winshims;src" .../daikin_madoka/.venv/Scripts/python.exe \
        -m pytest tests/ha/test_config_flow.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# The library venv (uv run pytest) also collects tests/ha but has no HA; this
# skips there and runs for real in the daikin_madoka venv.
pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bluetooth_mesh.btmesh.network_model import Network
from custom_components.bluetooth_mesh.const import (
    CONF_CONNECT_JSON,
    CONF_KEEPALIVE,
    DOMAIN,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "sample.connect.json"
)


def _connect_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


async def test_user_flow_imports_connect_network(hass) -> None:
    """A valid .connect paste creates an entry storing the raw JSON verbatim."""
    text = _connect_text()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: text}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fabricated Test Mesh"

    # The raw JSON round-trips back into a Network with the expected keys/nodes.
    stored = result["data"][CONF_CONNECT_JSON]
    network = Network.from_connect(json.loads(stored))
    assert network.net_key == bytes.fromhex("00112233445566778899aabbccddeeff")
    assert len(network.nodes) == 1
    assert network.nodes[0].unicast == 0x000C


async def test_user_flow_rejects_garbage(hass) -> None:
    """Non-JSON / non-.connect text re-shows the form with an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: "this is not json {{{"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_connect"}


async def test_user_flow_rejects_valid_json_but_not_connect(hass) -> None:
    """Well-formed JSON that is not a .connect document is rejected too."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: "{}"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_connect"}


async def test_options_flow_sets_keepalive(hass) -> None:
    """The options flow stores the keep-alive timeout (0 = always connected)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_KEEPALIVE: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_KEEPALIVE] == 120


async def test_duplicate_network_aborts(hass) -> None:
    """Importing the same mesh (same UUID) twice aborts on the second run."""
    text = _connect_text()

    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_CONNECT_JSON: text}
    )
    assert first["type"] is FlowResultType.CREATE_ENTRY

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {CONF_CONNECT_JSON: text}
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


# --------------------------------------------- identity and reconfiguration


def _text_without_uuid() -> str:
    """The fixture export with its meshUUID blanked out."""
    doc = json.loads(_connect_text())
    doc["meshUUID"] = ""
    return json.dumps(doc)


async def test_unique_id_falls_back_to_the_network_id(hass) -> None:
    """An export without a meshUUID must still identify its network.

    Falling back to an empty string made every such network collide: the
    second one imported aborted as already_configured. The Network ID —
    k3(NetKey) — is the subnet's real on-air identity and is already computed
    everywhere else.
    """
    text = _text_without_uuid()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: text}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    network = Network.from_connect(json.loads(text))
    assert entry.unique_id == network.identifier
    assert entry.unique_id  # not the empty string


async def test_reconfigure_replaces_the_export(hass) -> None:
    """Re-importing a network must not mean deleting the entry.

    A re-export after adding a node used to cost every entity id and its
    history, because the only way back in was to remove and re-add.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: "{}"},
        unique_id=Network.from_connect(json.loads(_connect_text())).identifier,
    )
    entry.add_to_hass(hass)

    # A successful reconfigure reloads the entry for real, which would start
    # the coordinator and reach for the Bluetooth stack; the flow is what is
    # under test here, not setup.
    with patch(
        "custom_components.bluetooth_mesh.async_setup_entry", return_value=True
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CONNECT_JSON: _connect_text()}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CONNECT_JSON] == _connect_text()


async def test_reconfigure_refuses_a_different_network(hass) -> None:
    """Pasting another network's export would silently repoint every entity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id="a-completely-different-network",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: _connect_text()}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_network"


async def test_reconfigure_rejects_garbage(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id=Network.from_connect(json.loads(_connect_text())).identifier,
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: "not json"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_connect"}
