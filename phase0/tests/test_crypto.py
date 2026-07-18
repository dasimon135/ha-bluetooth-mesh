"""Tests for btmesh_min.crypto against Mesh Profile 1.0.1 spec sample data (Section 8).

Vector sources (cross-checked, two independent implementations each):
- BlueZ unit/test-mesh-crypto.c
  https://github.com/bluez/bluez/blob/master/unit/test-mesh-crypto.c
- Silvair python-bluetooth-mesh-network tests/test_security.py
  https://github.com/SilvairGit/python-bluetooth-mesh-network/blob/master/tests/test_security.py
"""

from btmesh_min.crypto import k1, k2, k3, k4, s1

# Spec sample data §8.1.2 / §8.1.3 use these keys (also §8.1.5, §8.1.6).
APP_KEY = bytes.fromhex("3216d1509884b533248541792b877f98")
NET_KEY = bytes.fromhex("f7a2a44f8e8a8029064f173ddc1e2b00")


def test_s1_spec_8_1_1():
    """Spec §8.1.1: s1("test")."""
    assert s1(b"test") == bytes.fromhex("b73cefbd641ef2ea598c2b6efb62f79c")


def test_k1_spec_8_1_2():
    """Spec §8.1.2: k1 with N = AppKey sample, SALT and P as given."""
    salt = bytes.fromhex("2ba14ffa0df84a2831938d57d276cab4")
    p = bytes.fromhex("5a09d60797eeb4478aada59db3352a0d")
    assert k1(APP_KEY, salt, p) == bytes.fromhex("f6ed15a8934afbe7d83e8dcb57fcf5d7")


def test_k1_salt_and_p_derivation():
    """§8.1.2 SALT is s1("salt") and P is s1("info") — internal consistency
    check confirming BlueZ's derivation matches Silvair's literal values."""
    assert s1(b"salt") == bytes.fromhex("2ba14ffa0df84a2831938d57d276cab4")
    assert s1(b"info") == bytes.fromhex("5a09d60797eeb4478aada59db3352a0d")


def test_k2_master_spec_8_1_3():
    """Spec §8.1.3: k2 with P = 0x00 (master/flooding security material)."""
    nid, enc_key, priv_key = k2(NET_KEY, b"\x00")
    assert nid == 0x7F
    assert enc_key == bytes.fromhex("9f589181a0f50de73c8070c7a6d27f46")
    assert priv_key == bytes.fromhex("4c715bd4a64b938f99b453351653124f")


def test_k3_spec_8_1_5():
    """Spec §8.1.5: k3(NetKey) -> 64-bit Network ID."""
    assert k3(NET_KEY) == bytes.fromhex("ff046958233db014")


def test_k4_spec_8_1_6():
    """Spec §8.1.6: k4(AppKey) -> 6-bit AID."""
    assert k4(APP_KEY) == 0x38
