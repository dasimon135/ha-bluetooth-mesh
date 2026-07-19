"""Config flow for the Bluetooth Mesh integration.

The user imports a mesh network provisioned by the ThingOS / Häfele Connect
Mesh app, which exports it as a ``.connect`` JSON file. Rather than a native
file-upload widget (unreliable across the various HA front ends), the flow
takes the *contents* of that file pasted into a multiline text field, parses
it with :func:`btmesh.network_model.Network.from_connect`, and — on success —
stores the raw JSON verbatim in the config entry. The runtime (a later task)
re-parses it at setup time, so no key bytes need to be serialised here.
"""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from btmesh.network_model import Network, NetworkModelError

from .const import CONF_CONNECT_JSON, DOMAIN

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONNECT_JSON): TextSelector(
            TextSelectorConfig(multiline=True, type=TextSelectorType.TEXT)
        ),
    }
)


class BluetoothMeshConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle importing a ``.connect`` mesh network into a config entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import a ``.connect`` network export pasted as JSON text."""
        errors: dict[str, str] = {}

        if user_input is not None:
            text = user_input[CONF_CONNECT_JSON]
            try:
                data = json.loads(text)
                network = Network.from_connect(data)
            except (json.JSONDecodeError, NetworkModelError):
                errors["base"] = "invalid_connect"
            else:
                await self.async_set_unique_id(network.uuid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=network.name or "Bluetooth Mesh",
                    data={CONF_CONNECT_JSON: text},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
