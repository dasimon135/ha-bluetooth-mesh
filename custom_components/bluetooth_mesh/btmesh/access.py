"""Access layer: opcode codec plus Config/Generic message codecs (spec §3.7, §4.3).

Scope (Phase 0): the handful of messages a provisioner needs to configure a
node (AppKey Add, Model App Bind, Composition Data Get) and prove end-to-end
control (Generic OnOff). Opcode values and parameter endianness verified
against Zephyr ``subsys/bluetooth/mesh/foundation.h``/``cfg_cli.c`` and
``samples/bluetooth/mesh``; the 12+12-bit key index packing (§4.3.1.1) is
pinned by the §8.3.6 sample payload in the tests. Access parameters are
little-endian; multi-octet opcodes are big-endian on the wire.
"""

from typing import NamedTuple

from .errors import BtMeshError

__all__ = [
    "AccessError",
    # Foundation opcodes
    "OP_CONFIG_APPKEY_ADD",
    "OP_CONFIG_COMPOSITION_DATA_STATUS",
    "OP_CONFIG_APPKEY_STATUS",
    "OP_CONFIG_COMPOSITION_DATA_GET",
    "OP_CONFIG_MODEL_APP_BIND",
    "OP_CONFIG_MODEL_APP_STATUS",
    "OP_GENERIC_ONOFF_GET",
    "OP_GENERIC_ONOFF_SET",
    "OP_GENERIC_ONOFF_STATUS",
    "OP_LIGHT_LIGHTNESS_GET",
    "OP_LIGHT_LIGHTNESS_SET",
    "OP_LIGHT_LIGHTNESS_STATUS",
    "OP_LIGHT_CTL_SET",
    "OP_LIGHT_CTL_SET_UNACK",
    "OP_LIGHT_CTL_STATUS",
    "OP_LIGHT_CTL_TEMPERATURE_SET",
    "OP_LIGHT_CTL_TEMPERATURE_SET_UNACK",
    "OP_LIGHT_CTL_TEMPERATURE_STATUS",
    "OP_ATTENTION_GET",
    "OP_ATTENTION_STATUS",
    "OP_NODE_RESET",
    "OP_NODE_RESET_STATUS",
    # Model IDs
    "MODEL_HEALTH_SERVER",
    "MODEL_GENERIC_ONOFF_SERVER",
    "MODEL_LIGHT_LIGHTNESS_SERVER",
    "MODEL_LIGHT_CTL_SERVER",
    "MODEL_LIGHT_CTL_TEMPERATURE_SERVER",
    # Telink vendor opcodes / constants
    "VD_GROUP_G_GET",
    "VD_GROUP_G_SET",
    "VD_GROUP_G_SET_NOACK",
    "VD_GROUP_G_STATUS",
    "VD_SUB_OP_OFF",
    "VD_SUB_OP_ON",
    "HAEFELE_COMPANY_ID",
    "STATUS_NAMES",
    # Parsed result types
    "AppKeyStatus",
    "ModelAppStatus",
    "GenericOnOffStatus",
    "LightLightnessStatus",
    "LightCtlStatus",
    "LightCtlTemperatureStatus",
    "CompositionElement",
    "CompositionData",
    # Codec
    "encode_opcode",
    "parse_access",
    # Encoders
    "config_appkey_add",
    "config_model_app_bind",
    "config_model_app_bind_vendor",
    "vendor_opcode",
    "vendor_group_onoff_set",
    "vendor_group_set",
    "attention_get",
    "config_node_reset",
    "config_composition_data_get",
    "generic_onoff_get",
    "generic_onoff_set",
    "light_lightness_get",
    "light_lightness_set",
    "light_ctl_set",
    "light_ctl_temperature_set",
    # Decoders
    "parse_config_appkey_status",
    "parse_config_model_app_status",
    "parse_generic_onoff_status",
    "parse_light_lightness_status",
    "parse_light_ctl_status",
    "parse_light_ctl_temperature_status",
    "parse_composition_data_status",
    "RelayStatus",
    "config_relay_get",
    "parse_config_relay_status",
]

# Foundation model opcodes (Zephyr foundation.h).
OP_CONFIG_APPKEY_ADD = 0x00
OP_CONFIG_COMPOSITION_DATA_STATUS = 0x02
OP_CONFIG_APPKEY_STATUS = 0x8003
OP_CONFIG_COMPOSITION_DATA_GET = 0x8008
OP_CONFIG_MODEL_APP_BIND = 0x803D
OP_CONFIG_MODEL_APP_STATUS = 0x803E
OP_CONFIG_RELAY_GET = 0x8026
OP_CONFIG_RELAY_STATUS = 0x8028
# Generic OnOff model opcodes (Mesh Model spec §7.1, Zephyr mesh sample).
OP_GENERIC_ONOFF_GET = 0x8201
OP_GENERIC_ONOFF_SET = 0x8202
OP_GENERIC_ONOFF_STATUS = 0x8204
# Light Lightness model opcodes (Mesh Model spec §7.1).
OP_LIGHT_LIGHTNESS_GET = 0x824B
OP_LIGHT_LIGHTNESS_SET = 0x824C
OP_LIGHT_LIGHTNESS_STATUS = 0x824E
# Light CTL (color temperature) model opcodes (Mesh Model spec §7.1). Verified
# against the Nordic nRF5-SDK-for-Mesh header
# ``models/model_spec/light_ctl/include/light_ctl_messages.h``
# (LIGHT_CTL_OPCODE_*): all six values below matched the spec exactly.
OP_LIGHT_CTL_SET = 0x825E
OP_LIGHT_CTL_SET_UNACK = 0x825F
OP_LIGHT_CTL_STATUS = 0x8260
OP_LIGHT_CTL_TEMPERATURE_SET = 0x8264
OP_LIGHT_CTL_TEMPERATURE_SET_UNACK = 0x8265
OP_LIGHT_CTL_TEMPERATURE_STATUS = 0x8266
# Valid CTL temperature range in Kelvin (Mesh Model spec §6.1.3.1).
_CTL_TEMP_MIN = 0x0320  # 800 K
_CTL_TEMP_MAX = 0x4E20  # 20000 K
# Health model — Attention Get/Status (app-keyed; every node has a Health
# Server, so this is the app-key round-trip sanity check).
OP_ATTENTION_GET = 0x8004
OP_ATTENTION_STATUS = 0x8007
MODEL_HEALTH_SERVER = 0x0002
# Config Node Reset — device-keyed; unprovisions the node (spec §4.3.2.53).
OP_NODE_RESET = 0x8049
OP_NODE_RESET_STATUS = 0x804A

# SIG model IDs of interest (Mesh Model spec §7.3).
MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
MODEL_LIGHT_CTL_SERVER = 0x1303
MODEL_LIGHT_CTL_TEMPERATURE_SERVER = 0x1304

# Telink SIG Mesh SDK vendor "group generic" opcodes (vendor_model.h). On the
# wire a vendor opcode is 1 op-byte (0xC0-0xFF) + 2-byte company ID
# little-endian. 0xC0-0xDF is Telink's range. Häfele (company 0x07E9) builds
# its Connect Mesh firmware on this SDK.
VD_GROUP_G_GET = 0xC1
VD_GROUP_G_SET = 0xC2
VD_GROUP_G_SET_NOACK = 0xC3
VD_GROUP_G_STATUS = 0xC4
# vd_light onoff sub-ops (two separate sub-ops, legacy-compatible).
VD_SUB_OP_OFF = 0x00
VD_SUB_OP_ON = 0x01
HAEFELE_COMPANY_ID = 0x07E9

# Foundation status codes (spec §4.3.5, names per Zephyr foundation.h).
STATUS_NAMES = {
    0x00: "Success",
    0x01: "Invalid Address",
    0x02: "Invalid Model",
    0x03: "Invalid AppKey Index",
    0x04: "Invalid NetKey Index",
    0x05: "Insufficient Resources",
    0x06: "Key Index Already Stored",
    0x07: "Invalid Publish Parameters",
    0x08: "Not a Subscribe Model",
    0x09: "Storage Failure",
    0x0A: "Feature Not Supported",
    0x0B: "Cannot Update",
    0x0C: "Cannot Remove",
    0x0D: "Cannot Bind",
    0x0E: "Temporarily Unable to Change State",
    0x0F: "Cannot Set",
    0x10: "Unspecified Error",
    0x11: "Invalid Binding",
}


class AccessError(BtMeshError):
    """An access payload could not be built or parsed."""


class AppKeyStatus(NamedTuple):
    """Config AppKey Status (opcode 0x8003, spec §4.3.2.40)."""

    status: int
    netkey_idx: int
    appkey_idx: int


class ModelAppStatus(NamedTuple):
    """Config Model App Status (opcode 0x803E, spec §4.3.2.48).

    ``company_id`` is None for a SIG model, set for a vendor model.
    """

    status: int
    element_addr: int
    appkey_idx: int
    model_id: int
    company_id: int | None = None


class GenericOnOffStatus(NamedTuple):
    """Generic OnOff Status (opcode 0x8204, Mesh Model spec §3.2.1.4)."""

    present_onoff: int
    target_onoff: int | None
    remaining_time: int | None


class LightLightnessStatus(NamedTuple):
    """Light Lightness Status (opcode 0x824E, Mesh Model spec §6.3.1.4)."""

    present_lightness: int
    target_lightness: int | None
    remaining_time: int | None


class LightCtlStatus(NamedTuple):
    """Light CTL Status (opcode 0x8260, Mesh Model spec §6.3.2.4)."""

    present_lightness: int
    present_temperature: int
    target_lightness: int | None
    target_temperature: int | None
    remaining_time: int | None


class LightCtlTemperatureStatus(NamedTuple):
    """Light CTL Temperature Status (opcode 0x8266, Mesh Model spec §6.3.3.6).

    ``present_delta_uv`` and ``target_delta_uv`` are signed int16.
    """

    present_temperature: int
    present_delta_uv: int
    target_temperature: int | None
    target_delta_uv: int | None
    remaining_time: int | None


class RelayStatus(NamedTuple):
    """Config Relay Status (spec §4.3.2.15): the node's Relay feature state.

    ``enabled`` is False both when the feature is off and when the node does
    not support it at all — the wire values 0x00 and 0x02 respectively.
    Relevant because a node that does not relay forwards nothing from the proxy
    connection into the rest of the mesh.
    """

    enabled: bool
    supported: bool
    retransmit_count: int
    retransmit_interval_steps: int


class CompositionElement(NamedTuple):
    """One element of a Composition Data Page 0 (spec §4.2.1.1)."""

    loc: int
    sig_models: tuple[int, ...]
    vendor_models: tuple[tuple[int, int], ...]  # (company_id, model_id)


class CompositionData(NamedTuple):
    """Composition Data Page 0 (opcode 0x02, spec §4.2.1)."""

    page: int
    cid: int
    pid: int
    vid: int
    crpl: int
    features: int
    elements: tuple[CompositionElement, ...]

    def describe(self) -> str:
        parts = [
            f"CID {self.cid:#06x}, PID {self.pid:#06x}, VID {self.vid:#06x}, "
            f"CRPL {self.crpl}, features {self.features:#06x}"
        ]
        for i, el in enumerate(self.elements):
            sig = ", ".join(f"{m:#06x}" for m in el.sig_models) or "none"
            vnd = (
                ", ".join(f"{cid:#06x}:{m:#06x}" for cid, m in el.vendor_models)
                or "none"
            )
            parts.append(f"element {i}: SIG models [{sig}]; vendor models [{vnd}]")
        return "; ".join(parts)


# ------------------------------------------------------------- opcode codec


def encode_opcode(opcode: int) -> bytes:
    """Encode an opcode in its 1/2/3-octet on-air form (spec §3.7.3.1)."""
    if 0 <= opcode <= 0x7E:
        return bytes([opcode])
    if 0x8000 <= opcode <= 0xBFFF:
        return opcode.to_bytes(2, "big")
    if 0xC00000 <= opcode <= 0xFFFFFF:
        return opcode.to_bytes(3, "big")
    raise AccessError(f"opcode not encodable: {opcode:#x}")


def parse_access(payload: bytes) -> tuple[int, bytes]:
    """Split an access payload into ``(opcode, parameters)`` (spec §3.7.3.1).

    Opcode forms by the top bits of the first octet: ``0xxxxxxx`` 1 octet
    (0x7F is RFU), ``10xxxxxx`` 2 octets, ``11xxxxxx`` 3 octets (vendor;
    returned as an opaque 24-bit big-endian int, company ID untouched).
    """
    if not payload:
        raise AccessError("empty access payload")
    first = payload[0]
    if first == 0x7F:
        raise AccessError("opcode 0x7F is reserved for future use")
    size = 1 if first < 0x80 else 2 if first < 0xC0 else 3
    if len(payload) < size:
        raise AccessError(f"access payload shorter than its {size}-octet opcode")
    return int.from_bytes(payload[:size], "big"), payload[size:]


def _format_opcode(opcode: int) -> str:
    """Format an opcode with the width of its on-air form (0x00, 0x8003, ...)."""
    width = 4 if opcode <= 0x7E else 6 if opcode <= 0xBFFF else 8
    return f"{opcode:#0{width}x}"


def _expect_params(
    payload: bytes, opcode: int, lengths: tuple[int, ...]
) -> bytes:
    got_opcode, params = parse_access(payload)
    if got_opcode != opcode:
        raise AccessError(
            f"expected opcode {_format_opcode(opcode)}, "
            f"got {_format_opcode(got_opcode)}"
        )
    if len(params) not in lengths:
        raise AccessError(
            f"opcode {_format_opcode(opcode)} expects "
            f"{' or '.join(map(str, lengths))} parameter bytes, "
            f"got {len(params)}"
        )
    return params


# --------------------------------------------------------- key index packing


def _check_key_index(name: str, idx: int) -> None:
    if not 0 <= idx <= 0xFFF:
        raise AccessError(f"{name} out of 12-bit range: {idx:#x}")


def _pack_key_indexes(idx1: int, idx2: int) -> bytes:
    """Pack two 12-bit key indexes into 3 octets (spec §4.3.1.1).

    Little-endian 16 bits of ``idx1 | (idx2[3:0] << 12)`` followed by
    ``idx2 >> 4`` — pinned by §8.3.6: (0x456, 0x123) → ``56 34 12``.
    """
    _check_key_index("first key index", idx1)
    _check_key_index("second key index", idx2)
    return (idx1 | ((idx2 & 0xF) << 12)).to_bytes(2, "little") + bytes([idx2 >> 4])


def _unpack_key_indexes(data: bytes) -> tuple[int, int]:
    """Inverse of :func:`_pack_key_indexes` (3 octets → two 12-bit indexes)."""
    low = int.from_bytes(data[:2], "little")
    return low & 0xFFF, (data[2] << 4) | (low >> 12)


# ------------------------------------------------------------------ encoders


def config_appkey_add(netkey_idx: int, appkey_idx: int, appkey: bytes) -> bytes:
    """Config AppKey Add access payload (spec §4.3.2.37)."""
    if len(appkey) != 16:
        raise AccessError(f"AppKey must be 16 bytes, got {len(appkey)}")
    return (
        encode_opcode(OP_CONFIG_APPKEY_ADD)
        + _pack_key_indexes(netkey_idx, appkey_idx)
        + appkey
    )


def config_model_app_bind(
    element_addr: int, appkey_idx: int, model_id: int
) -> bytes:
    """Config Model App Bind for a SIG model (spec §4.3.2.46)."""
    if not 0 <= element_addr <= 0xFFFF:
        raise AccessError(f"element address out of range: {element_addr:#x}")
    _check_key_index("AppKeyIndex", appkey_idx)
    if not 0 <= model_id <= 0xFFFF:
        raise AccessError(f"SIG model ID out of range: {model_id:#x}")
    return (
        encode_opcode(OP_CONFIG_MODEL_APP_BIND)
        + element_addr.to_bytes(2, "little")
        + appkey_idx.to_bytes(2, "little")
        + model_id.to_bytes(2, "little")
    )


def config_model_app_bind_vendor(
    element_addr: int, appkey_idx: int, company_id: int, model_id: int
) -> bytes:
    """Config Model App Bind for a VENDOR model (spec §4.3.2.46).

    Same opcode as the SIG form but the model identifier is 4 bytes:
    company ID (2 LE) + model ID (2 LE).
    """
    if not 0 <= element_addr <= 0xFFFF:
        raise AccessError(f"element address out of range: {element_addr:#x}")
    _check_key_index("AppKeyIndex", appkey_idx)
    if not 0 <= company_id <= 0xFFFF:
        raise AccessError(f"company ID out of range: {company_id:#x}")
    if not 0 <= model_id <= 0xFFFF:
        raise AccessError(f"vendor model ID out of range: {model_id:#x}")
    return (
        encode_opcode(OP_CONFIG_MODEL_APP_BIND)
        + element_addr.to_bytes(2, "little")
        + appkey_idx.to_bytes(2, "little")
        + company_id.to_bytes(2, "little")
        + model_id.to_bytes(2, "little")
    )


def vendor_opcode(op_byte: int, company_id: int) -> int:
    """Assemble a 3-octet vendor opcode as parse_access returns it.

    On air: op_byte (0xC0-0xFF) then company ID little-endian. As a big-endian
    int that is ``op_byte<<16 | company_lo<<8 | company_hi``.
    """
    if not 0xC0 <= op_byte <= 0xFF:
        raise AccessError(f"vendor op byte out of range: {op_byte:#x}")
    if not 0 <= company_id <= 0xFFFF:
        raise AccessError(f"company ID out of range: {company_id:#x}")
    return int.from_bytes(bytes([op_byte]) + company_id.to_bytes(2, "little"), "big")


def vendor_group_onoff_set(
    company_id: int, on: bool, tid: int, *, ack: bool = True
) -> bytes:
    """Telink vendor VD_GROUP_G_SET on/off message.

    Payload: op-byte + company(2 LE) + sub_op(1: 1=on/0=off) + tid(1).
    """
    if not 0 <= tid <= 0xFF:
        raise AccessError(f"TID out of range: {tid:#x}")
    op_byte = VD_GROUP_G_SET if ack else VD_GROUP_G_SET_NOACK
    sub_op = VD_SUB_OP_ON if on else VD_SUB_OP_OFF
    return bytes([op_byte]) + company_id.to_bytes(2, "little") + bytes([sub_op, tid])


def vendor_group_set(
    company_id: int, sub_op: int, params: bytes = b"", *, ack: bool = True
) -> bytes:
    """Generic Telink vendor VD_GROUP_G_SET with an arbitrary sub-op + params.

    Used by the vendor probe to try sub-ops beyond on/off (level, lightness…).
    """
    if not 0 <= sub_op <= 0xFF:
        raise AccessError(f"sub-op out of range: {sub_op:#x}")
    op_byte = VD_GROUP_G_SET if ack else VD_GROUP_G_SET_NOACK
    return bytes([op_byte]) + company_id.to_bytes(2, "little") + bytes([sub_op]) + params


def attention_get() -> bytes:
    """Health Attention Get (opcode 0x8004, no params) — app-keyed."""
    return encode_opcode(OP_ATTENTION_GET)


def config_node_reset() -> bytes:
    """Config Node Reset (opcode 0x8049, no params) — device-keyed."""
    return encode_opcode(OP_NODE_RESET)


def config_relay_get() -> bytes:
    """Config Relay Get (opcode 0x8026, no params) — device-keyed.

    Its Status is two bytes, so the whole exchange fits in one unsegmented
    message in both directions. That makes it the reachability probe this stack
    can actually rely on: a Composition Data Status is segmented, and nothing
    here transmits Segment Acks, so silence there says nothing.
    """
    return encode_opcode(OP_CONFIG_RELAY_GET)


def config_composition_data_get(page: int = 0) -> bytes:
    """Config Composition Data Get access payload (spec §4.3.2.4)."""
    if not 0 <= page <= 0xFF:
        raise AccessError(f"page out of range: {page:#x}")
    return encode_opcode(OP_CONFIG_COMPOSITION_DATA_GET) + bytes([page])


def generic_onoff_get() -> bytes:
    """Generic OnOff Get (opcode 0x8201, no params) — app-keyed (Model spec §3.2.1.1)."""
    return encode_opcode(OP_GENERIC_ONOFF_GET)


def generic_onoff_set(onoff: bool, tid: int) -> bytes:
    """Generic OnOff Set (acked) without transition time (Model spec §3.2.1.2)."""
    if not 0 <= tid <= 0xFF:
        raise AccessError(f"TID out of range: {tid:#x}")
    return encode_opcode(OP_GENERIC_ONOFF_SET) + bytes([int(bool(onoff)), tid])


def light_lightness_get() -> bytes:
    """Light Lightness Get (opcode 0x824B, no params) — app-keyed (Model spec §6.3.1.1)."""
    return encode_opcode(OP_LIGHT_LIGHTNESS_GET)


def light_lightness_set(lightness: int, tid: int) -> bytes:
    """Light Lightness Set (acked) without transition time (Model spec §6.3.1.2)."""
    if not 0 <= lightness <= 0xFFFF:
        raise AccessError(f"lightness out of range: {lightness:#x}")
    if not 0 <= tid <= 0xFF:
        raise AccessError(f"TID out of range: {tid:#x}")
    return (
        encode_opcode(OP_LIGHT_LIGHTNESS_SET)
        + lightness.to_bytes(2, "little")
        + bytes([tid])
    )


def _check_ctl_temperature(temperature: int) -> None:
    if not _CTL_TEMP_MIN <= temperature <= _CTL_TEMP_MAX:
        raise AccessError(
            f"CTL temperature out of range "
            f"[{_CTL_TEMP_MIN:#x}, {_CTL_TEMP_MAX:#x}]: {temperature:#x}"
        )


def _check_delta_uv(delta_uv: int) -> None:
    if not -32768 <= delta_uv <= 32767:
        raise AccessError(f"CTL delta UV out of int16 range: {delta_uv}")


def light_ctl_set(
    lightness: int, temperature: int, delta_uv: int, tid: int, *, ack: bool = True
) -> bytes:
    """Light CTL Set without transition time (Mesh Model spec §6.3.2.2).

    Params: CTL Lightness (2 LE) + CTL Temperature (2 LE, Kelvin) + CTL Delta UV
    (2 LE, signed int16) + TID (1). ``ack=False`` uses the Set Unacknowledged
    opcode.
    """
    if not 0 <= lightness <= 0xFFFF:
        raise AccessError(f"lightness out of range: {lightness:#x}")
    _check_ctl_temperature(temperature)
    _check_delta_uv(delta_uv)
    if not 0 <= tid <= 0xFF:
        raise AccessError(f"TID out of range: {tid:#x}")
    opcode = OP_LIGHT_CTL_SET if ack else OP_LIGHT_CTL_SET_UNACK
    return (
        encode_opcode(opcode)
        + lightness.to_bytes(2, "little")
        + temperature.to_bytes(2, "little")
        + delta_uv.to_bytes(2, "little", signed=True)
        + bytes([tid])
    )


def light_ctl_temperature_set(
    temperature: int, delta_uv: int, tid: int, *, ack: bool = True
) -> bytes:
    """Light CTL Temperature Set without transition time (Model spec §6.3.3.4).

    Params: CTL Temperature (2 LE, Kelvin) + CTL Delta UV (2 LE, signed int16) +
    TID (1). ``ack=False`` uses the Temperature Set Unacknowledged opcode.
    """
    _check_ctl_temperature(temperature)
    _check_delta_uv(delta_uv)
    if not 0 <= tid <= 0xFF:
        raise AccessError(f"TID out of range: {tid:#x}")
    opcode = (
        OP_LIGHT_CTL_TEMPERATURE_SET if ack else OP_LIGHT_CTL_TEMPERATURE_SET_UNACK
    )
    return (
        encode_opcode(opcode)
        + temperature.to_bytes(2, "little")
        + delta_uv.to_bytes(2, "little", signed=True)
        + bytes([tid])
    )


# ------------------------------------------------------------------ decoders


def parse_config_appkey_status(payload: bytes) -> AppKeyStatus:
    """Parse a Config AppKey Status access payload (spec §4.3.2.40)."""
    params = _expect_params(payload, OP_CONFIG_APPKEY_STATUS, (4,))
    netkey_idx, appkey_idx = _unpack_key_indexes(params[1:4])
    return AppKeyStatus(
        status=params[0], netkey_idx=netkey_idx, appkey_idx=appkey_idx
    )


def parse_config_model_app_status(payload: bytes) -> ModelAppStatus:
    """Parse a Config Model App Status (spec §4.3.2.48).

    7 parameter bytes for a SIG model; 9 for a vendor model (2-byte company ID
    before the model ID).
    """
    params = _expect_params(payload, OP_CONFIG_MODEL_APP_STATUS, (7, 9))
    status = params[0]
    element_addr = int.from_bytes(params[1:3], "little")
    appkey_idx = int.from_bytes(params[3:5], "little")
    if len(params) == 7:
        return ModelAppStatus(
            status=status,
            element_addr=element_addr,
            appkey_idx=appkey_idx,
            model_id=int.from_bytes(params[5:7], "little"),
        )
    return ModelAppStatus(
        status=status,
        element_addr=element_addr,
        appkey_idx=appkey_idx,
        company_id=int.from_bytes(params[5:7], "little"),
        model_id=int.from_bytes(params[7:9], "little"),
    )


def parse_generic_onoff_status(payload: bytes) -> GenericOnOffStatus:
    """Parse a Generic OnOff Status (Model spec §3.2.1.4)."""
    params = _expect_params(payload, OP_GENERIC_ONOFF_STATUS, (1, 3))
    if len(params) == 1:
        return GenericOnOffStatus(
            present_onoff=params[0], target_onoff=None, remaining_time=None
        )
    return GenericOnOffStatus(
        present_onoff=params[0],
        target_onoff=params[1],
        remaining_time=params[2],
    )


def parse_light_lightness_status(payload: bytes) -> LightLightnessStatus:
    """Parse a Light Lightness Status (Model spec §6.3.1.4)."""
    params = _expect_params(payload, OP_LIGHT_LIGHTNESS_STATUS, (2, 5))
    present = int.from_bytes(params[0:2], "little")
    if len(params) == 2:
        return LightLightnessStatus(
            present_lightness=present, target_lightness=None, remaining_time=None
        )
    return LightLightnessStatus(
        present_lightness=present,
        target_lightness=int.from_bytes(params[2:4], "little"),
        remaining_time=params[4],
    )


def parse_light_ctl_status(payload: bytes) -> LightCtlStatus:
    """Parse a Light CTL Status (Model spec §6.3.2.4).

    4 mandatory bytes (present lightness + temperature); 9 bytes with the
    optional target lightness + target temperature + remaining time.
    """
    params = _expect_params(payload, OP_LIGHT_CTL_STATUS, (4, 9))
    present_lightness = int.from_bytes(params[0:2], "little")
    present_temperature = int.from_bytes(params[2:4], "little")
    if len(params) == 4:
        return LightCtlStatus(
            present_lightness=present_lightness,
            present_temperature=present_temperature,
            target_lightness=None,
            target_temperature=None,
            remaining_time=None,
        )
    return LightCtlStatus(
        present_lightness=present_lightness,
        present_temperature=present_temperature,
        target_lightness=int.from_bytes(params[4:6], "little"),
        target_temperature=int.from_bytes(params[6:8], "little"),
        remaining_time=params[8],
    )


def parse_light_ctl_temperature_status(
    payload: bytes,
) -> LightCtlTemperatureStatus:
    """Parse a Light CTL Temperature Status (Model spec §6.3.3.6).

    4 mandatory bytes (present temperature + present delta UV); 9 bytes with the
    optional target temperature + target delta UV + remaining time. Delta UV is
    a signed int16.
    """
    params = _expect_params(payload, OP_LIGHT_CTL_TEMPERATURE_STATUS, (4, 9))
    present_temperature = int.from_bytes(params[0:2], "little")
    present_delta_uv = int.from_bytes(params[2:4], "little", signed=True)
    if len(params) == 4:
        return LightCtlTemperatureStatus(
            present_temperature=present_temperature,
            present_delta_uv=present_delta_uv,
            target_temperature=None,
            target_delta_uv=None,
            remaining_time=None,
        )
    return LightCtlTemperatureStatus(
        present_temperature=present_temperature,
        present_delta_uv=present_delta_uv,
        target_temperature=int.from_bytes(params[4:6], "little"),
        target_delta_uv=int.from_bytes(params[6:8], "little", signed=True),
        remaining_time=params[8],
    )


def parse_config_relay_status(payload: bytes) -> RelayStatus:
    """Parse a Config Relay Status (spec §4.3.2.15).

    Two parameter bytes: Relay (0x00 off, 0x01 on, 0x02 unsupported), then
    RelayRetransmit — count in bits 0..2, interval steps in bits 3..7.
    """
    params = _expect_params(payload, OP_CONFIG_RELAY_STATUS, (2,))
    relay, retransmit = params[0], params[1]
    return RelayStatus(
        enabled=relay == 0x01,
        supported=relay != 0x02,
        retransmit_count=retransmit & 0x07,
        retransmit_interval_steps=(retransmit >> 3) & 0x1F,
    )


def parse_composition_data_status(payload: bytes) -> CompositionData:
    """Parse a Composition Data Status, Page 0 layout (spec §4.2.1/§4.3.2.5).

    Params: Page(1) + CID/PID/VID/CRPL/Features (2 LE each) + per element:
    Loc(2 LE), NumS(1), NumV(1), NumS SIG model IDs (2 LE), NumV vendor
    models (CID 2 LE + model ID 2 LE).
    """
    opcode, params = parse_access(payload)
    if opcode != OP_CONFIG_COMPOSITION_DATA_STATUS:
        raise AccessError(
            f"expected opcode {_format_opcode(OP_CONFIG_COMPOSITION_DATA_STATUS)}, "
            f"got {_format_opcode(opcode)}"
        )
    if len(params) < 11:
        raise AccessError(f"composition data too short: {len(params)} bytes")
    page = params[0]
    cid, pid, vid, crpl, features = (
        int.from_bytes(params[1 + 2 * i : 3 + 2 * i], "little") for i in range(5)
    )
    elements: list[CompositionElement] = []
    off = 11
    while off < len(params):
        if off + 4 > len(params):
            raise AccessError("truncated element header in composition data")
        loc = int.from_bytes(params[off : off + 2], "little")
        num_s, num_v = params[off + 2], params[off + 3]
        off += 4
        end = off + 2 * num_s + 4 * num_v
        if end > len(params):
            raise AccessError("truncated model list in composition data")
        sig = tuple(
            int.from_bytes(params[off + 2 * i : off + 2 * i + 2], "little")
            for i in range(num_s)
        )
        off += 2 * num_s
        vendor = tuple(
            (
                int.from_bytes(params[off + 4 * i : off + 4 * i + 2], "little"),
                int.from_bytes(params[off + 4 * i + 2 : off + 4 * i + 4], "little"),
            )
            for i in range(num_v)
        )
        off += 4 * num_v
        elements.append(
            CompositionElement(loc=loc, sig_models=sig, vendor_models=vendor)
        )
    return CompositionData(
        page=page, cid=cid, pid=pid, vid=vid, crpl=crpl,
        features=features, elements=tuple(elements),
    )
