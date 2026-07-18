"""Mesh security functions (Mesh Profile spec §3.8.2)."""

from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.cmac import CMAC

# All-zero 128-bit CMAC key used by s1 (spec §3.8.2.4).
ZERO_KEY = bytes(16)


def aes_cmac(key: bytes, data: bytes) -> bytes:
    """AES-CMAC authentication function (spec §3.8.2.2, RFC 4493)."""
    c = CMAC(AES(key))
    c.update(data)
    return c.finalize()


def aes_ecb(key: bytes, block: bytes) -> bytes:
    """AES-128 encryption of a single 16-byte block (spec 'e' function §3.8.2.1).

    Used by the network layer to compute the PECB for header obfuscation.
    """
    encryptor = Cipher(AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def s1(m: bytes) -> bytes:
    """SALT generation function (spec §3.8.2.4)."""
    return aes_cmac(ZERO_KEY, m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    """Key derivation function k1 (spec §3.8.2.5)."""
    return aes_cmac(aes_cmac(salt, n), p)


def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    """Network key material derivation k2 (spec §3.8.2.6).

    Returns (NID, EncryptionKey, PrivacyKey).
    """
    salt = s1(b"smk2")
    t = aes_cmac(salt, n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    nid = t1[15] & 0x7F  # (T1) mod 2^7
    return nid, t2, t3


def k3(n: bytes) -> bytes:
    """64-bit Network ID derivation k3 (spec §3.8.2.7)."""
    salt = s1(b"smk3")
    t = aes_cmac(salt, n)
    return aes_cmac(t, b"id64\x01")[8:]  # mod 2^64 (least significant 8 bytes)


def k4(n: bytes) -> int:
    """6-bit application key identifier derivation k4 (spec §3.8.2.8)."""
    salt = s1(b"smk4")
    t = aes_cmac(salt, n)
    return aes_cmac(t, b"id6\x01")[15] & 0x3F  # mod 2^6


def ccm_encrypt(
    key: bytes, nonce: bytes, plaintext: bytes, mic_len: int, aad: bytes = b""
) -> bytes:
    """AES-CCM encrypt; returns ciphertext || MIC (spec §3.8.2.3).

    Mesh nonces (network/application/device/proxy) are always 13 bytes (L=2).
    """
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, aad)


def ccm_decrypt(
    key: bytes, nonce: bytes, data: bytes, mic_len: int, aad: bytes = b""
) -> bytes:
    """AES-CCM decrypt of ciphertext || MIC; raises InvalidTag on MIC mismatch.

    Mesh nonces (network/application/device/proxy) are always 13 bytes (L=2).
    """
    return AESCCM(key, tag_length=mic_len).decrypt(nonce, data, aad)
