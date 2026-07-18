"""Tests for btmesh_min.crypto against Mesh Profile 1.0.1 spec sample data (Section 8).

Vector sources (cross-checked, two independent implementations each):
- BlueZ unit/test-mesh-crypto.c
  https://github.com/bluez/bluez/blob/master/unit/test-mesh-crypto.c
- Silvair python-bluetooth-mesh-network tests/test_security.py
  https://github.com/SilvairGit/python-bluetooth-mesh-network/blob/master/tests/test_security.py

CCM vector sources:
- Mesh §8.3.1 Message #1: BlueZ unit/test-mesh-crypto.c (s8_3_1) and
  btstack test/mesh/mesh_message_test.py
  https://github.com/bluekitchen/btstack/blob/master/test/mesh/mesh_message_test.py
- RFC 3610 Packet Vector #1: https://www.rfc-editor.org/rfc/rfc3610.txt

Additional sources:
- k2 friendship vector (§8.1.4): BlueZ unit/test-mesh-crypto.c (s8_1_4),
  cross-checked against AndrewGi/BluetoothMeshRust src/crypto/k_funcs.rs
  https://github.com/AndrewGi/BluetoothMeshRust/blob/master/src/crypto/k_funcs.rs
- AES-CMAC Example 1: https://www.rfc-editor.org/rfc/rfc4493.txt
"""

import pytest
from cryptography.exceptions import InvalidTag

from btmesh_min.crypto import aes_cmac, ccm_decrypt, ccm_encrypt, k1, k2, k3, k4, s1

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


def test_k2_friendship_spec_8_1_4():
    """Spec §8.1.4: k2 with 9-byte friendship P.

    P = 0x01 || LPNAddress(0203) || FriendAddress(0405) ||
        LPNCounter(0607) || FriendCounter(0809).
    Exercises multi-byte P in the T1/T2/T3 concatenations.
    """
    p = bytes.fromhex("010203040506070809")
    nid, enc_key, priv_key = k2(NET_KEY, p)
    assert nid == 0x73
    assert enc_key == bytes.fromhex("11efec0642774992510fb5929646df49")
    assert priv_key == bytes.fromhex("d4d7cc0dfa772d836a8df9df5510d7a7")


def test_aes_cmac_rfc4493_example_1():
    """RFC 4493 §4 Example 1: empty message, pins aes_cmac independently."""
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    assert aes_cmac(key, b"") == bytes.fromhex("bb1d6929e95937287fa37d129b756746")


def test_k3_spec_8_1_5():
    """Spec §8.1.5: k3(NetKey) -> 64-bit Network ID."""
    assert k3(NET_KEY) == bytes.fromhex("ff046958233db014")


def test_k4_spec_8_1_6():
    """Spec §8.1.6: k4(AppKey) -> 6-bit AID."""
    assert k4(APP_KEY) == 0x38


# --- AES-CCM ---

# Spec §8.3.1 Message #1: network-layer encryption intermediates.
# EncryptionKey (from k2 of NetKey 7dd7364c...), network nonce,
# plaintext = DST || TransportPDU, CTL=1 so NetMIC is 64-bit.
MSG1_ENC_KEY = bytes.fromhex("0953fa93e7caac9638f58820220a398e")
MSG1_NET_NONCE = bytes.fromhex("00800000011201000012345678")
MSG1_PLAINTEXT = bytes.fromhex("fffd034b50057e400000010000")
MSG1_CIPHERTEXT_MIC = bytes.fromhex(
    "b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df"
)


def test_ccm_encrypt_spec_8_3_1():
    """Spec §8.3.1 Message #1 network encryption (mic_len=8, no AAD)."""
    out = ccm_encrypt(MSG1_ENC_KEY, MSG1_NET_NONCE, MSG1_PLAINTEXT, mic_len=8)
    assert out == MSG1_CIPHERTEXT_MIC


def test_ccm_decrypt_spec_8_3_1():
    """Spec §8.3.1 Message #1 network decryption."""
    out = ccm_decrypt(MSG1_ENC_KEY, MSG1_NET_NONCE, MSG1_CIPHERTEXT_MIC, mic_len=8)
    assert out == MSG1_PLAINTEXT


def test_ccm_rfc3610_packet_vector_1():
    """RFC 3610 Packet Vector #1 (with AAD, M=8)."""
    key = bytes.fromhex("c0c1c2c3c4c5c6c7c8c9cacbcccdcecf")
    nonce = bytes.fromhex("00000003020100a0a1a2a3a4a5")
    aad = bytes.fromhex("0001020304050607")
    plaintext = bytes.fromhex("08090a0b0c0d0e0f101112131415161718191a1b1c1d1e")
    expected = bytes.fromhex(
        "588c979a61c663d2f066d0c2c0f989806d5f6b61dac38417e8d12cfdf926e0"
    )
    assert ccm_encrypt(key, nonce, plaintext, mic_len=8, aad=aad) == expected
    assert ccm_decrypt(key, nonce, expected, mic_len=8, aad=aad) == plaintext


def test_ccm_decrypt_tampered_raises():
    """Flipping any bit of ciphertext or MIC must raise InvalidTag."""
    tampered = bytearray(MSG1_CIPHERTEXT_MIC)
    tampered[0] ^= 0x01
    with pytest.raises(InvalidTag):
        ccm_decrypt(MSG1_ENC_KEY, MSG1_NET_NONCE, bytes(tampered), mic_len=8)
    tampered = bytearray(MSG1_CIPHERTEXT_MIC)
    tampered[-1] ^= 0x80  # MIC byte
    with pytest.raises(InvalidTag):
        ccm_decrypt(MSG1_ENC_KEY, MSG1_NET_NONCE, bytes(tampered), mic_len=8)


@pytest.mark.parametrize("mic_len", [4, 8])
def test_ccm_round_trip(mic_len):
    key = bytes(range(16))
    nonce = bytes(range(13))
    plaintext = b"btmesh round trip"
    aad = b"aad"
    data = ccm_encrypt(key, nonce, plaintext, mic_len=mic_len, aad=aad)
    assert len(data) == len(plaintext) + mic_len
    assert ccm_decrypt(key, nonce, data, mic_len=mic_len, aad=aad) == plaintext
