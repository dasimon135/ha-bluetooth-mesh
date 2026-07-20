"""Light platform for the Bluetooth Mesh integration (Task B4).

Every provisioned node whose primary element (element 0) hosts a Light
Lightness (0x1300) or a Generic OnOff (0x1000) server becomes a single
:class:`MeshLight` entity. The entity's capabilities scale with the node's
composition:

* Light CTL server (0x1303) present → tunable white: HA ``COLOR_TEMP`` mode
  (which in HA implies brightness too).
* else Light Lightness server (0x1300) → dimmable: HA ``BRIGHTNESS`` mode.
* else Generic OnOff only → HA ``ONOFF`` mode.

All BLE lives behind the coordinator (``entry.runtime_data``); the entity only
enumerates the static network model and calls the coordinator's best-effort
command coroutines. State is optimistic: a mesh set returns a status value, so
when that value is non-``None`` we cache it, otherwise we reflect the requested
intent. The coordinator's availability governs whether HA shows the entity as
live or stale.
"""

from __future__ import annotations

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluetoothMeshConfigEntry
from .const import DOMAIN
from .coordinator import MeshCoordinator

# SIG model identifiers we key capabilities off of (spec Mesh Model §6/§7).
MODEL_GENERIC_ONOFF = 0x1000
MODEL_LIGHT_LIGHTNESS = 0x1300
MODEL_LIGHT_CTL = 0x1303
# The Light CTL Temperature server lives on its OWN (secondary) element and sets
# temperature WITHOUT touching lightness — unlike Light CTL Set on element 0.
MODEL_LIGHT_CTL_TEMP = 0x1306

# HA brightness is a 0..255 byte; mesh Lightness/CTL are 16-bit 0..65535.
MESH_LEVEL_MAX = 0xFFFF
HA_BRIGHTNESS_MAX = 255

# Tunable-white lamps advertise the full mesh CTL temperature range (800..20000
# K) but only track a narrow band. Expose the conventional tunable-white span as
# a safe default rather than the raw model limits.
DEFAULT_MIN_KELVIN = 2700
DEFAULT_MAX_KELVIN = 6500

# Known company identifiers → friendly manufacturer names. Unknown CIDs fall
# back to the raw hex (see ``_manufacturer``).
_KNOWN_CIDS = {
    0x07E9: "Häfele",
}


def _manufacturer(cid: int) -> str:
    """Friendly manufacturer for a company identifier, else its hex form."""
    return _KNOWN_CIDS.get(cid, f"CID {cid:#06x}")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothMeshConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one light per node whose element 0 is a lightness/onoff server."""
    coordinator = entry.runtime_data
    entities: list[MeshLight] = []
    for node in coordinator.network.nodes:
        if not node.elements:
            continue
        element0 = node.elements[0]
        if element0.has_model(MODEL_LIGHT_LIGHTNESS) or element0.has_model(
            MODEL_GENERIC_ONOFF
        ):
            entities.append(MeshLight(coordinator, node))
    async_add_entities(entities)


class MeshLight(LightEntity):
    """A mesh node exposed as a Home Assistant light.

    Optimistic: the cached ``is_on`` / ``brightness`` / ``color_temp_kelvin``
    reflect the last commanded (and, when the mesh answered, confirmed) state.
    """

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: MeshCoordinator, node) -> None:
        self._coordinator = coordinator
        self._node = node
        self._unicast = node.unicast

        self._attr_unique_id = (
            f"{coordinator.network.uuid}_{node.unicast:04x}"
        )

        # Capability → HA color mode. COLOR_TEMP implies brightness support in
        # HA, so a CTL node needs only that single mode in the set.
        if node.has_model(MODEL_LIGHT_CTL):
            mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = DEFAULT_MIN_KELVIN
            self._attr_max_color_temp_kelvin = DEFAULT_MAX_KELVIN
        elif node.has_model(MODEL_LIGHT_LIGHTNESS):
            mode = ColorMode.BRIGHTNESS
        else:
            mode = ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

        model = {
            ColorMode.COLOR_TEMP: "Light CTL",
            ColorMode.BRIGHTNESS: "Light Lightness",
            ColorMode.ONOFF: "Generic OnOff",
        }[mode]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=node.name or f"Mesh {node.unicast:04x}",
            manufacturer=_manufacturer(node.cid),
            model=model,
        )

        # The Light CTL Temperature server, if any, is on its own element with
        # its own unicast — address temperature-only changes there so brightness
        # is left untouched. None → fall back to Light CTL Set on element 0.
        temp_element = node.element_for_model(MODEL_LIGHT_CTL_TEMP)
        self._ctl_temp_unicast = (
            temp_element.unicast if temp_element is not None else None
        )

        # Optimistic cache.
        self._is_on: bool = False
        self._brightness: int | None = None
        self._color_temp_kelvin: int | None = None

    # ------------------------------------------------------------- properties

    @property
    def available(self) -> bool:
        """Track the coordinator's proxy connection."""
        return self._coordinator.available

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        return self._color_temp_kelvin

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _brightness_to_level(brightness: int) -> float:
        """HA brightness (0..255) → mesh level fraction (0..1)."""
        return brightness / HA_BRIGHTNESS_MAX

    @staticmethod
    def _level_to_brightness(level: int) -> int:
        """Mesh lightness (0..65535) → HA brightness (0..255)."""
        return round(level / MESH_LEVEL_MAX * HA_BRIGHTNESS_MAX)

    def _mesh_kelvin(self, kelvin: int) -> int:
        """HA color temperature → the value this lamp's CTL server expects.

        The lamp maps the Light CTL temperature inversely to its warm/cool LEDs
        (dragging toward warm produced cool and vice-versa), so mirror the
        requested Kelvin around the midpoint of the exposed range before sending:
        ``min + max - K``. The mirror stays inside the exposed 2700..6500 K band,
        so the controller's spec-range clamp never triggers, and HA keeps
        displaying the un-mirrored value the user selected.
        """
        return (
            self._attr_min_color_temp_kelvin
            + self._attr_max_color_temp_kelvin
            - kelvin
        )

    # ---------------------------------------------------------------- commands

    async def async_turn_on(self, **kwargs) -> None:
        """Apply requested brightness and/or temperature and ensure the lamp is on.

        Each attribute is applied independently so a temperature change never
        disturbs brightness and vice-versa: temperature goes to the Light CTL
        Temperature server on its own element (carrying no lightness) when the
        node exposes one, else falls back to Light CTL Set (which must carry a
        lightness). Because that Temperature message does NOT switch the light on,
        a turn-on that only changes temperature — or a bare turn-on — also sends
        Generic OnOff, so the lamp actually lights instead of HA showing it on
        while it stays dark. Brightness is tracked from the last commanded value
        (the lamp restores it across off/on), never read back mid-fade.
        """
        was_on = self._is_on

        # Optimistic state up front so the UI reflects the tap instantly.
        self._is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            self._color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
        self.async_write_ha_state()

        # Track whether any command drives lightness > 0 (which itself lights the
        # lamp) versus a temperature-only change (which does not).
        drove_lightness = False
        temp_only = False

        if ATTR_BRIGHTNESS in kwargs:
            result = await self._coordinator.async_set_lightness(
                self._unicast, self._brightness_to_level(kwargs[ATTR_BRIGHTNESS])
            )
            if result is not None:  # confirmed present lightness → trust it
                self._brightness = self._level_to_brightness(result)
            drove_lightness = True

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            mesh_kelvin = self._mesh_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN])
            if self._ctl_temp_unicast is not None:
                # Dedicated CTL Temperature element: sets ONLY temperature and
                # does NOT switch the light on.
                await self._coordinator.async_set_ctl_temperature(
                    self._ctl_temp_unicast, mesh_kelvin
                )
                temp_only = True
            else:
                # No temperature element: Light CTL Set carries a lightness (the
                # last known, else full), so it also lights the lamp.
                level = self._brightness_to_level(
                    self._brightness if self._brightness else HA_BRIGHTNESS_MAX
                )
                await self._coordinator.async_set_ctl(
                    self._unicast, level, mesh_kelvin
                )
                drove_lightness = True

        if not drove_lightness and (not temp_only or not was_on):
            # Plain turn-on, or a temperature-only turn-on of a lamp that was
            # off: switch it on explicitly so the optimistic on-state is true.
            await self._coordinator.async_set_onoff(self._unicast, True)

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Switch the node off via Generic OnOff (optimistic UI first)."""
        self._is_on = False
        self.async_write_ha_state()
        await self._coordinator.async_set_onoff(self._unicast, False)
