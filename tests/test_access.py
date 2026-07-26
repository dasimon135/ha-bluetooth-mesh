"""Access-layer message codec tests.

Key-index packing is pinned to the Mesh Profile §8.3.6 sample: the Config
AppKey Add access payload ``0056341263964771734fbd76e3b40519d1d94a48``
(NetKeyIndex=0x456, AppKeyIndex=0x123, AppKey=63964771...). Opcodes and field
endianness verified against Zephyr ``subsys/bluetooth/mesh/foundation.h`` and
``cfg_cli.c`` (access parameters are little-endian; multi-octet opcodes are
big-endian).
"""

import pytest

from btmesh.access import (
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
    generic_onoff_get,
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


def test_generic_onoff_get():
    """Generic OnOff Get (opcode 0x8201) is a bare 2-octet opcode, no params."""
    assert generic_onoff_get() == bytes.fromhex("8201")


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


# ---------------------------------------------- composition data + lightness


def test_light_lightness_set_layout():
    from btmesh.access import light_lightness_set

    assert light_lightness_set(0xFFFF, 5) == bytes.fromhex("824cffff05")
    assert light_lightness_set(0, 0) == bytes.fromhex("824c000000")


def test_parse_light_lightness_status_short_and_full():
    from btmesh.access import parse_light_lightness_status

    s = parse_light_lightness_status(bytes.fromhex("824e3412"))
    assert s.present_lightness == 0x1234
    assert s.target_lightness is None
    full = parse_light_lightness_status(bytes.fromhex("824e0000ffff0a"))
    assert full.present_lightness == 0
    assert full.target_lightness == 0xFFFF
    assert full.remaining_time == 0x0A


def test_light_ctl_set_layout():
    from btmesh.access import light_ctl_set

    # lightness 0x8000, temperature 0x1F40 (8000 K), delta_uv 0, tid 5.
    assert light_ctl_set(0x8000, 0x1F40, 0, 5) == bytes.fromhex(
        "825e" "0080" "401f" "0000" "05"
    )


def test_light_ctl_set_delta_uv_signed():
    from btmesh.access import light_ctl_set

    # delta_uv -100 → little-endian signed int16 == 9cff.
    assert (-100).to_bytes(2, "little", signed=True) == bytes.fromhex("9cff")
    assert light_ctl_set(0, 0x0320, -100, 0) == bytes.fromhex(
        "825e" "0000" "2003" "9cff" "00"
    )


def test_light_ctl_set_unack_uses_unack_opcode():
    from btmesh.access import light_ctl_set

    assert light_ctl_set(0, 0x0320, 0, 1, ack=False) == bytes.fromhex(
        "825f" "0000" "2003" "0000" "01"
    )


def test_light_ctl_set_validates_ranges():
    from btmesh.access import light_ctl_set

    with pytest.raises(AccessError):
        light_ctl_set(0x10000, 0x0320, 0, 0)  # lightness too large
    with pytest.raises(AccessError):
        light_ctl_set(0, 0x0100, 0, 0)  # temperature below 0x0320
    with pytest.raises(AccessError):
        light_ctl_set(0, 0x5000, 0, 0)  # temperature above 0x4E20
    with pytest.raises(AccessError):
        light_ctl_set(0, 0x0320, 40000, 0)  # delta_uv above int16 max
    with pytest.raises(AccessError):
        light_ctl_set(0, 0x0320, 0, 0x100)  # tid out of range


def test_light_ctl_temperature_set_layout():
    from btmesh.access import light_ctl_temperature_set

    assert light_ctl_temperature_set(0x4E20, 0, 3) == bytes.fromhex(
        "8264" "204e" "0000" "03"
    )


def test_light_ctl_temperature_set_unack_and_signed_delta_uv():
    from btmesh.access import light_ctl_temperature_set

    assert light_ctl_temperature_set(0x0320, -100, 0, ack=False) == bytes.fromhex(
        "8265" "2003" "9cff" "00"
    )


def test_light_ctl_temperature_set_validates_ranges():
    from btmesh.access import light_ctl_temperature_set

    with pytest.raises(AccessError):
        light_ctl_temperature_set(0x0100, 0, 0)  # below range
    with pytest.raises(AccessError):
        light_ctl_temperature_set(0x5000, 0, 0)  # above range
    with pytest.raises(AccessError):
        light_ctl_temperature_set(0x0320, -40000, 0)  # delta_uv below int16 min


def test_parse_light_ctl_status_short_and_full():
    from btmesh.access import parse_light_ctl_status

    s = parse_light_ctl_status(bytes.fromhex("8260" "0080" "401f"))
    assert s.present_lightness == 0x8000
    assert s.present_temperature == 0x1F40
    assert s.target_lightness is None
    assert s.target_temperature is None
    assert s.remaining_time is None
    full = parse_light_ctl_status(
        bytes.fromhex("8260" "0080" "401f" "ffff" "204e" "0a")
    )
    assert full.present_lightness == 0x8000
    assert full.present_temperature == 0x1F40
    assert full.target_lightness == 0xFFFF
    assert full.target_temperature == 0x4E20
    assert full.remaining_time == 0x0A


def test_parse_light_ctl_temperature_status_short_and_full():
    from btmesh.access import parse_light_ctl_temperature_status

    # present temp 0x1F40, present delta_uv -100 (9cff signed).
    s = parse_light_ctl_temperature_status(bytes.fromhex("8266" "401f" "9cff"))
    assert s.present_temperature == 0x1F40
    assert s.present_delta_uv == -100
    assert s.target_temperature is None
    assert s.target_delta_uv is None
    assert s.remaining_time is None
    full = parse_light_ctl_temperature_status(
        bytes.fromhex("8266" "2003" "0000" "204e" "9cff" "0a")
    )
    assert full.present_temperature == 0x0320
    assert full.present_delta_uv == 0
    assert full.target_temperature == 0x4E20
    assert full.target_delta_uv == -100
    assert full.remaining_time == 0x0A


def test_parse_composition_data_status():
    from btmesh.access import parse_composition_data_status

    # Synthetic page 0: CID 0x0211 (Telink), PID 1, VID 2, CRPL 10,
    # features 0x0007; one element: loc 0x0100, 2 SIG models (0x0000 Config
    # Server, 0x1300 Lightness Server), 1 vendor model 0x0211:0x0001.
    params = (
        "00" "1102" "0100" "0200" "0a00" "0700"
        "0001" "02" "01" "0000" "0013" "1102" "0100"
    )
    comp = parse_composition_data_status(bytes.fromhex("02" + params))
    assert comp.cid == 0x0211
    assert comp.features == 0x0007
    assert len(comp.elements) == 1
    el = comp.elements[0]
    assert el.sig_models == (0x0000, 0x1300)
    assert el.vendor_models == ((0x0211, 0x0001),)
    assert "0x1300" in comp.describe()


def test_parse_composition_data_truncated_raises():
    from btmesh.access import AccessError, parse_composition_data_status

    with pytest.raises(AccessError):
        parse_composition_data_status(bytes.fromhex("0200110201000200"))
    with pytest.raises(AccessError):
        # Element header claims 3 SIG models but only 1 present.
        parse_composition_data_status(
            bytes.fromhex("02" + "001102010002000a000700" + "000103000000")
        )


# ------------------------------------------------------ vendor (Telink/Häfele)


def test_vendor_group_onoff_set_bytes():
    from btmesh.access import HAEFELE_COMPANY_ID, vendor_group_onoff_set

    # C2 (SET) + E9 07 (company 0x07E9 LE) + 01 (ON) + 05 (tid)
    assert vendor_group_onoff_set(HAEFELE_COMPANY_ID, True, 5) == bytes.fromhex("c2e9070105")
    # C2 + E9 07 + 00 (OFF) + 00
    assert vendor_group_onoff_set(HAEFELE_COMPANY_ID, False, 0) == bytes.fromhex("c2e9070000")
    # NoAck uses C3
    assert vendor_group_onoff_set(HAEFELE_COMPANY_ID, True, 0, ack=False) == bytes.fromhex("c3e9070100")


def test_vendor_opcode_roundtrips_through_parse_access():
    from btmesh.access import (
        HAEFELE_COMPANY_ID,
        VD_GROUP_G_STATUS,
        parse_access,
        vendor_opcode,
    )

    op = vendor_opcode(VD_GROUP_G_STATUS, HAEFELE_COMPANY_ID)
    # Status on the wire: C4 E9 07 <sub_op>
    opcode, params = parse_access(bytes.fromhex("c4e90701"))
    assert opcode == op
    assert params == b"\x01"


def test_config_model_app_bind_vendor_bytes():
    from btmesh.access import HAEFELE_COMPANY_ID, config_model_app_bind_vendor

    # 803D + elem 0003 LE + appidx 0000 LE + company E9 07 LE + model 0010 LE
    got = config_model_app_bind_vendor(0x0003, 0, HAEFELE_COMPANY_ID, 0x1000)
    assert got == bytes.fromhex("803d") + bytes.fromhex("0300") + bytes.fromhex("0000") + bytes.fromhex("e907") + bytes.fromhex("0010")


def test_parse_model_app_status_vendor_clean():
    from btmesh.access import parse_config_model_app_status

    payload = bytes.fromhex("803e") + bytes([0x00]) + bytes.fromhex("0300") + bytes.fromhex("0000") + bytes.fromhex("e907") + bytes.fromhex("0010")
    s = parse_config_model_app_status(payload)
    assert s.status == 0x00
    assert s.element_addr == 0x0003
    assert s.company_id == 0x07E9
    assert s.model_id == 0x1000


def test_parse_model_app_status_sig_form_unchanged():
    from btmesh.access import parse_config_model_app_status

    payload = bytes.fromhex("803e") + bytes([0x00]) + bytes.fromhex("0300") + bytes.fromhex("0000") + bytes.fromhex("0010")
    s = parse_config_model_app_status(payload)
    assert s.company_id is None
    assert s.model_id == 0x1000
