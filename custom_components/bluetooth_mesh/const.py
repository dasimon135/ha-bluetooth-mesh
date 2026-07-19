"""Constants for the Bluetooth Mesh integration."""

from __future__ import annotations

DOMAIN = "bluetooth_mesh"

# Config entry keys (populated by the config flow in a later task).
CONF_NETWORK = "network"
CONF_NETWORK_NAME = "network_name"

# Raw ``.connect`` network export JSON, stored verbatim in the config entry.
# The runtime (B3) re-parses it via ``Network.from_connect`` at setup time.
CONF_CONNECT_JSON = "connect_json"
