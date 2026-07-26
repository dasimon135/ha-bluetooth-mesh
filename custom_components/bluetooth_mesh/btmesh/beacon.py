"""Secure Network Beacon (Mesh Profile spec §3.9.3).

A node sends this beacon over the proxy connection as soon as the link comes
up, announcing the subnet's **current IV Index** and whether an IV Update or a
Key Refresh is in progress. It is the only way a client that was not present
during an IV Update can discover that its own IV Index has gone stale — and a
stale IV Index is fatal in silence: every PDU we send is discarded by the mesh,
and every PDU we receive fails the IVI check.

The authentication value is not optional. Without verifying it, anyone in radio
range could push us onto a bogus IV Index and mute the integration; with it, a
beacon can only be produced by someone already holding the NetKey.
"""

from typing import NamedTuple

from .crypto import aes_cmac, k1, k3, s1
from .errors import BtMeshError

__all__ = [
    "BEACON_TYPE_SECURE_NETWORK",
    "BeaconError",
    "SecureNetworkBeacon",
    "beacon_key",
    "parse_secure_network_beacon",
    "build_secure_network_beacon",
]

# Mesh beacon types (spec §3.9.2). 0x00 is the Unprovisioned Device beacon.
BEACON_TYPE_SECURE_NETWORK = 0x01

_FLAG_KEY_REFRESH = 0x01
_FLAG_IV_UPDATE = 0x02
_AUTH_LEN = 8
_BEACON_LEN = 22  # type(1) + flags(1) + network id(8) + iv index(4) + auth(8)


class BeaconError(BtMeshError):
    """A beacon was malformed, foreign, or failed authentication."""


class SecureNetworkBeacon(NamedTuple):
    """An authenticated Secure Network Beacon."""

    key_refresh: bool
    iv_update: bool
    network_id: bytes
    iv_index: int


def beacon_key(net_key: bytes) -> bytes:
    """Derive the Beacon Key: ``k1(NetKey, s1("nkbk"), "id128" || 0x01)``."""
    return k1(net_key, s1(b"nkbk"), b"id128\x01")


def _auth(net_key: bytes, body: bytes) -> bytes:
    """The 64-bit authentication value over flags || Network ID || IV Index."""
    return aes_cmac(beacon_key(net_key), body)[:_AUTH_LEN]


def parse_secure_network_beacon(
    payload: bytes, net_key: bytes
) -> SecureNetworkBeacon:
    """Parse and AUTHENTICATE a Secure Network Beacon for ``net_key``.

    Raises :class:`BeaconError` unless the beacon is well formed, carries our
    subnet's Network ID, and its authentication value verifies — an
    unauthenticated IV Index must never be adopted.
    """
    if len(payload) != _BEACON_LEN:
        raise BeaconError(
            f"secure network beacon must be {_BEACON_LEN} bytes, got {len(payload)}"
        )
    if payload[0] != BEACON_TYPE_SECURE_NETWORK:
        raise BeaconError(f"not a secure network beacon: type {payload[0]:#04x}")

    body = payload[1:14]  # flags(1) + network id(8) + iv index(4)
    network_id = payload[2:10]
    if network_id != k3(net_key):
        raise BeaconError(
            f"beacon belongs to another network: {network_id.hex()}"
        )
    if _auth(net_key, body) != payload[14:]:
        raise BeaconError("beacon authentication failed")

    flags = payload[1]
    return SecureNetworkBeacon(
        key_refresh=bool(flags & _FLAG_KEY_REFRESH),
        iv_update=bool(flags & _FLAG_IV_UPDATE),
        network_id=network_id,
        iv_index=int.from_bytes(payload[10:14], "big"),
    )


def build_secure_network_beacon(
    net_key: bytes,
    *,
    iv_index: int,
    key_refresh: bool = False,
    iv_update: bool = False,
) -> bytes:
    """Build a beacon for ``net_key`` (the peer side, used by the tests)."""
    if not 0 <= iv_index <= 0xFFFFFFFF:
        raise BeaconError(f"IV Index out of range: {iv_index:#x}")
    flags = (_FLAG_KEY_REFRESH if key_refresh else 0) | (
        _FLAG_IV_UPDATE if iv_update else 0
    )
    body = bytes([flags]) + k3(net_key) + iv_index.to_bytes(4, "big")
    return bytes([BEACON_TYPE_SECURE_NETWORK]) + body + _auth(net_key, body)
