"""Tests for the HA-bluetooth mesh transport bridge (Task B1).

These exercise ``custom_components.bluetooth_mesh.mesh_transport`` against the
real Home Assistant ``bluetooth`` API surface (its module functions are patched
so no radios are touched). Run in the daikin_madoka venv, which has HA +
pytest-homeassistant-custom-component installed::

    PYTHONPATH="src" .../daikin_madoka/.venv/Scripts/python.exe \
        -m pytest tests/ha/test_mesh_transport.py -q
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The library venv (uv run pytest) also collects tests/ha but has no HA; these
# skip there and run for real in the daikin_madoka venv (see module docstring).
pytest.importorskip("homeassistant")
pytest.importorskip("bleak_retry_connector")

from custom_components.bluetooth_mesh.btmesh.bearer import GattBearer, PROXY_SERVICE
from custom_components.bluetooth_mesh.btmesh.crypto import k3
from custom_components.bluetooth_mesh import mesh_transport
from custom_components.bluetooth_mesh.mesh_transport import (
    MeshTransportError,
    async_connect_bearer,
    async_register_proxy_callback,
    find_proxy_address,
)

# Two distinct 16-byte NetKeys → two distinct 8-byte Network IDs.
NET_KEY = bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6")
FOREIGN_NET_KEY = bytes.fromhex("f7a2a44f8e8a8929127bb1a04d31e0e5")


def _network_id_advert(net_key: bytes) -> bytes:
    """0x1828 service data for a Network-ID advert: type 0x00 + 8-byte Net ID."""
    return bytes([0x00]) + k3(net_key)


def _fake_info(
    address: str, service_data: dict[str, bytes], connectable: bool = True
):
    """A BluetoothServiceInfoBleak-like object (duck-typed for our code)."""
    return SimpleNamespace(
        address=address,
        device=SimpleNamespace(address=address),
        service_data=service_data,
        connectable=connectable,
    )


def test_find_proxy_address_matches_network_id(hass) -> None:
    matching = _fake_info(
        "AA:BB:CC:DD:EE:FF", {PROXY_SERVICE: _network_id_advert(NET_KEY)}
    )
    foreign = _fake_info(
        "11:22:33:44:55:66", {PROXY_SERVICE: _network_id_advert(FOREIGN_NET_KEY)}
    )
    with patch.object(
        mesh_transport.bluetooth,
        "async_discovered_service_info",
        return_value=[foreign, matching],
    ) as disc:
        address = find_proxy_address(hass, NET_KEY)

    assert address == "AA:BB:CC:DD:EE:FF"
    # Scans the FULL snapshot (connectable=False) and selects on the per-advert
    # connectable flag — the same data path the diagnostic uses.
    disc.assert_called_once_with(hass, connectable=False)


def test_find_proxy_address_skips_non_connectable_match(hass) -> None:
    """A matching proxy seen only by a non-connectable scanner is not used.

    Its address would fail ``async_ble_device_from_address(connectable=True)`` at
    connect time, so we must not hand it back as reachable.
    """
    non_conn = _fake_info(
        "AA:BB:CC:DD:EE:FF",
        {PROXY_SERVICE: _network_id_advert(NET_KEY)},
        connectable=False,
    )
    with patch.object(
        mesh_transport.bluetooth,
        "async_discovered_service_info",
        return_value=[non_conn],
    ):
        assert find_proxy_address(hass, NET_KEY) is None


def test_find_proxy_address_prefers_connectable_match(hass) -> None:
    """Skip a non-connectable match to return a later connectable one.

    This is the exact bug the field diagnostic surfaced: the proxy is present and
    connectable in the full snapshot, so discovery must find it.
    """
    non_conn = _fake_info(
        "11:22:33:44:55:66",
        {PROXY_SERVICE: _network_id_advert(NET_KEY)},
        connectable=False,
    )
    conn = _fake_info(
        "AA:BB:CC:DD:EE:FF",
        {PROXY_SERVICE: _network_id_advert(NET_KEY)},
        connectable=True,
    )
    with patch.object(
        mesh_transport.bluetooth,
        "async_discovered_service_info",
        return_value=[non_conn, conn],
    ):
        assert find_proxy_address(hass, NET_KEY) == "AA:BB:CC:DD:EE:FF"


def test_find_proxy_address_none_for_only_foreign(hass) -> None:
    foreign = _fake_info(
        "11:22:33:44:55:66", {PROXY_SERVICE: _network_id_advert(FOREIGN_NET_KEY)}
    )
    no_proxy = _fake_info("99:99:99:99:99:99", {})
    with patch.object(
        mesh_transport.bluetooth,
        "async_discovered_service_info",
        return_value=[foreign, no_proxy],
    ):
        assert find_proxy_address(hass, NET_KEY) is None


async def test_async_connect_bearer_returns_bearer(hass) -> None:
    ble_device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    fake_client = MagicMock(name="BleakClient")
    with (
        patch.object(
            mesh_transport.bluetooth,
            "async_ble_device_from_address",
            return_value=ble_device,
        ) as from_addr,
        patch.object(
            mesh_transport,
            "establish_connection",
            new=AsyncMock(return_value=fake_client),
        ) as est,
    ):
        client, bearer = await async_connect_bearer(hass, "AA:BB:CC:DD:EE:FF")

    assert client is fake_client
    assert isinstance(bearer, GattBearer)
    # The bearer must wrap exactly the connected client (proxy service pair).
    assert bearer._client is fake_client
    from_addr.assert_called_once_with(hass, "AA:BB:CC:DD:EE:FF", connectable=True)
    est.assert_awaited_once()
    # establish_connection(cls, ble_device, name) — name is btmesh-<address>.
    args, _ = est.await_args
    assert args[1] is ble_device
    assert args[2] == "btmesh-AA:BB:CC:DD:EE:FF"


async def test_async_connect_bearer_raises_when_no_device(hass) -> None:
    with patch.object(
        mesh_transport.bluetooth,
        "async_ble_device_from_address",
        return_value=None,
    ):
        with pytest.raises(MeshTransportError):
            await async_connect_bearer(hass, "AA:BB:CC:DD:EE:FF")


def test_async_register_proxy_callback_forwards_only_matches(hass) -> None:
    found: list[str] = []
    sentinel_unregister = object()

    with patch.object(
        mesh_transport.bluetooth,
        "async_register_callback",
        return_value=sentinel_unregister,
    ) as reg:
        unregister = async_register_proxy_callback(
            hass, NET_KEY, found.append
        )

    assert unregister is sentinel_unregister
    # The registered matcher targets the 0x1828 proxy service.
    _, matcher, _mode = reg.call_args.args[1:4]
    assert dict(matcher).get("service_uuid") == PROXY_SERVICE

    # Drive the captured callback with a matching then a foreign advert.
    callback = reg.call_args.args[1]
    match_info = _fake_info(
        "AA:BB:CC:DD:EE:FF", {PROXY_SERVICE: _network_id_advert(NET_KEY)}
    )
    foreign_info = _fake_info(
        "11:22:33:44:55:66", {PROXY_SERVICE: _network_id_advert(FOREIGN_NET_KEY)}
    )
    callback(match_info, None)
    callback(foreign_info, None)

    assert found == ["AA:BB:CC:DD:EE:FF"]
