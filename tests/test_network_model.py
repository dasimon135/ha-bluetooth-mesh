"""Tests for the MeshNetwork (.connect import) parsing model.

Loads a SANITIZED, FABRICATED ThingOS ``.connect`` export (no real keys) and
asserts the parsed Network / Node / Element / Model structure. See
``tests/fixtures/sample.connect.json``.
"""

import json
import os

import pytest

from btmesh.network_model import (
    Element,
    Model,
    Network,
    NetworkModelError,
    Node,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.connect.json")

NET_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
APP_KEY = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")
DEVICE_KEY = bytes.fromhex("112233445566778899aabbccddeeff00")


def test_from_connect_file_keys_and_network_metadata():
    net = Network.from_connect_file(FIXTURE)
    assert net.name == "Fabricated Test Mesh"
    assert net.uuid == "0F0E0D0C-0B0A-0908-0706-050403020100"
    assert net.net_key == NET_KEY
    assert net.net_key_index == 0
    assert net.app_key == APP_KEY
    assert net.app_key_index == 0
    assert net.iv_index == 0  # absent in .connect → default 0


def test_from_connect_file_single_node_fields():
    net = Network.from_connect_file(FIXTURE)
    assert len(net.nodes) == 1
    node = net.nodes[0]
    assert isinstance(node, Node)
    assert node.unicast == 0x000C
    assert node.device_key == DEVICE_KEY
    assert node.cid == 0x07E9
    assert node.name == "Test Lamp"
    assert node.uuid == "AABBCCDD-EEFF-0011-2233-445566778899"


def test_node_has_model():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    assert node.has_model(0x1000) is True
    assert node.has_model(0x1300) is True
    assert node.has_model(0x9999) is False


def test_element_for_model_primary():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    elem = node.element_for_model(0x1000)
    assert isinstance(elem, Element)
    assert elem.index == 0
    assert elem.unicast == 0x000C  # primary element == node unicast


def test_element_for_model_secondary():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    elem = node.element_for_model(0x1001)
    assert elem is not None
    # 0x1001 lives on element 1 (first match) → node_unicast + 1
    assert elem.unicast == 0x000D
    assert elem.unicast in (0x000D, 0x000E)


def test_element_for_model_missing_returns_none():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    assert node.element_for_model(0x9999) is None


def test_element_unicast_offsets():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    assert [e.unicast for e in node.elements] == [0x000C, 0x000D, 0x000E]
    assert [e.index for e in node.elements] == [0, 1, 2]


def test_vendor_model_parsed():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    elem0 = node.elements[0]
    vendor = next(m for m in elem0.models if m.model_id == 0x07E91000)
    assert isinstance(vendor, Model)
    assert vendor.is_vendor is True
    assert vendor.bound_appkey_indexes == (0,)
    # A SIG model on the same element is not vendor.
    sig = next(m for m in elem0.models if m.model_id == 0x1000)
    assert sig.is_vendor is False


def test_element_has_model():
    node = Network.from_connect_file(FIXTURE).nodes[0]
    assert node.elements[0].has_model(0x1300) is True
    assert node.elements[0].has_model(0x1001) is False
    assert node.elements[1].has_model(0x1001) is True


def test_from_connect_accepts_dict():
    with open(FIXTURE, encoding="utf-8") as fh:
        data = json.load(fh)
    net = Network.from_connect(data)
    assert net.nodes[0].unicast == 0x000C


def test_malformed_empty_dict_raises():
    with pytest.raises(NetworkModelError):
        Network.from_connect({})


def test_bad_hex_key_raises():
    with open(FIXTURE, encoding="utf-8") as fh:
        data = json.load(fh)
    data["netKeys"][0]["key"] = "not-hex-zz"
    with pytest.raises(NetworkModelError):
        Network.from_connect(data)


def test_missing_nodes_raises():
    with open(FIXTURE, encoding="utf-8") as fh:
        data = json.load(fh)
    del data["nodes"]
    with pytest.raises(NetworkModelError):
        Network.from_connect(data)


def test_name_fallback_to_tos_node_type():
    with open(FIXTURE, encoding="utf-8") as fh:
        data = json.load(fh)
    data["nodes"][0]["tos_devices"] = []
    net = Network.from_connect(data)
    assert net.nodes[0].name == "com.example.fabricated.testlamp.v1"


# ------------------------------------------------- tolerant node parsing


def _connect_doc(nodes):
    return {
        "meshName": "T",
        "meshUUID": "u",
        "netKeys": [{"key": "7dd7364cd842ad18c17c2b820c84c3d6", "index": 0}],
        "appKeys": [{"key": "63964771734fbd76e3b40519d1d94a48", "index": 0}],
        "nodes": nodes,
    }


_GOOD_NODE = {
    "UUID": "n1",
    "unicastAddress": "000C",
    "deviceKey": "9d6dd0e96eb25dc19a40ed9914f8f03f",
    "cid": "07E9",
    "elements": [{"index": 0, "models": [{"modelId": "1000", "bind": [0]}]}],
}


def test_one_unparseable_node_does_not_sink_the_whole_import():
    """A single odd entry used to make the entire network unusable.

    Exports are written by another vendor's app; one node missing a field it
    never promised (a provisioner record, a firmware quirk) would raise, the
    config flow would answer a flat "not a valid export", and there was no way
    to tell which node was at fault. The usable nodes are what matter.
    """
    doc = _connect_doc([_GOOD_NODE, {"UUID": "broken"}])

    network = Network.from_connect(doc)

    assert len(network.nodes) == 1
    assert network.nodes[0].unicast == 0x000C


def test_keys_are_still_a_hard_failure():
    """Without keys there is no network at all — that must still raise."""
    doc = _connect_doc([_GOOD_NODE])
    del doc["netKeys"]
    with pytest.raises(NetworkModelError):
        Network.from_connect(doc)


def test_a_document_whose_every_node_is_broken_still_raises():
    """Silently importing an empty network would look like success."""
    doc = _connect_doc([{"UUID": "broken"}, {"UUID": "also-broken"}])
    with pytest.raises(NetworkModelError):
        Network.from_connect(doc)


def test_an_export_with_no_nodes_at_all_is_accepted():
    """An empty list is a legitimate (if useless) network, not a parse error."""
    assert Network.from_connect(_connect_doc([])).nodes == ()


# ---------------------------------------------- application-key resolution

_APP_KEY_0 = "63964771734fbd76e3b40519d1d94a48"
_APP_KEY_1 = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _doc(nodes, app_keys=None):
    """A connect document with an explicit ``appKeys`` list."""
    doc = _connect_doc(nodes)
    if app_keys is not None:
        doc["appKeys"] = app_keys
    return doc


def _node(unicast, elements):
    return {
        "UUID": f"n{unicast}",
        "unicastAddress": f"{unicast:04X}",
        "deviceKey": "9d6dd0e96eb25dc19a40ed9914f8f03f",
        "cid": "07E9",
        "elements": elements,
    }


def test_app_keys_lists_every_key_in_the_export():
    """Only the first key used to survive parsing; a network can hold several."""
    network = Network.from_connect(
        _doc(
            [_GOOD_NODE],
            [
                {"key": _APP_KEY_0, "index": 0, "name": "First"},
                {"key": _APP_KEY_1, "index": 4, "name": "Second"},
            ],
        )
    )

    assert [(k.index, k.name) for k in network.app_keys] == [
        (0, "First"),
        (4, "Second"),
    ]
    assert network.app_keys[1].key == bytes.fromhex(_APP_KEY_1)


def test_app_key_and_index_still_name_the_first_key():
    """The historical fields keep their meaning: resolution is explicit."""
    network = Network.from_connect(
        _doc(
            [_GOOD_NODE],
            [{"key": _APP_KEY_0, "index": 0}, {"key": _APP_KEY_1, "index": 4}],
        )
    )

    assert network.app_key == bytes.fromhex(_APP_KEY_0)
    assert network.app_key_index == 0


def test_app_key_for_models_follows_what_the_models_bind():
    """The key we encrypt with must be the one the target models are bound to.

    Taking ``appKeys[0]`` blindly encrypts under an AppKey the lamp's lighting
    models never bound: the AID does not match, the node discards the message
    at the upper transport layer, and nothing is heard back.
    """
    node = _node(0x000C, [{"index": 0, "models": [{"modelId": "1300", "bind": [4]}]}])
    network = Network.from_connect(
        _doc(
            [node],
            [{"key": _APP_KEY_0, "index": 0}, {"key": _APP_KEY_1, "index": 4}],
        )
    )

    resolved = network.app_key_for_models([0x1300])

    assert resolved.index == 4
    assert resolved.key == bytes.fromhex(_APP_KEY_1)


def test_app_key_for_models_ignores_models_we_do_not_drive():
    """A remote's client models may bind another key; they must not sway it."""
    lamp = _node(0x000C, [{"index": 0, "models": [{"modelId": "1300", "bind": [0]}]}])
    remote = _node(
        0x0020,
        [
            {"index": 0, "models": [{"modelId": "1001", "bind": [4]}]},
            {"index": 1, "models": [{"modelId": "1001", "bind": [4]}]},
            {"index": 2, "models": [{"modelId": "1001", "bind": [4]}]},
        ],
    )
    network = Network.from_connect(
        _doc(
            [lamp, remote],
            [{"key": _APP_KEY_0, "index": 0}, {"key": _APP_KEY_1, "index": 4}],
        )
    )

    assert network.app_key_for_models([0x1300]).index == 0


def test_app_key_for_models_falls_back_to_the_first_key():
    """No binding at all (or an index the export has no key for) → the first."""
    node = _node(0x000C, [{"index": 0, "models": [{"modelId": "1300", "bind": []}]}])
    network = Network.from_connect(
        _doc(
            [node],
            [{"key": _APP_KEY_0, "index": 0}, {"key": _APP_KEY_1, "index": 4}],
        )
    )

    assert network.app_key_for_models([0x1300]).index == 0


def test_bound_app_key_indexes_reports_what_the_export_asks_for():
    """So a caller can say *why* it could not honour the binding."""
    node = _node(0x000C, [{"index": 0, "models": [{"modelId": "1300", "bind": [7]}]}])
    network = Network.from_connect(_doc([node]))

    assert network.bound_app_key_indexes([0x1300]) == (7,)
    # ...and with no key for index 7 the resolution still yields a usable key.
    assert network.app_key_for_models([0x1300]).index == 0


# ------------------------------------------------------ source addressing


def test_unicast_addresses_covers_every_element_not_just_the_node():
    node = _node(
        0x000C,
        [
            {"index": 0, "models": []},
            {"index": 1, "models": []},
            {"index": 2, "models": []},
        ],
    )
    network = Network.from_connect(_doc([node]))

    assert network.unicast_addresses() == frozenset({0x000C, 0x000D, 0x000E})


def test_free_unicast_keeps_the_preferred_address_when_it_is_free():
    network = Network.from_connect(_doc([_GOOD_NODE]))

    assert network.free_unicast(0x7FFF) == 0x7FFF


def test_free_unicast_steps_past_an_address_the_export_already_owns():
    """Transmitting from an address another node owns is silently fatal.

    The node's replay protection holds a sequence number for that address; ours
    starts far below it, so every message we send is discarded and no Status
    ever comes back — with nothing in any log to say so.
    """
    node = _node(0x7FFE, [{"index": 0, "models": []}, {"index": 1, "models": []}])
    network = Network.from_connect(_doc([node]))

    # 0x7FFF is element 1 of that node, 0x7FFE is element 0 → first free below.
    assert network.free_unicast(0x7FFF) == 0x7FFD


def test_free_unicast_rejects_an_address_outside_the_unicast_range():
    network = Network.from_connect(_doc([]))

    with pytest.raises(NetworkModelError):
        network.free_unicast(0xC000)  # a group address, not a unicast
