"""Secure Network Beacon tests (Mesh Profile spec §3.9.3).

The beacon is what a node sends over a freshly opened proxy connection to
announce the subnet's current IV Index. Authenticating it is the whole point:
an unauthenticated beacon would let anyone in radio range push us onto a bogus
IV Index and silence every message we send.
"""

import pytest

from btmesh.beacon import (
    BEACON_TYPE_SECURE_NETWORK,
    BeaconError,
    SecureNetworkBeacon,
    beacon_key,
    build_secure_network_beacon,
    parse_secure_network_beacon,
)
from btmesh.crypto import k3

NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
OTHER_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")


def test_round_trip_carries_the_flags_and_iv_index():
    payload = build_secure_network_beacon(
        NET_KEY, iv_index=0x12345678, key_refresh=True, iv_update=False
    )
    beacon = parse_secure_network_beacon(payload, NET_KEY)
    assert beacon == SecureNetworkBeacon(
        key_refresh=True,
        iv_update=False,
        network_id=k3(NET_KEY),
        iv_index=0x12345678,
    )


def test_beacon_is_22_bytes_with_the_type_octet():
    """1 type + 1 flags + 8 Network ID + 4 IV Index + 8 authentication."""
    payload = build_secure_network_beacon(NET_KEY, iv_index=0)
    assert len(payload) == 22
    assert payload[0] == BEACON_TYPE_SECURE_NETWORK


def test_iv_update_flag_round_trips():
    payload = build_secure_network_beacon(
        NET_KEY, iv_index=7, key_refresh=False, iv_update=True
    )
    beacon = parse_secure_network_beacon(payload, NET_KEY)
    assert beacon.iv_update is True
    assert beacon.key_refresh is False


def test_a_tampered_iv_index_is_rejected():
    """The attack this authentication exists to stop.

    Flipping the IV Index without the key invalidates the authentication
    value, so the beacon must be refused rather than adopted — adopting it
    would silence every message we send afterwards.
    """
    payload = bytearray(build_secure_network_beacon(NET_KEY, iv_index=0x12345678))
    payload[10] ^= 0xFF  # first octet of the IV Index
    with pytest.raises(BeaconError):
        parse_secure_network_beacon(bytes(payload), NET_KEY)


def test_a_beacon_of_another_network_is_rejected():
    payload = build_secure_network_beacon(OTHER_KEY, iv_index=3)
    with pytest.raises(BeaconError):
        parse_secure_network_beacon(payload, NET_KEY)


def test_an_unknown_beacon_type_is_rejected():
    payload = bytearray(build_secure_network_beacon(NET_KEY, iv_index=0))
    payload[0] = 0x00  # Unprovisioned Device beacon
    with pytest.raises(BeaconError):
        parse_secure_network_beacon(bytes(payload), NET_KEY)


def test_a_truncated_beacon_is_rejected():
    payload = build_secure_network_beacon(NET_KEY, iv_index=0)[:-1]
    with pytest.raises(BeaconError):
        parse_secure_network_beacon(payload, NET_KEY)


def test_beacon_key_is_derived_from_the_net_key():
    """k1(NetKey, s1("nkbk"), "id128" || 0x01) — distinct per network."""
    assert len(beacon_key(NET_KEY)) == 16
    assert beacon_key(NET_KEY) != beacon_key(OTHER_KEY)
