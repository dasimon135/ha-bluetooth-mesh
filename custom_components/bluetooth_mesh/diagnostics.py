"""Diagnostics for the Bluetooth Mesh integration.

Support for this integration is almost always the same question: *which proxy
does Home Assistant see, and does it belong to my network?* This dump answers
it without a log-level round trip — the Network ID we look for, every 0x1828
advert currently visible, the IV Index and SEQ cursor in use, and each node's
composition (which decides what an entity can do).

**Nothing here may carry key material.** The config entry stores a whole
``.connect`` export verbatim — NetKey, AppKey and every node's DeviceKey — and
a diagnostics dump is meant to be pasted into a public issue. The entry data is
therefore never echoed; only derived, non-secret values are.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import BluetoothMeshConfigEntry
from .btmesh.crypto import k3
from .mesh_transport import discovered_proxies


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BluetoothMeshConfigEntry
) -> dict[str, Any]:
    """Return a redacted snapshot of the entry's runtime."""
    coordinator = entry.runtime_data
    network = coordinator.network

    return {
        "network": {
            "name": network.name,
            "uuid": network.uuid,
            # k3(NetKey) — the identifier a proxy advertises. Derived and
            # public: it is broadcast in the clear by every node.
            "network_id": k3(network.net_key).hex(),
            "iv_index": coordinator.iv_index,
            "net_key_index": network.net_key_index,
            "app_key_index": network.app_key_index,
            "node_count": len(network.nodes),
        },
        "state": {
            "available": coordinator.available,
            "connected": coordinator.connected,
            "seq": coordinator.seq,
            "keepalive_seconds": coordinator.keepalive_seconds,
        },
        # Every 0x1828 advert Home Assistant currently sees, so a "no proxy"
        # report can be told apart from a "wrong network" one at a glance.
        "proxies_seen": [
            {"address": address, "description": description}
            for address, description in discovered_proxies(hass)
        ],
        "nodes": [
            {
                "name": node.name,
                "unicast": f"{node.unicast:#06x}",
                "cid": f"{node.cid:#06x}",
                "elements": [
                    {
                        "index": element.index,
                        "unicast": f"{element.unicast:#06x}",
                        "models": [f"{m.model_id:#06x}" for m in element.models],
                    }
                    for element in node.elements
                ],
            }
            for node in network.nodes
        ],
    }
