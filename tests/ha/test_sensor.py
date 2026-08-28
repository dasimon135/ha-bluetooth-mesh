"""Tests for the mesh proxy sensor.

The integration holds one GATT link, to whichever node advertises the network's
Network ID. That link occupies a Bluetooth *connection slot* on an ESPHome proxy
for as long as it is held, but nothing in Home Assistant said which BLE address
it belongs to: every mesh device is keyed on the network UUID plus a unicast
address, and carries no `connections` at all.

So a slot-accounting tool could see the address held and find no device for it.
BlueSight reported this network's proxy link as a stuck slot for hours while it
was perfectly healthy -- it had no way to tell "silent because nothing changed on
the mesh" from "silent because it is wedged", the device being invisible to it.

This entity closes that: it names the address on the device, and it goes
unavailable exactly when the link does, so a reader can judge the slot from a
real signal instead of guessing from idle time.

A fake coordinator and a fake device registry stand in for the real ones; no BLE
and no `hass` are touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The library venv (uv run pytest) also collects tests/ha but has no HA; this
# skips there and runs for real in the HA venv.
pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.bluetooth_mesh import sensor as sensor_module
from custom_components.bluetooth_mesh.btmesh.network_model import Network
from custom_components.bluetooth_mesh.const import DOMAIN
from custom_components.bluetooth_mesh.sensor import MeshProxySensor, async_setup_entry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.connect.json"

ADDRESS = "C3:EB:49:65:67:55"
OTHER_ADDRESS = "D4:FA:5A:76:78:66"


def _network() -> Network:
    return Network.from_connect(__import__("json").loads(FIXTURE.read_text()))


class FakeCoordinator:
    def __init__(self, network, *, available=True, proxy_address=None):
        self.network = network
        self.available = available
        self.proxy_address = proxy_address
        self.listeners: list = []

    def async_add_listener(self, callback_):
        self.listeners.append(callback_)
        return lambda: self.listeners.remove(callback_)

    def fire(self) -> None:
        for callback_ in list(self.listeners):
            callback_()


class FakeDevice:
    def __init__(self, device_id="dev1", connections=frozenset()):
        self.id = device_id
        self.connections = connections


class FakeRegistry:
    """Records `async_update_device` the way the real registry is driven."""

    def __init__(self, device=None):
        self._device = device if device is not None else FakeDevice()
        self.updates: list[tuple] = []

    def async_get_device(self, identifiers=None, connections=None):
        return self._device

    def async_update_device(self, device_id, **kwargs):
        self.updates.append((device_id, kwargs))
        if "new_connections" in kwargs:
            self._device.connections = kwargs["new_connections"]
        return self._device


@pytest.fixture
def registry(monkeypatch):
    reg = FakeRegistry()
    monkeypatch.setattr(sensor_module.dr, "async_get", lambda hass: reg)
    return reg


def _sensor(coordinator, hass=object()):
    """An entity wired to fakes, with the HA state write stubbed out.

    Writing state needs a real ``hass``; every assertion below is about the
    registry and the entity's own properties, so the write is recorded rather
    than performed.
    """
    entity = MeshProxySensor(coordinator)
    entity.hass = hass
    entity.writes = []
    entity.async_write_ha_state = lambda: entity.writes.append(1)
    return entity


# ------------------------------------------------------------------ the state


def test_the_sensor_reports_the_address_it_is_connected_through():
    """The point of the entity: name the BLE address holding the slot."""
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)
    assert _sensor(coordinator).native_value == ADDRESS


def test_no_address_yet_reads_as_unknown_not_as_a_guess():
    """Before the first connect there is no address, and inventing one would
    put a device in the registry claiming a slot nobody holds."""
    coordinator = FakeCoordinator(_network(), proxy_address=None)
    assert _sensor(coordinator).native_value is None


def test_the_sensor_is_unavailable_exactly_when_the_mesh_link_is():
    """This is the whole signal a slot reader judges the link by.

    A sensor that stayed available while the link was down would tell that
    reader the slot is healthy at exactly the moment it is not -- the blind spot
    this entity exists to remove, re-created with extra steps.
    """
    coordinator = FakeCoordinator(_network(), available=True, proxy_address=ADDRESS)
    entity = _sensor(coordinator)
    assert entity.available is True

    coordinator.available = False
    assert entity.available is False


def test_the_sensor_is_diagnostic_and_not_a_control():
    entity = _sensor(FakeCoordinator(_network(), proxy_address=ADDRESS))
    assert entity.entity_category is sensor_module.EntityCategory.DIAGNOSTIC
    assert entity.should_poll is False


def test_the_device_is_keyed_on_the_network_not_on_a_node():
    """One GATT link per network, not per lamp.

    The address belongs to the connection, so it cannot be hung off a light: a
    mesh reaches many nodes through one proxy, and claiming the same BLE
    connection on several devices makes the registry treat them as one.
    """
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)
    info = _sensor(coordinator).device_info
    assert info["identifiers"] == {
        (DOMAIN, f"{coordinator.network.identifier}_proxy")
    }


# ------------------------------------------------- the registry `connections`


async def test_the_device_gains_the_address_as_a_bluetooth_connection(registry):
    """What makes the address resolvable at all.

    Without this the address appears in no device's `connections`, so anything
    correlating a held slot to a Home Assistant device draws a blank -- which is
    exactly how a healthy mesh link came to be reported as a stuck one.
    """
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)
    entity = _sensor(coordinator)

    await entity.async_added_to_hass()

    [(device_id, kwargs)] = registry.updates
    assert device_id == "dev1"
    assert kwargs["new_connections"] == {
        (sensor_module.dr.CONNECTION_BLUETOOTH, "c3:eb:49:65:67:55")
    }


async def test_a_rotated_address_replaces_the_old_one_rather_than_joining_it(
    registry,
):
    """Mesh proxies advertise a *random static* address.

    Those are stable while the node is powered and free to change when it is
    not, so the address learned today can be a stranger tomorrow. Adding each
    one would leave the device claiming a BLE connection it does not have --
    and a slot reader resolving that stale address would name this device for
    somebody else's slot. `new_connections` replaces the set.
    """
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)
    entity = _sensor(coordinator)
    await entity.async_added_to_hass()

    coordinator.proxy_address = OTHER_ADDRESS
    coordinator.fire()

    assert registry.updates[-1][1]["new_connections"] == {
        (sensor_module.dr.CONNECTION_BLUETOOTH, "d4:fa:5a:76:78:66")
    }
    assert len(registry.updates[-1][1]["new_connections"]) == 1


async def test_no_connection_is_claimed_before_the_first_connect(registry):
    """A device claiming a MAC it has never reached is worse than one claiming
    none: the address may belong to somebody else's device entirely."""
    coordinator = FakeCoordinator(_network(), proxy_address=None)

    await _sensor(coordinator).async_added_to_hass()

    assert registry.updates == []


async def test_an_unchanged_address_is_not_rewritten_every_snapshot(registry):
    """The link notifies on every reconnect. Rewriting an identical connection
    each time churns the device registry -- which is stored on disk -- for a
    fact that did not change."""
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)
    entity = _sensor(coordinator)
    await entity.async_added_to_hass()

    coordinator.fire()
    coordinator.fire()

    assert len(registry.updates) == 1


async def test_a_missing_device_is_survived(registry):
    """The entity can be notified before the registry has the device (the
    platform creates it from `device_info` as the entity is added). Raising
    inside an availability callback would take the coordinator's notify loop
    down with it."""
    registry._device = None
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)

    await _sensor(coordinator).async_added_to_hass()

    assert registry.updates == []


# ------------------------------------------------------------------ the setup


async def test_setup_creates_exactly_one_proxy_sensor():
    """One GATT link per config entry, so one entity -- however many nodes the
    network has."""
    coordinator = FakeCoordinator(_network(), proxy_address=ADDRESS)
    entry = type("Entry", (), {"runtime_data": coordinator})()
    added: list = []

    await async_setup_entry(object(), entry, lambda entities: added.extend(entities))

    assert len(added) == 1
    assert isinstance(added[0], MeshProxySensor)


def test_the_entity_name_is_translated_not_a_raw_key():
    """`_attr_translation_key` with no catalogue entry renders the raw key as
    the entity name, in every language at once."""
    import json

    strings = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "custom_components"
            / "bluetooth_mesh"
            / "strings.json"
        ).read_text(encoding="utf-8")
    )
    entity = MeshProxySensor(FakeCoordinator(_network()))

    assert entity.translation_key in strings["entity"]["sensor"]
