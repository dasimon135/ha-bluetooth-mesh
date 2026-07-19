"""Tests for the bluetooth_mesh config flow (Task B2).

Drive the ``.connect`` import flow end-to-end against a real ``hass``. Run in
the daikin_madoka venv (HA + pytest-homeassistant-custom-component)::

    PYTHONPATH="tests/ha/_winshims;src" .../daikin_madoka/.venv/Scripts/python.exe \
        -m pytest tests/ha/test_config_flow.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The library venv (uv run pytest) also collects tests/ha but has no HA; this
# skips there and runs for real in the daikin_madoka venv.
pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.data_entry_flow import FlowResultType

from btmesh.network_model import Network
from custom_components.bluetooth_mesh.const import CONF_CONNECT_JSON, DOMAIN

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
