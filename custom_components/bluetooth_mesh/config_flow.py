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
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .btmesh.network_model import Network, NetworkModelError
from .const import (
    CONF_CONNECT_JSON,
    CONF_INVERTED_CTL,
    CONF_KEEPALIVE,
    CONF_SRC_ADDR,
    DEFAULT_KEEPALIVE,
    DEFAULT_SRC_ADDR,
    DOMAIN,
    MODEL_LIGHT_CTL,
)


def _parse(text: str) -> tuple[Network | None, str, str]:
    """Parse a pasted ``.connect`` export.

    Returns ``(network, "", "")`` on success and ``(None, error_key, detail)``
    otherwise, where ``error_key`` selects the translated form error and
    ``detail`` is interpolated into it.

    The two failures are separated because their fixes differ: text that is
    not JSON at all is usually a truncated or word-wrapped paste, whereas a
    well-formed document that is not an export means the wrong file. Only the
    sentence around the detail is translated — the detail itself stays in the
    parser's words, since it names JSON fields that are English in the file.
    """
    try:
        return Network.from_connect(json.loads(text)), "", ""
    except json.JSONDecodeError as exc:
        return None, "invalid_json", str(exc)
    except NetworkModelError as exc:
        return None, "invalid_connect", str(exc)


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

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "BluetoothMeshOptionsFlow":
        """Expose the options flow (keep-alive tuning)."""
        return BluetoothMeshOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import a ``.connect`` network export pasted as JSON text."""
        errors: dict[str, str] = {}

        reason = ""
        if user_input is not None:
            text = user_input[CONF_CONNECT_JSON]
            network, error_key, reason = _parse(text)
            if network is None:
                errors["base"] = error_key
            else:
                await self.async_set_unique_id(network.identifier)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=network.name or "Bluetooth Mesh",
                    data={CONF_CONNECT_JSON: text},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"error": reason},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the stored export with a freshly exported one.

        Networks change: a node is added, a key is refreshed. Without this the
        only way to import the new export was to delete the entry and re-add
        it, which costs every entity id and the history behind it.
        """
        errors: dict[str, str] = {}
        reason = ""

        if user_input is not None:
            text = user_input[CONF_CONNECT_JSON]
            network, error_key, reason = _parse(text)
            if network is None:
                errors["base"] = error_key
            else:
                await self.async_set_unique_id(network.identifier)
                # Pasting a *different* network here would silently repoint
                # every entity at nodes that are not theirs.
                self._abort_if_unique_id_mismatch(reason="wrong_network")
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates={CONF_CONNECT_JSON: text},
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"error": reason},
        )


class BluetoothMeshOptionsFlow(OptionsFlowWithReload):
    """Tune runtime behaviour: how long to hold the proxy connection open.

    A mesh node offers a single proxy connection slot. Holding it open makes
    commands instant but locks the vendor (Häfele Connect Mesh) app out of the
    lamp; dropping it after an idle period hands the slot back at the cost of a
    multi-second reconnect on the next command. ``0`` keeps it always open.

    ``OptionsFlowWithReload`` reloads the entry itself when the options change.
    A config-entry update listener did that job before; Home Assistant reports
    that pattern as deprecated and stops honouring it in 2026.12, and the two
    are mutually exclusive — registering a listener makes this class raise.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show/store the keep-alive timeout."""
        if user_input is not None:
            data = {
                CONF_KEEPALIVE: int(user_input[CONF_KEEPALIVE]),
                CONF_SRC_ADDR: int(user_input[CONF_SRC_ADDR]),
            }
            # The field is absent from the form when the network has no CTL
            # lamp. Carry the stored value through rather than letting it go:
            # this replaces the whole options dict, and a dropped key is not
            # read as "empty" but as "never configured" — so the next setup
            # would seed the vendor default back over the user's choice.
            if CONF_INVERTED_CTL in user_input:
                data[CONF_INVERTED_CTL] = [
                    int(value, 16) for value in user_input[CONF_INVERTED_CTL]
                ]
            elif CONF_INVERTED_CTL in self.config_entry.options:
                data[CONF_INVERTED_CTL] = self.config_entry.options[
                    CONF_INVERTED_CTL
                ]
            return self.async_create_entry(data=data)

        current = self.config_entry.options.get(
            CONF_KEEPALIVE, DEFAULT_KEEPALIVE
        )
        current_src = self.config_entry.options.get(
            CONF_SRC_ADDR, DEFAULT_SRC_ADDR
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_KEEPALIVE, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=3600, step=1, unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_SRC_ADDR, default=current_src
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=0x7FFF, step=1, mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        if ctl_nodes := self._ctl_nodes():
            current_inverted = self.config_entry.options.get(
                CONF_INVERTED_CTL, []
            )
            schema = schema.extend(
                {
                    vol.Optional(
                        CONF_INVERTED_CTL,
                        default=[f"{a:04x}" for a in current_inverted],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=f"{node.unicast:04x}",
                                    label=node.name
                                    or f"Mesh {node.unicast:04x}",
                                )
                                for node in ctl_nodes
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            )
        return self.async_show_form(step_id="init", data_schema=schema)

    def _ctl_nodes(self) -> list:
        """The nodes whose colour temperature the mirror could apply to.

        Read from the stored export rather than from the running coordinator so
        the form still lists the lamps when no proxy is in range. A damaged
        export is not this step's problem — the setup already refuses the entry
        with a message pointing at the reconfigure flow — so it degrades to an
        empty list and simply omits the field.
        """
        try:
            network = Network.from_connect(
                json.loads(self.config_entry.data[CONF_CONNECT_JSON])
            )
        except (json.JSONDecodeError, NetworkModelError, KeyError):
            return []
        return [n for n in network.nodes if n.has_model(MODEL_LIGHT_CTL)]
