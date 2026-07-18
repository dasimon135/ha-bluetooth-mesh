"""Access-layer message codec tests.

Key-index packing is pinned to the Mesh Profile §8.3.6 sample: the Config
AppKey Add access payload ``0056341263964771734fbd76e3b40519d1d94a48``
(NetKeyIndex=0x456, AppKeyIndex=0x123, AppKey=63964771...). Opcodes and field
endianness verified against Zephyr ``subsys/bluetooth/mesh/foundation.h`` and
``cfg_cli.c`` (access parameters are little-endian; multi-octet opcodes are
big-endian).
"""

import pytest

from btmesh_min.access import (
    OP_CONFIG_APPKEY_ADD,
    OP_CONFIG_APPKEY_STATUS,
    OP_CONFIG_COMPOSITION_DATA_GET,
    OP_CONFIG_MODEL_APP_BIND,
    OP_CONFIG_MODEL_APP_STATUS,
    OP_GENERIC_ONOFF_SET,
    OP_GENERIC_ONOFF_STATUS,
    STATUS_NAMES,
    AccessError,
    AppKeyStatus,
    GenericOnOffStatus,
    ModelAppStatus,
    config_appkey_add,
    config_composition_data_get,
    config_model_app_bind,
    generic_onoff_set,
    parse_access,
    parse_config_appkey_status,
    parse_config_model_app_status,
    parse_generic_onoff_status,
)

APP_KEY = bytes.fromhex("63964771734fbd76e3b40519d1d94a48")

# §8.3.6 Message #6 access payload — the validated sample already used by
# test_transport.py, reused here to pin the 12+12-bit key index packing.
MSG6_ACCESS = bytes.fromhex("0056341263964771734fbd76e3b40519d1d94a48")


# ------------------------------------------------------------------ encoders


def test_config_appkey_add_matches_spec_sample():
    """§8.3.6: NetKeyIndex=0x456, AppKeyIndex=0x123 pack to 56 34 12."""
    assert config_appkey_add(0x456, 0x123, APP_KEY) == MSG6_ACCESS


def test_config_appkey_add_zero_indexes():
    assert config_appkey_add(0, 0, APP_KEY) == b"\x00\x00\x00\x00" + APP_KEY


def test_config_appkey_add_validates():
    with pytest.raises(AccessError):
        config_appkey_add(0x1000, 0, APP_KEY)  # index is 12-bit
    with pytest.raises(AccessError):
        config_appkey_add(0, 0x1000, APP_KEY)
    with pytest.raises(AccessError):
        config_appkey_add(0, 0, APP_KEY[:15])  # AppKey must be 16 bytes


def test_config_model_app_bind_little_endian_params():
    """Zephyr cfg_cli.c mod_app_bind: le16 elem_addr, le16 app_idx, le16 mod_id."""
    payload = config_model_app_bind(0x1201, 0x0123, 0x1000)
    assert payload == bytes.fromhex("803d" "0112" "2301" "0010")


def test_config_model_app_bind_validates():
    with pytest.raises(AccessError):
        config_model_app_bind(0x10000, 0, 0)
    with pytest.raises(AccessError):
        config_model_app_bind(0, 0x1000, 0)  # AppKeyIndex is 12-bit
    with pytest.raises(AccessError):
        config_model_app_bind(0, 0, 0x10000)


def test_generic_onoff_set():
    assert generic_onoff_set(True, 0x2A) == bytes.fromhex("8202" "01" "2a")
    assert generic_onoff_set(False, 0) == bytes.fromhex("8202" "00" "00")


def test_generic_onoff_set_validates_tid():
    with pytest.raises(AccessError):
        generic_onoff_set(True, 0x100)


def test_config_composition_data_get():
    assert config_composition_data_get() == bytes.fromhex("8008" "00")
    assert config_composition_data_get(page=0xFF) == bytes.fromhex("8008" "ff")


# ------------------------------------------------------------- opcode parser


def test_parse_access_one_byte_opcode():
    assert parse_access(MSG6_ACCESS) == (OP_CONFIG_APPKEY_ADD, MSG6_ACCESS[1:])


def test_parse_access_two_byte_opcode():
    opcode, params = parse_access(bytes.fromhex("8003" "00" "563412"))
    assert opcode == OP_CONFIG_APPKEY_STATUS
    assert params == bytes.fromhex("00563412")


def test_parse_access_three_byte_vendor_opcode():
    # 0b11xxxxxx first octet → 3-octet (vendor) opcode, kept as a 24-bit int.
    opcode, params = parse_access(bytes.fromhex("c15f05" "aa"))
    assert opcode == 0xC15F05
    assert params == b"\xaa"


def test_parse_access_rejects_rfu_and_truncated():
    with pytest.raises(AccessError):
        parse_access(b"")
    with pytest.raises(AccessError):
        parse_access(b"\x7f")  # 0b01111111 is RFU
    with pytest.raises(AccessError):
        parse_access(b"\x80")  # 2-octet opcode cut short
    with pytest.raises(AccessError):
        parse_access(b"\xc1\x5f")  # 3-octet opcode cut short


# ------------------------------------------------------------------ decoders


def test_parse_config_appkey_status_unpacks_indexes():
    """Same §4.3.1.1 packing on the way back: 56 34 12 → 0x456 / 0x123."""
    status = parse_config_appkey_status(bytes.fromhex("8003" "00" "563412"))
    assert status == AppKeyStatus(status=0x00, netkey_idx=0x456, appkey_idx=0x123)


def test_parse_config_appkey_status_error_code():
    status = parse_config_appkey_status(bytes.fromhex("8003" "03" "000000"))
    assert status.status == 0x03
    assert STATUS_NAMES[status.status] == "Invalid AppKey Index"


def test_parse_config_appkey_status_rejects_bad_payload():
    with pytest.raises(AccessError):
        parse_config_appkey_status(bytes.fromhex("8003" "00" "5634"))  # short
    with pytest.raises(AccessError):
        parse_config_appkey_status(bytes.fromhex("8004" "00" "563412"))  # opcode


def test_parse_config_model_app_status():
    status = parse_config_model_app_status(
        bytes.fromhex("803e" "00" "0112" "2301" "0010")
    )
    assert status == ModelAppStatus(
        status=0x00, element_addr=0x1201, appkey_idx=0x0123, model_id=0x1000
    )


def test_parse_config_model_app_status_rejects_bad_payload():
    with pytest.raises(AccessError):
        parse_config_model_app_status(bytes.fromhex("803e" "00" "0112" "2301"))
    with pytest.raises(AccessError):
        parse_config_model_app_status(bytes.fromhex("803d" "00" "0112" "2301" "0010"))


def test_parse_generic_onoff_status_mandatory_only():
    status = parse_generic_onoff_status(bytes.fromhex("8204" "01"))
    assert status == GenericOnOffStatus(
        present_onoff=1, target_onoff=None, remaining_time=None
    )


def test_parse_generic_onoff_status_with_target():
    status = parse_generic_onoff_status(bytes.fromhex("8204" "00" "01" "0f"))
    assert status == GenericOnOffStatus(
        present_onoff=0, target_onoff=1, remaining_time=0x0F
    )


def test_parse_generic_onoff_status_rejects_bad_length():
    with pytest.raises(AccessError):
        parse_generic_onoff_status(bytes.fromhex("8204"))
    with pytest.raises(AccessError):
        parse_generic_onoff_status(bytes.fromhex("8204" "00" "01"))


# ----------------------------------------------------------------- constants


def test_opcode_constants():
    """Values from Zephyr foundation.h / samples/bluetooth/mesh."""
    assert OP_CONFIG_APPKEY_ADD == 0x00
    assert OP_CONFIG_APPKEY_STATUS == 0x8003
    assert OP_CONFIG_COMPOSITION_DATA_GET == 0x8008
    assert OP_CONFIG_MODEL_APP_BIND == 0x803D
    assert OP_CONFIG_MODEL_APP_STATUS == 0x803E
    assert OP_GENERIC_ONOFF_SET == 0x8202
    assert OP_GENERIC_ONOFF_STATUS == 0x8204


def test_status_names_cover_spec_table():
    assert STATUS_NAMES[0x00] == "Success"
    assert STATUS_NAMES[0x11] == "Invalid Binding"
    assert set(STATUS_NAMES) == set(range(0x12))
