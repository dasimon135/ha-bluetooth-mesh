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
from .const import CONF_INVERTED_CTL, CONTROLLED_MODEL_IDS, MODEL_LIGHT_CTL
from .mesh_transport import discovered_proxies

# How many nodes the composition probe interrogates. Each one is a mesh round
# trip inside a diagnostics download, so the dump stays bounded on a large
# network; whatever is left out is reported rather than silently dropped.
PROBE_MAX_NODES = 6


async def _probe(coordinator) -> dict[str, Any]:
    """Ask each node, under its device key, whether it is there and what it is.

    Two requests, because they answer different questions and only one of them
    is trustworthy when it fails:

    * **Config Relay Get** — four bytes of answer, unsegmented in both
      directions. This is ``answered``, and it is the honest reachability
      signal: our message reached the node's Config Server and its reply came
      back. It needs no AppKey binding, no Light LC mode and no vendor model, so
      it separates "never arrived" from "arrived and was ignored". Its content
      matters too: a node that does not relay forwards nothing from our proxy
      connection into the rest of the mesh.
    * **Composition Data Get** — what the node says it *is*, against which the
      export (the vendor app's account of it) can be checked. Its Status is
      segmented. That used to make a null here meaningless, because the stack
      transmitted no Segment Acks and the reply could never complete (#9);
      since 0.4.6 it acknowledges them, so on a node that *answered* the relay
      request a null composition is now a finding in its own right rather than
      an artefact of our own transport.

    Skipped entirely when no proxy connection is held: a diagnostics download
    should not spend the connect timeout dialling a proxy that is not there.
    """
    if not coordinator.connected:
        return {"ran": False, "reason": "no proxy connection held"}

    nodes = coordinator.network.nodes[:PROBE_MAX_NODES]
    results: list[dict[str, Any]] = []
    for node in nodes:
        relay = await coordinator.async_get_relay(node.unicast)
        composition = await coordinator.async_get_composition(node.unicast)
        # Only for lamps that have a temperature to bound. The inversion
        # workaround mirrors around this range, so without it a dump cannot
        # explain a lamp whose warm and cool land off-centre.
        ctl_range = (
            await coordinator.async_get_ctl_temperature_range(node.unicast)
            if node.has_model(MODEL_LIGHT_CTL)
            else None
        )
        results.append(
            {
                "unicast": f"{node.unicast:#06x}",
                # Null means the node gave none and the entity is using the
                # conventional default, which is a different situation from a
                # node that named its own limits.
                "ctl_range": (
                    [str(ctl_range[0]), str(ctl_range[1])]
                    if ctl_range is not None
                    else None
                ),
                "answered": relay is not None,
                "relay": (
                    {
                        "enabled": relay.enabled,
                        "supported": relay.supported,
                        "retransmit_count": relay.retransmit_count,
                        "retransmit_interval_steps": relay.retransmit_interval_steps,
                    }
                    if relay is not None
                    else None
                ),
                "composition": _composition(composition),
            }
        )
    return {
        "ran": True,
        # Stated in the dump itself, because the two fields fail differently
        # and reading one as the other is the mistake this probe was first
        # shipped inviting.
        "note": (
            "'answered' is a Config Relay Get round trip and is the reachability "
            "signal. 'composition' comes from a segmented Status; since 0.4.6 "
            "this stack does acknowledge segments, so a null composition on a "
            "node that answered is a finding, not an artefact of our transport."
        ),
        "nodes": results,
        "not_probed": len(coordinator.network.nodes) - len(nodes),
    }


def _composition(composition) -> dict[str, Any] | None:
    """Serialise a CompositionData, or ``None`` when the node did not send one."""
    if composition is None:
        return None
    return {
        "cid": f"{composition.cid:#06x}",
        "pid": f"{composition.pid:#06x}",
        "vid": f"{composition.vid:#06x}",
        "crpl": composition.crpl,
        "features": f"{composition.features:#06x}",
        "elements": [
            {
                "index": index,
                "sig_models": [f"{m:#06x}" for m in element.sig_models],
                "vendor_models": [
                    f"{company:#06x}:{model:#06x}"
                    for company, model in element.vendor_models
                ],
            }
            for index, element in enumerate(composition.elements)
        ],
    }


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
            # The lamps whose colour temperature we mirror before sending. A
            # lamp reported as showing warm when Home Assistant says cool is
            # either natively inverted or being inverted BY US, and without
            # this line a dump cannot tell the two apart (issue #7).
            "inverted_ctl": [
                f"{unicast:#06x}"
                for unicast in entry.options.get(CONF_INVERTED_CTL, ())
            ],
        },
        # What each node says it IS, asked under its device key. The section
        # above is the vendor app's account of the network; this is the
        # network's own — and the only place the two can be compared.
        "probe": await _probe(coordinator),
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
