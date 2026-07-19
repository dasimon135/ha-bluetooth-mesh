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
