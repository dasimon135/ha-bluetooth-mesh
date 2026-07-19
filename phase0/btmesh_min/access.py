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

# Foundation model opcodes (Zephyr foundation.h).
OP_CONFIG_APPKEY_ADD = 0x00
OP_CONFIG_COMPOSITION_DATA_STATUS = 0x02
OP_CONFIG_APPKEY_STATUS = 0x8003
OP_CONFIG_COMPOSITION_DATA_GET = 0x8008
OP_CONFIG_MODEL_APP_BIND = 0x803D
OP_CONFIG_MODEL_APP_STATUS = 0x803E
# Generic OnOff model opcodes (Mesh Model spec §7.1, Zephyr mesh sample).
OP_GENERIC_ONOFF_SET = 0x8202
OP_GENERIC_ONOFF_STATUS = 0x8204
# Light Lightness model opcodes (Mesh Model spec §7.1).
OP_LIGHT_LIGHTNESS_SET = 0x824C
OP_LIGHT_LIGHTNESS_STATUS = 0x824E

# SIG model IDs of interest (Mesh Model spec §7.3).
MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300

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
    """Config Model App Status for a SIG model (opcode 0x803E, spec §4.3.2.48)."""

    status: int
    element_addr: int
    appkey_idx: int
    model_id: int


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


def config_composition_data_get(page: int = 0) -> bytes:
    """Config Composition Data Get access payload (spec §4.3.2.4)."""
    if not 0 <= page <= 0xFF:
        raise AccessError(f"page out of range: {page:#x}")
    return encode_opcode(OP_CONFIG_COMPOSITION_DATA_GET) + bytes([page])


def generic_onoff_set(onoff: bool, tid: int) -> bytes:
    """Generic OnOff Set (acked) without transition time (Model spec §3.2.1.2)."""
    if not 0 <= tid <= 0xFF:
        raise AccessError(f"TID out of range: {tid:#x}")
    return encode_opcode(OP_GENERIC_ONOFF_SET) + bytes([int(bool(onoff)), tid])


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


# ------------------------------------------------------------------ decoders


def parse_config_appkey_status(payload: bytes) -> AppKeyStatus:
    """Parse a Config AppKey Status access payload (spec §4.3.2.40)."""
    params = _expect_params(payload, OP_CONFIG_APPKEY_STATUS, (4,))
    netkey_idx, appkey_idx = _unpack_key_indexes(params[1:4])
    return AppKeyStatus(
        status=params[0], netkey_idx=netkey_idx, appkey_idx=appkey_idx
    )


def parse_config_model_app_status(payload: bytes) -> ModelAppStatus:
    """Parse a Config Model App Status for a SIG model (spec §4.3.2.48).

    Vendor-model responses (9 parameter bytes, extra company ID) are out of
    Phase 0 scope and rejected.
    """
    params = _expect_params(payload, OP_CONFIG_MODEL_APP_STATUS, (7,))
    return ModelAppStatus(
        status=params[0],
        element_addr=int.from_bytes(params[1:3], "little"),
        appkey_idx=int.from_bytes(params[3:5], "little"),
        model_id=int.from_bytes(params[5:7], "little"),
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
