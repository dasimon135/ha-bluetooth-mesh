"""Mesh security functions (Mesh Profile spec §3.8.2)."""

from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.cmac import CMAC

ZERO = bytes(16)


def aes_cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(AES(key))
    c.update(data)
    return c.finalize()


def s1(m: bytes) -> bytes:
    """SALT generation function (spec §3.8.2.4)."""
    return aes_cmac(ZERO, m)


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
    nid = t1[15] & 0x7F
    return nid, t2, t3


def k3(n: bytes) -> bytes:
    """64-bit Network ID derivation k3 (spec §3.8.2.7)."""
    salt = s1(b"smk3")
    t = aes_cmac(salt, n)
    return aes_cmac(t, b"id64\x01")[8:]


def k4(n: bytes) -> int:
    """6-bit application key identifier derivation k4 (spec §3.8.2.8)."""
    salt = s1(b"smk4")
    t = aes_cmac(salt, n)
    return aes_cmac(t, b"id6\x01")[15] & 0x3F
