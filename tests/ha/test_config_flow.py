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

from custom_components.bluetooth_mesh import config_flow
from custom_components.bluetooth_mesh import coordinator as coordinator_mod
from custom_components.bluetooth_mesh.btmesh.network_model import Network
from custom_components.bluetooth_mesh.const import (
    CONF_CONNECT_JSON,
    CONF_INVERTED_CTL,
    CONF_KEEPALIVE,
    CONF_SRC_ADDR,
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
    """Text that is not JSON at all re-shows the form with an error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: "this is not json {{{"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_json"}


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


async def test_user_flow_says_why_the_export_was_rejected(hass) -> None:
    """`invalid_connect` on its own leaves the user with nowhere to go.

    Exports come from another vendor's app; a node missing a field, a truncated
    paste and a file that is not an export at all are three different problems
    with three different fixes, and the form used to render them identically.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: "{}"}
    )

    assert result["errors"] == {"base": "invalid_connect"}
    assert "netKeys" in result["description_placeholders"]["error"]


async def test_reconfigure_flow_says_why_the_export_was_rejected(hass) -> None:
    """The reconfigure form owes the same explanation as the import form."""
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

    assert result["errors"] == {"base": "invalid_json"}
    assert result["description_placeholders"]["error"]


async def test_options_flow_sets_keepalive_and_reloads_the_entry(hass) -> None:
    """The options flow stores the keep-alive timeout (0 = always connected).

    Storing it is only half the job: the coordinator reads the value once, at
    construction, so the entry has to be reloaded for a change to take effect.
    ``OptionsFlowWithReload`` does that itself — which is why this test has to
    survive a real reload, hence the mocked BLE seams and the unload at the end.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)

    with (
        # No proxy in range: the coordinator marks itself unavailable and
        # retries, which is enough for a reload to complete without a radio.
        patch.object(coordinator_mod, "find_proxy_address", return_value=None),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
        patch.object(
            coordinator_mod,
            "async_register_proxy_callback",
            return_value=lambda: None,
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_KEEPALIVE: 120}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_KEEPALIVE] == 120

        # The scheduled reload runs as a task; let it finish, then tear the
        # entry down so its periodic probe does not outlive the test.
        await hass.async_block_till_done()
        assert entry.runtime_data.keepalive_seconds == 120

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


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
    assert result["errors"] == {"base": "invalid_json"}


async def test_options_flow_stores_a_source_address_override(hass) -> None:
    """The escape hatch for the one collision an export cannot reveal.

    Exports carry no provisioner node, so if the vendor app claimed the address
    we transmit from, every message we send is dropped as a replay before any
    model sees it — invisibly. Moving off it has to be reachable from the UI.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)

    with (
        patch.object(coordinator_mod, "find_proxy_address", return_value=None),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
        patch.object(
            coordinator_mod,
            "async_register_proxy_callback",
            return_value=lambda: None,
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_KEEPALIVE: 0, CONF_SRC_ADDR: 0x0030}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_SRC_ADDR] == 0x0030

        await hass.async_block_till_done()
        assert entry.runtime_data.src_addr == 0x0030

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_truncated_paste_is_told_apart_from_a_bad_export(hass) -> None:
    """Unparseable text and a well-formed non-export need different advice.

    They also need different *languages*: the reason detail is interpolated
    into a translated sentence, so it cannot be hand-written English prose.
    The decoder's own message is the exception — it points at a byte offset in
    a file whose field names are English anyway.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECT_JSON: "this is not json {{{"}
    )

    assert result["errors"] == {"base": "invalid_json"}
    assert "not valid JSON" not in result["description_placeholders"]["error"]


async def test_options_flow_stores_inverted_lamps_as_addresses(hass) -> None:
    """The colour-temperature mirror is chosen per lamp, from the form.

    The selector deals in the hexadecimal form this integration writes unicasts
    in everywhere; what gets stored has to be the integer, because that is what
    the light platform matches node addresses against.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)

    with (
        patch.object(coordinator_mod, "find_proxy_address", return_value=None),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
        patch.object(
            coordinator_mod,
            "async_register_proxy_callback",
            return_value=lambda: None,
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert CONF_INVERTED_CTL in {
            str(key) for key in result["data_schema"].schema
        }

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_KEEPALIVE: 0,
                CONF_SRC_ADDR: 0,
                CONF_INVERTED_CTL: ["000c"],
            },
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_INVERTED_CTL] == [0x000C]

        await hass.async_block_till_done()
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_options_flow_can_clear_every_inverted_lamp(hass) -> None:
    """Unchecking the last lamp has to be storable, not read as "unset".

    Setup seeds the option from the old vendor rule, so the fixture's Häfele
    lamp starts checked. Turning the mirror off is exactly what issue #7 needs,
    and an empty list is the only way to say it.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)

    with (
        patch.object(coordinator_mod, "find_proxy_address", return_value=None),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
        patch.object(
            coordinator_mod,
            "async_register_proxy_callback",
            return_value=lambda: None,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.options[CONF_INVERTED_CTL] == [0x000C]  # seeded

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_KEEPALIVE: 0, CONF_SRC_ADDR: 0, CONF_INVERTED_CTL: []},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_INVERTED_CTL] == []

        await hass.async_block_till_done()
        # The reload must not put the seeded lamp back.
        assert entry.options[CONF_INVERTED_CTL] == []

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_options_flow_keeps_inverted_lamps_when_the_field_is_absent(
    hass,
) -> None:
    """Saving options must not drop a setting the form did not render.

    The field is omitted when the network has no colour-temperature lamp, and
    ``async_create_entry`` REPLACES the whole options dict — so anything not
    resubmitted is deleted. Deleting it is worse than it looks: the entry then
    reloads with the key absent, which is precisely the state the seed treats as
    "never configured", so it puts the vendor default back and silently undoes
    the user's choice.

    Asserted with an EMPTY stored list, because that is the case the two
    behaviours disagree on. A non-empty one would come back from the seed
    looking identical and prove nothing.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_CONNECT_JSON: _connect_text()},
        options={CONF_INVERTED_CTL: []},
        unique_id="0F0E0D0C-0B0A-0908-0706-050403020100",
    )
    entry.add_to_hass(hass)

    with (
        patch.object(coordinator_mod, "find_proxy_address", return_value=None),
        patch.object(coordinator_mod, "discovered_proxies", return_value=[]),
        patch.object(
            coordinator_mod,
            "async_register_proxy_callback",
            return_value=lambda: None,
        ),
        patch.object(
            config_flow.BluetoothMeshOptionsFlow, "_ctl_nodes", return_value=[]
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert CONF_INVERTED_CTL not in {
            str(key) for key in result["data_schema"].schema
        }

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_KEEPALIVE: 0, CONF_SRC_ADDR: 0}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_INVERTED_CTL] == []

        # The reload that follows must not read the survivor as "unconfigured".
        await hass.async_block_till_done()
        assert entry.options[CONF_INVERTED_CTL] == []

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
