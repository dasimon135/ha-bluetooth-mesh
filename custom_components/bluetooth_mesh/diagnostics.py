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
from .const import CONTROLLED_MODEL_IDS
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
            # Three different answers to "which application key", because a
            # mismatch between them is invisible on air: a node silently drops
            # anything encrypted with a key its models were not bound to.
            # What we encrypt with / what the export carries / what the models
            # we drive actually ask for.
            "app_key_index": coordinator.app_key_index,
            "app_key_indexes": [key.index for key in network.app_keys],
            "bound_app_key_indexes": list(
                network.bound_app_key_indexes(CONTROLLED_MODEL_IDS)
            ),
            "node_count": len(network.nodes),
        },
        "state": {
            "available": coordinator.available,
            "connected": coordinator.connected,
            "seq": coordinator.seq,
            # The unicast we transmit FROM. Sharing it with a node of the mesh
            # makes every message we send look like a replay to that node's
            # peers, which discard it without a trace.
            "src_addr": f"{coordinator.src_addr:#06x}",
            "keepalive_seconds": coordinator.keepalive_seconds,
        },
        # A subnet that beacons AND authenticates proves the imported keys are
        # the right ones; silence here points at the node, a mismatch at the
        # export.
        "beacon": (
            {
                "iv_index": coordinator.beacon.iv_index,
                "iv_update": coordinator.beacon.iv_update,
                "key_refresh": coordinator.beacon.key_refresh,
            }
            if coordinator.beacon is not None
            else None
        ),
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
                        "models": [
                            {
                                "id": f"{m.model_id:#06x}",
                                "bind": list(m.bound_appkey_indexes),
                            }
                            for m in element.models
                        ],
                    }
                    for element in node.elements
                ],
            }
            for node in network.nodes
        ],
    }
