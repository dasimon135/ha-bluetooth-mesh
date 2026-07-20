"""Constants for the Bluetooth Mesh integration."""

from __future__ import annotations

DOMAIN = "bluetooth_mesh"

# Config entry keys (populated by the config flow in a later task).
CONF_NETWORK = "network"
CONF_NETWORK_NAME = "network_name"

# Raw ``.connect`` network export JSON, stored verbatim in the config entry.
# The runtime (B3) re-parses it via ``Network.from_connect`` at setup time.
CONF_CONNECT_JSON = "connect_json"

# Options-flow key: how long (seconds) to keep the proxy connection open after
# the last command before dropping it to free the lamp's single proxy slot.
# 0 = keep it always open (most responsive; the Häfele app then cannot connect
# to the lamp while the integration is loaded). A positive value trades a
# multi-second cold-start after that idle period for letting the vendor app
# reclaim the lamp when Home Assistant is not actively driving it.
CONF_KEEPALIVE = "keepalive_seconds"
DEFAULT_KEEPALIVE = 0
