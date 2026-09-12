"""Light platform for the Bluetooth Mesh integration (Task B4).

Every provisioned node hosting a Light Lightness (0x1300) or Generic OnOff
(0x1000) server — on any element — becomes a single :class:`MeshLight` entity.
Each command is then addressed to the element that actually hosts the model it
targets, because an element silently ignores an opcode it has no model for. The
entity's capabilities scale with the node's composition:

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

import logging

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BluetoothMeshConfigEntry
from .const import (
    CONF_INVERTED_CTL,
    DOMAIN,
    MODEL_GENERIC_ONOFF,
    MODEL_LIGHT_CTL,
    MODEL_LIGHT_CTL_TEMP,
    MODEL_LIGHT_LIGHTNESS,
)
from .coordinator import MeshCoordinator

logger = logging.getLogger(__name__)

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


def _temp_element_for(node, element):
    """The CTL Temperature element belonging to ``element``, on a multi-output node.

    A composition lists an output's temperature element right after it, before
    the next output. Taking the node's first one instead would hand every
    channel the temperature server of channel one.
    """
    following = [e for e in node.elements if e.index > element.index]
    next_output = next(
        (e.index for e in _outputs(node) if e.index > element.index), None
    )
    for candidate in following:
        if next_output is not None and candidate.index >= next_output:
            break
        if candidate.has_model(MODEL_LIGHT_CTL_TEMP):
            return candidate
    return None


def _model_unicast(node, model_id: int, *fallback_model_ids: int) -> int:
    """Address of the element hosting ``model_id`` on ``node``.

    ``fallback_model_ids`` are tried in turn for a model that EXTENDS the one
    asked for and therefore answers the same opcodes — a Light Lightness Server
    handles Generic OnOff, so a node that declares only the former is still
    switched by addressing its element. The node's primary address is the last
    resort: it is what a single-element composition means anyway.
    """
    for candidate in (model_id, *fallback_model_ids):
        element = node.element_for_model(candidate)
        if element is not None:
            return element.unicast
    return node.unicast


def _outputs(node) -> tuple:
    """The elements of ``node`` that are each a light in their own right.

    A multi-channel controller is ONE node whose channels are its elements,
    each carrying a full lighting stack: the Häfele 24 V box of
    ha-bluetooth-mesh#30 drives two LED strips from elements 0 and 1, and only
    the first ever reached Home Assistant.

    The test is the Light Lightness server, not "answers an on/off opcode".
    One lamp is free to spread its models over several elements — Generic
    OnOff on element 0, Light Lightness and Light CTL on element 1 — and
    counting every element that answers on/off would cut that lamp in two, one
    half unable to dim and the other unable to switch on. An element that
    carries its own dimmer is an output; anything else belongs to one.

    Falls back to the on/off servers for a node that dims nothing, so a
    two-channel relay still gets one entity per channel.

    Server models only (0x1000 / 0x1300): the client counterparts (0x1001, …)
    are what a remote hosts, and a remote is not a light.
    """
    return node.elements_for_model(MODEL_LIGHT_LIGHTNESS) or node.elements_for_model(
        MODEL_GENERIC_ONOFF
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothMeshConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one light per lighting element (see :func:`_outputs`).

    A node with a single lighting element — every lamp tested here — yields
    exactly the entity it did before, addressed across the whole node, so no
    existing entity is renamed or re-addressed. A node with several yields one
    per element, addressed at that element, sharing one device.
    """
    coordinator = entry.runtime_data
    # Read once here rather than per command in the entity: the options flow is
    # an ``OptionsFlowWithReload``, so changing this rebuilds every entity.
    inverted = set(entry.options.get(CONF_INVERTED_CTL, ()))
    entities: list[MeshLight] = []
    for node in coordinator.network.nodes:
        outputs = _outputs(node)
        if not outputs:
            continue
        if len(outputs) == 1:
            entities.append(
                MeshLight(
                    coordinator, node, invert_ctl=node.unicast in inverted
                )
            )
            continue
        for element in outputs:
            entities.append(
                MeshLight(
                    coordinator,
                    node,
                    element=element,
                    invert_ctl=element.unicast in inverted,
                )
            )
    async_add_entities(entities)


class MeshLight(LightEntity):
    """A mesh node exposed as a Home Assistant light.

    Optimistic for the length of one round trip: a tap shows at once, then
    the cached ``is_on`` / ``brightness`` / ``color_temp_kelvin`` settle on what
    the node *answered* in its Status -- or fall back to what they were, when
    it answered nothing. A Set that times out is not a state; until 2026-09-10
    it was shown as one, on a node that had stopped applying writes while still
    answering reads, and nothing above debug said so (see _note_answer).
    """

    _attr_has_entity_name = True
    _attr_name = None
    # Availability and state are pushed by the coordinator (see
    # async_added_to_hass); there is nothing for Home Assistant to poll.
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: MeshCoordinator,
        node,
        *,
        element=None,
        invert_ctl: bool = False,
    ) -> None:
        """One light on ``node``, or on ``element`` of it.

        ``element`` is given only for a node with several outputs (see
        :func:`_outputs`). It scopes BOTH capability detection and addressing
        to that element: on such a node the models are duplicated per channel,
        so a node-wide search would send every channel's command to the first
        one's address.
        """
        self._coordinator = coordinator
        self._node = node
        self._element = element
        self._unicast = (element or node).unicast
        # Whether this lamp's CTL server maps temperature inversely. Frozen at
        # construction rather than read per command: the options flow is an
        # ``OptionsFlowWithReload``, so changing it rebuilds every entity.
        self._invert_ctl = invert_ctl

        # Keyed on the element this light IS, which for a single-output node
        # is element 0, whose address is the node's — so every entity that
        # exists today keeps its unique id, and with it its history and any
        # customisation.
        self._attr_unique_id = (
            f"{coordinator.network.identifier}_{self._unicast:04x}"
        )
        # The DEVICE stays the node: two strips in one box are two entities on
        # one device, not two boxes. Node-keyed, so existing devices are
        # untouched as well.
        device_uid = f"{coordinator.network.identifier}_{node.unicast:04x}"

        # Capability → HA color mode. COLOR_TEMP implies brightness support in
        # HA, so a CTL node needs only that single mode in the set.
        scope = element or node
        if scope.has_model(MODEL_LIGHT_CTL):
            mode = ColorMode.COLOR_TEMP
            self._attr_min_color_temp_kelvin = DEFAULT_MIN_KELVIN
            self._attr_max_color_temp_kelvin = DEFAULT_MAX_KELVIN
        elif scope.has_model(MODEL_LIGHT_LIGHTNESS):
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
            identifiers={(DOMAIN, device_uid)},
            name=node.name or f"Mesh {node.unicast:04x}",
            manufacturer=_manufacturer(node.cid),
            model=model,
        )
        # A single-output node leaves the name to the device, as before. Each
        # output of a multi-output one needs its own, or two strips arrive as
        # two entities with one name between them; the export usually supplies
        # it, and the element address is the fallback that is at least unique.
        if element is not None:
            self._attr_name = element.name or f"Output {element.unicast:04x}"

        # Every command is addressed to the element that actually HOSTS the
        # model it targets, not to the node's primary address. An element
        # ignores an opcode it has no model for — without acting and without
        # answering — so a node that lays its lighting servers out across
        # several elements would take every command in silence.
        if element is not None:
            # An output carries its own stack: every opcode goes to it.
            self._onoff_unicast = element.unicast
            self._lightness_unicast = element.unicast
            self._ctl_unicast = element.unicast
        else:
            self._onoff_unicast = _model_unicast(
                node, MODEL_GENERIC_ONOFF, MODEL_LIGHT_LIGHTNESS
            )
            self._lightness_unicast = _model_unicast(node, MODEL_LIGHT_LIGHTNESS)
            self._ctl_unicast = _model_unicast(node, MODEL_LIGHT_CTL)
        # The Light CTL Temperature server, if any, is on its own element with
        # its own unicast — address temperature-only changes there so brightness
        # is left untouched. None → fall back to Light CTL Set instead.
        temp_element = (
            _temp_element_for(node, element)
            if element is not None
            else node.element_for_model(MODEL_LIGHT_CTL_TEMP)
        )
        self._ctl_temp_unicast = (
            temp_element.unicast if temp_element is not None else None
        )

        # Optimistic cache. ``None`` means "not known yet" — never claim a state
        # that has not been commanded or read (see the ``is_on`` property).
        self._is_on: bool | None = None
        self._brightness: int | None = None
        self._color_temp_kelvin: int | None = None
        # Sets the node has not acknowledged since it last did. One warning
        # per such outage, one info line when it ends -- a warning per tap
        # would bury the first, and a silent node is exactly the failure a
        # user cannot see from the dashboard.
        self._unacknowledged = 0
        # The exposed range starts as the conventional default and is replaced
        # by the lamp's own the first time it answers. Asked once: it is a
        # property of the device, not a state.
        self._range_read = False

    # -------------------------------------------------------------- lifecycle

    async def async_added_to_hass(self) -> None:
        """Track the coordinator and seed the cache from the lamp itself.

        Reading is only attempted while the mesh is actually reachable. At Home
        Assistant startup the entity is added before the Bluetooth proxies have
        finished registering their scanners, so a read fired here finds no proxy
        and answers nothing — hence the subscription: the read happens when the
        coordinator BECOMES available, and again after every reconnection, which
        also catches whatever changed while we were away.
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_availability)
        )
        if self._coordinator.available:
            self._schedule_refresh()

    @callback
    def _handle_availability(self) -> None:
        """Push the availability change to HA, and re-read when back online."""
        self.async_write_ha_state()
        if self._coordinator.available:
            self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Read the lamp in the background; a round trip must not block setup."""
        self.hass.async_create_background_task(
            self.async_refresh_state(),
            f"bluetooth_mesh refresh {self._unicast:04x}",
        )

    async def async_refresh_state(self) -> None:
        """Ask the lamp what it is actually doing and cache the answer.

        The optimistic cache starts blank, so before this a lamp that was
        physically lit came back as *off* after every Home Assistant restart and
        stayed wrong until someone touched it. Reading it is only possible now
        that the proxy address filter lets Status replies through.

        Best-effort: an unanswered GET leaves the cache exactly as it was —
        never invent a state from silence.
        """
        on = await self._coordinator.async_get_onoff(self._onoff_unicast)
        if on is None:
            return
        self._is_on = on
        # An off lamp reports lightness 0, which is not a brightness worth
        # showing — HA wants no brightness at all while off.
        if on and self._attr_color_mode is not ColorMode.ONOFF:
            level = await self._coordinator.async_get_lightness(self._lightness_unicast)
            if level is not None:
                self._brightness = self._level_to_brightness(level)
        if self._attr_color_mode is ColorMode.COLOR_TEMP:
            await self._refresh_ctl()
        self.async_write_ha_state()

    async def _refresh_ctl(self) -> None:
        """Read the lamp's colour temperature, and once, the range it works in.

        No on/off gate, unlike brightness: that one is skipped on an off lamp
        because an off lamp reports lightness 0, which is not a brightness worth
        showing. A temperature is held across off/on and has no such problem.

        The range is asked for once. It changes the exposed limits — and with
        them the mirror's pivot — so a lamp whose real range is not the assumed
        2700..6500 stops being quietly mis-mirrored. A silent or invalid answer
        leaves the default standing rather than collapsing the range to nothing.
        """
        if not self._range_read:
            self._range_read = True
            ctl_range = await self._coordinator.async_get_ctl_temperature_range(
                self._ctl_unicast
            )
            if ctl_range is not None:
                self._attr_min_color_temp_kelvin = ctl_range[0]
                self._attr_max_color_temp_kelvin = ctl_range[1]

        # The Temperature server lives on its own element; a node without one
        # answers Light CTL instead, mirroring the fallback the send path takes.
        if self._ctl_temp_unicast is not None:
            kelvin = await self._coordinator.async_get_ctl_temperature(
                self._ctl_temp_unicast
            )
        else:
            kelvin = await self._coordinator.async_get_ctl(self._ctl_unicast)
        if kelvin is not None:
            self._color_temp_kelvin = self._ha_kelvin(kelvin)

    # ------------------------------------------------------------- properties

    @property
    def available(self) -> bool:
        """Track the coordinator's proxy connection."""
        return self._coordinator.available

    @property
    def is_on(self) -> bool | None:
        """``None`` until the lamp has been read or commanded ("unknown").

        Reporting *off* for a lamp nobody has looked at is not a harmless
        default: another integration acting on it — a light group syncing its
        members is enough — switches the lamp off for real, and the invented
        state becomes true. Home Assistant renders ``None`` as ``unknown``,
        which is what we actually have.
        """
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

        Some lamps map the Light CTL temperature inversely to their warm/cool
        LEDs (dragging toward warm produces cool and vice-versa), so for a lamp
        marked as one the requested Kelvin is mirrored around the midpoint of
        the exposed range before sending: ``min + max - K``. The mirror stays
        inside the exposed 2700..6500 K band, so the controller's spec-range
        clamp never triggers, and HA keeps displaying the un-mirrored value the
        user selected.

        Every unmarked lamp gets the value untouched: the mirror is a quirk, not
        the spec, and applying it to a conformant lamp would invert warm and
        cool end to end.

        Which lamps are marked is a stored option, not a property of the vendor.
        It was gated on the company identifier until 0.5.1, when issue #7 turned
        up a Häfele lamp that the mirror was itself inverting: the quirk varies
        within a vendor, by model or firmware, so a CID cannot predict it.
        """
        return self._mirror(kelvin) if self._invert_ctl else kelvin

    def _ha_kelvin(self, kelvin: int) -> int:
        """The value this lamp's CTL server reported → what to display.

        A marked lamp reports the temperature it was *sent*, which is the
        mirrored one: ask for 2700 K, we send 6500 K, and the lamp answers
        6500 K. Showing that raw would put a wrong number in front of exactly
        the users the option exists for.

        Same reflection as outbound, because it is its own inverse. Two names
        rather than one so the direction is legible where it is called — a
        single ``_ctl_kelvin`` used both ways reads as a bug.
        """
        return self._mirror(kelvin) if self._invert_ctl else kelvin

    def _mirror(self, kelvin: int) -> int:
        """Reflect a Kelvin value around the midpoint of the EXPOSED range.

        The range matters: a lamp maps its temperature inversely within its own
        limits, so reflecting around an assumed 2700..6500 is off-centre by
        twice the difference of the midpoints on any lamp that is not one.
        Since the range is read from the lamp, this follows it for free.
        """
        return (
            self._attr_min_color_temp_kelvin
            + self._attr_max_color_temp_kelvin
            - kelvin
        )

    # ---------------------------------------------------------------- commands

    def _note_answer(self, command: str, answered: bool) -> None:
        """Account for whether the node acknowledged ``command``.

        Silence while the mesh is unreachable is the coordinator's news (it
        warns, and takes the entity unavailable); the entity only reports the
        other case, a node that is reachable and does not answer its writes.
        """
        if answered:
            if self._unacknowledged:
                logger.info(
                    "mesh node %#06x acknowledges again after %d unacknowledged "
                    "commands",
                    self._unicast,
                    self._unacknowledged,
                )
                self._unacknowledged = 0
            return
        self._unacknowledged += 1
        if self._unacknowledged == 1 and self._coordinator.available:
            logger.warning(
                "mesh node %#06x did not acknowledge %s; showing its last known "
                "state. It answers reads but not writes: power-cycle it if this "
                "persists",
                self._unicast,
                command,
            )
        else:
            logger.debug(
                "mesh node %#06x did not acknowledge %s (%d in a row)",
                self._unicast,
                command,
                self._unacknowledged,
            )

    def _settle_onoff(self, answer: bool | None, requested: bool, was_on) -> None:
        """Show the on/off state the node answered, else the one before."""
        self._note_answer("set_onoff", answer is not None)
        if answer is None:
            self._is_on = was_on
            return
        self._is_on = answer
        if answer is not requested:
            logger.warning(
                "mesh node %#06x answered %s to an %s command",
                self._unicast,
                "on" if answer else "off",
                "on" if requested else "off",
            )

    async def async_turn_on(self, **kwargs) -> None:
        """Apply requested brightness and/or temperature and ensure the lamp is on.

        Each attribute is applied independently so a temperature change never
        disturbs brightness and vice-versa: temperature goes to the Light CTL
        Temperature server on its own element (carrying no lightness) when the
        node exposes one, else falls back to Light CTL Set (which must carry a
        lightness). Because that Temperature message does NOT switch the light on,
        a turn-on that only changes temperature — or a bare turn-on — also sends
        Generic OnOff, so the lamp actually lights instead of HA showing it on
        while it stays dark. Each attribute then settles on the node's Status
        reply, and falls back to its previous value when there is none.
        """
        was_on = self._is_on
        previous_brightness = self._brightness
        previous_kelvin = self._color_temp_kelvin

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
        # Whether a command that lights the lamp was acknowledged: only then
        # is the optimistic "on" a state the node has confirmed.
        lit = False
        temp_only = False

        if ATTR_BRIGHTNESS in kwargs:
            result = await self._coordinator.async_set_lightness(
                self._lightness_unicast,
                self._brightness_to_level(kwargs[ATTR_BRIGHTNESS]),
            )
            self._note_answer("set_lightness", result is not None)
            if result is not None:  # the settled lightness the node reports
                self._brightness = self._level_to_brightness(result)
                lit = True
            else:
                self._brightness = previous_brightness
            drove_lightness = True

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            mesh_kelvin = self._mesh_kelvin(kwargs[ATTR_COLOR_TEMP_KELVIN])
            if self._ctl_temp_unicast is not None:
                # Dedicated CTL Temperature element: sets ONLY temperature and
                # does NOT switch the light on.
                result = await self._coordinator.async_set_ctl_temperature(
                    self._ctl_temp_unicast, mesh_kelvin
                )
                self._note_answer("set_ctl_temperature", result is not None)
                temp_only = True
            else:
                # No temperature element: Light CTL Set carries a lightness (the
                # last known, else full), so it also lights the lamp.
                level = self._brightness_to_level(
                    self._brightness if self._brightness else HA_BRIGHTNESS_MAX
                )
                result = await self._coordinator.async_set_ctl(
                    self._ctl_unicast, level, mesh_kelvin
                )
                self._note_answer("set_ctl", result is not None)
                drove_lightness = True
                lit = lit or result is not None
            if result is not None:
                self._color_temp_kelvin = self._ha_kelvin(result)
            else:
                self._color_temp_kelvin = previous_kelvin

        if not drove_lightness and (not temp_only or not was_on):
            # Plain turn-on, or a temperature-only turn-on of a lamp that was
            # off: switch it on explicitly so the optimistic on-state is true.
            answer = await self._coordinator.async_set_onoff(
                self._onoff_unicast, True
            )
            self._settle_onoff(answer, True, was_on)
        elif not lit:
            # Nothing that lights the lamp was acknowledged (a temperature-only
            # change on a lamp already on, or a lightness the node ignored):
            # the on-state is whatever it was, not what the tap assumed.
            self._is_on = was_on

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Switch the node off via Generic OnOff (optimistic UI first)."""
        was_on = self._is_on
        self._is_on = False
        self.async_write_ha_state()
        answer = await self._coordinator.async_set_onoff(self._onoff_unicast, False)
        self._settle_onoff(answer, False, was_on)
        self.async_write_ha_state()
