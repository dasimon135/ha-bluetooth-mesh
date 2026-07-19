"""Tests for btmesh.prov_pdu (Mesh Profile spec §5.4.1 Provisioning PDUs).

Layout sources:
- Mesh Profile 1.0.1 §5.4.1 (opcode in first byte, 2 padding bits = 0)
- Silvair python-bluetooth-mesh-network tests/test_provisioning.py
  https://github.com/SilvairGit/python-bluetooth-mesh-network/blob/master/tests/test_provisioning.py
- btstack test/mesh/provisioning_device_test.cpp
  https://github.com/bluekitchen/btstack/blob/master/test/mesh/provisioning_device_test.cpp
- Zephyr subsys/bluetooth/mesh/prov.h (opcode + error code values)
  https://github.com/zephyrproject-rtos/zephyr/blob/main/subsys/bluetooth/mesh/prov.h
"""

import pytest

from btmesh.prov_pdu import (
    Capabilities,
    Complete,
    Confirmation,
    Data,
    Failed,
    Invite,
    ProvisioningPDUError,
    PublicKey,
    Random,
    Start,
    decode,
)

# ---------------------------------------------------------------------------
# Round trips, one per PDU type
# ---------------------------------------------------------------------------


def test_invite_round_trip():
    pdu = Invite(attention_duration=0x10)
    encoded = pdu.encode()
    assert encoded == bytes.fromhex("0010")
    assert decode(encoded) == pdu


def test_capabilities_round_trip():
    pdu = Capabilities(
        num_elements=1,
        algorithms=0x0003,
        public_key_type=0x01,
        static_oob_type=0x01,
        output_oob_size=8,
        output_oob_actions=0x0009,
        input_oob_size=5,
        input_oob_actions=0x0002,
    )
    encoded = pdu.encode()
    # Same field values as the Silvair test_provisioning.py "capabilities" case;
    # confirms 16-bit fields are big-endian.
    assert encoded == bytes.fromhex("010100030101080009050002")
    assert decode(encoded) == pdu


def test_start_round_trip():
    pdu = Start(
        algorithm=0x00,
        public_key=0x00,
        auth_method=0x02,
        auth_action=0x00,
        auth_size=0x01,
    )
    encoded = pdu.encode()
    # btstack provisioning_device_test.cpp prov_start
    assert encoded == bytes.fromhex("020000020001")
    assert decode(encoded) == pdu


def test_public_key_round_trip():
    x = bytes(range(32))
    y = bytes(range(32, 64))
    pdu = PublicKey(x=x, y=y)
    encoded = pdu.encode()
    assert encoded == bytes([0x03]) + x + y
    assert len(encoded) == 65
    assert decode(encoded) == pdu


def test_confirmation_round_trip():
    value = bytes.fromhex("b38a114dfdca1fe153bd2c1e0dc46ac2")
    pdu = Confirmation(value=value)
    encoded = pdu.encode()
    assert encoded == bytes([0x05]) + value
    assert decode(encoded) == pdu


def test_random_round_trip():
    value = bytes.fromhex("8b19ac31d58b124c946209b5db1021b9")
    pdu = Random(value=value)
    encoded = pdu.encode()
    assert encoded == bytes([0x06]) + value
    assert decode(encoded) == pdu


def test_data_round_trip():
    # Mesh Profile 1.0.1 §8.7.2 sample: encrypted provisioning data + MIC
    # (via Silvair test_provisioning.py prov_params).
    encrypted = bytes.fromhex("d0bd7f4a89a2ff6222af59a90a60ad58acfe3123356f5cec29")
    mic = bytes.fromhex("73e0ec50783b10c7")
    pdu = Data(encrypted=encrypted, mic=mic)
    encoded = pdu.encode()
    assert encoded == bytes([0x07]) + encrypted + mic
    assert len(encoded) == 34
    assert decode(encoded) == pdu


def test_complete_round_trip():
    pdu = Complete()
    encoded = pdu.encode()
    assert encoded == bytes([0x08])
    assert decode(encoded) == pdu


def test_failed_round_trip():
    pdu = Failed(error_code=0x04)
    encoded = pdu.encode()
    assert encoded == bytes.fromhex("0904")
    assert decode(encoded) == pdu


# ---------------------------------------------------------------------------
# Spec sample data
# ---------------------------------------------------------------------------


def test_capabilities_spec_8_7_2_sample():
    """Decode the §8.7.2 Capabilities PDU from the Mesh Profile 1.0.1 sample data.

    ConfirmationInputs capabilities portion is 0100010000000000000000 (the
    on-air PDU prefixes opcode 0x01).  Source: Silvair
    python-bluetooth-mesh-network tests/test_provisioning.py
    (test_confirmation: capabilities = "0100010000000000000000"),
    https://github.com/SilvairGit/python-bluetooth-mesh-network/blob/master/tests/test_provisioning.py
    """
    pdu = decode(bytes.fromhex("01" + "0100010000000000000000"))
    assert isinstance(pdu, Capabilities)
    assert pdu.num_elements == 1
    assert pdu.algorithms == 0x0001  # FIPS P-256 Elliptic Curve
    assert pdu.public_key_type == 0x00
    assert pdu.static_oob_type == 0x00
    assert pdu.output_oob_size == 0
    assert pdu.output_oob_actions == 0x0000
    assert pdu.input_oob_size == 0
    assert pdu.input_oob_actions == 0x0000


def test_capabilities_btstack_sample():
    """Decode the btstack provisioning_device_test.cpp prov_capabilities PDU.

    https://github.com/bluekitchen/btstack/blob/master/test/mesh/provisioning_device_test.cpp
    """
    pdu = decode(bytes.fromhex("010100010001080008080008"))
    assert isinstance(pdu, Capabilities)
    assert pdu.num_elements == 1
    assert pdu.algorithms == 0x0001
    assert pdu.public_key_type == 0x00
    assert pdu.static_oob_type == 0x01
    assert pdu.output_oob_size == 8
    assert pdu.output_oob_actions == 0x0008
    assert pdu.input_oob_size == 8
    assert pdu.input_oob_actions == 0x0008


def test_capabilities_describe_is_readable():
    """describe() must name capabilities in words (go/no-go diagnostics)."""
    pdu = decode(bytes.fromhex("01" + "0100010000000000000000"))
    text = pdu.describe()
    assert "FIPS P-256" in text
    assert "1 element" in text
    described = Capabilities(
        num_elements=2,
        algorithms=0x0001,
        public_key_type=0x01,
        static_oob_type=0x01,
        output_oob_size=4,
        output_oob_actions=0x0009,  # Blink | Output Numeric
        input_oob_size=5,
        input_oob_actions=0x0002,  # Twist
    ).describe()
    assert "Blink" in described
    assert "Output Numeric" in described
    assert "Twist" in described
    assert "OOB public key" in described
    assert "static OOB" in described
    # repr should carry the same readable description
    assert "Blink" in repr(
        Capabilities(1, 0x0001, 0, 0, 1, 0x0001, 0, 0)
    )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_decode_unknown_opcode_raises_with_raw_bytes():
    raw = bytes.fromhex("2a010203")
    with pytest.raises(ProvisioningPDUError) as excinfo:
        decode(raw)
    assert excinfo.value.data == raw


def test_decode_empty_raises():
    with pytest.raises(ProvisioningPDUError):
        decode(b"")


def test_decode_bad_length_raises():
    # Confirmation must carry exactly 16 bytes
    with pytest.raises(ProvisioningPDUError):
        decode(bytes([0x05]) + bytes(15))
    # Invite must carry exactly 1 byte
    with pytest.raises(ProvisioningPDUError):
        decode(bytes.fromhex("000102"))


def test_decode_padding_bits_must_be_zero():
    # 0b01_000000 | 0x00: opcode 0x00 with non-zero 2-bit padding
    with pytest.raises(ProvisioningPDUError):
        decode(bytes.fromhex("4000"))


def test_encode_out_of_range_int_field_raises():
    with pytest.raises(ProvisioningPDUError):
        Invite(attention_duration=300).encode()
    with pytest.raises(ProvisioningPDUError):
        Failed(error_code=-1).encode()
    with pytest.raises(ProvisioningPDUError):
        Start(
            algorithm=256, public_key=0, auth_method=0, auth_action=0, auth_size=0
        ).encode()
    with pytest.raises(ProvisioningPDUError):
        Capabilities(
            num_elements=1,
            algorithms=0x10000,  # wider than 16 bits
            public_key_type=0,
            static_oob_type=0,
            output_oob_size=0,
            output_oob_actions=0,
            input_oob_size=0,
            input_oob_actions=0,
        ).encode()


def test_public_key_rejects_bad_coordinate_length():
    with pytest.raises(ProvisioningPDUError):
        PublicKey(x=bytes(31), y=bytes(32)).encode()
    with pytest.raises(ProvisioningPDUError):
        decode(bytes([0x03]) + bytes(63))
