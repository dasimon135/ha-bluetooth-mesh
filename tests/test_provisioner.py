"""Tests for btmesh.provisioner against the Mesh Profile 1.0.1 §8.7 sample.

The full provisioning session sample (§8.7.1 invite/capabilities/start,
§8.7.2 confirmation values, §8.7.3 provisioning data encryption) is
reproduced from two independent public sources and cross-checked:

- Silvair python-bluetooth-mesh-network tests/test_provisioning.py
  https://github.com/SilvairGit/python-bluetooth-mesh-network/blob/master/tests/test_provisioning.py
  (public keys, ECDH secret, confirmation salt/key, randoms, confirmations,
  provisioning salt, plaintext/encrypted data + MIC, device key)
- Telink tc_ble_mesh TelinkSigMeshLibTests/CheckSampleData.m
  https://github.com/telink-semi/tc_ble_mesh/blob/master/app/ios/TelinkSigMeshLib/TelinkSigMeshLib/TelinkSigMeshLibTests/CheckSampleData.m
  (provisioner private key, ConfirmationInputs, session key/nonce, device key)

The chain was additionally re-derived end-to-end from the provisioner
private key with btmesh.crypto before these constants were committed:
every intermediate value (public key, ECDH secret, salts, keys, MIC)
reproduces the sample exactly, which also confirms the ConfirmationInputs
layout: PDU *parameters without the opcode byte* of
invite ‖ capabilities ‖ start ‖ provisioner public key ‖ device public key.

Static OOB AuthValue layout (value first, zero padding appended, truncated
to the leftmost 16 bytes) follows:
- Zephyr subsys/bluetooth/mesh/provisioner.c bt_mesh_auth_method_set_static
  https://github.com/zephyrproject-rtos/zephyr/blob/main/subsys/bluetooth/mesh/provisioner.c
- Nordic IOS-nRF-Mesh-Library Library/Provisioning/ProvisioningManager.swift
  https://github.com/NordicSemiconductor/IOS-nRF-Mesh-Library/blob/main/Library/Provisioning/ProvisioningManager.swift
No public crypto vector exists for the static OOB path, so only the
AuthValue formatting is asserted (no fabricated "expected" bytes).
"""

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from btmesh.errors import BtMeshError
from btmesh.prov_pdu import Capabilities, Complete, Failed, ProvisioningPDUError
from btmesh.provisioner import (
    Provisioner,
    ProvisioningError,
    ProvisioningFailed,
    State,
    static_auth_value,
)

# --- Mesh Profile 1.0.1 §8.7 sample values (sources in module docstring) ---

PROV_PRIVATE_KEY = bytes.fromhex(
    "06a516693c9aa31a6084545d0c5db641b48572b97203ddffb7ac73f7d0457663"
)
PROV_PUBLIC_KEY = bytes.fromhex(
    "2c31a47b5779809ef44cb5eaaf5c3e43d5f8faad4a8794cb987e9b03745c78dd"
    "919512183898dfbecd52e2408e43871fd021109117bd3ed4eaf8437743715d4f"
)
DEVICE_PUBLIC_KEY = bytes.fromhex(
    "f465e43ff23d3f1b9dc7dfc04da8758184dbc966204796eccf0d6cf5e16500cc"
    "0201d048bcbbd899eeefc424164e33c201c2b010ca6b4d43a8a155cad8ecb279"
)
RANDOM_PROVISIONER = bytes.fromhex("8b19ac31d58b124c946209b5db1021b9")
RANDOM_DEVICE = bytes.fromhex("55a2a2bca04cd32ff6f346bd0a0c1a3a")
CONFIRMATION_PROVISIONER = bytes.fromhex("b38a114dfdca1fe153bd2c1e0dc46ac2")
CONFIRMATION_DEVICE = bytes.fromhex("eeba521c196b52cc2e37aa40329f554e")
DEVICE_KEY = bytes.fromhex("0520adad5e0142aa3e325087b4ec16d8")

NET_KEY = bytes.fromhex("efb2255e6422d330088e09bb015ed707")
KEY_INDEX = 0x0567
IV_INDEX = 0x01020304
UNICAST = 0x0B0C

# On-air PDUs of the sample session (opcode byte included)
INVITE_PDU = bytes.fromhex("00" + "00")
CAPABILITIES_PDU = bytes.fromhex("01" + "0100010000000000000000")
START_PDU = bytes.fromhex("02" + "0000000000")
PROV_PUBLIC_KEY_PDU = bytes([0x03]) + PROV_PUBLIC_KEY
DEVICE_PUBLIC_KEY_PDU = bytes([0x03]) + DEVICE_PUBLIC_KEY
PROV_CONFIRMATION_PDU = bytes([0x05]) + CONFIRMATION_PROVISIONER
DEVICE_CONFIRMATION_PDU = bytes([0x05]) + CONFIRMATION_DEVICE
PROV_RANDOM_PDU = bytes([0x06]) + RANDOM_PROVISIONER
DEVICE_RANDOM_PDU = bytes([0x06]) + RANDOM_DEVICE
DATA_PDU = bytes.fromhex(
    "07" + "d0bd7f4a89a2ff6222af59a90a60ad58acfe3123356f5cec29" + "73e0ec50783b10c7"
)
COMPLETE_PDU = Complete().encode()


DEVICE_SESSION_PDUS = [
    CAPABILITIES_PDU,
    DEVICE_PUBLIC_KEY_PDU,
    DEVICE_CONFIRMATION_PDU,
    DEVICE_RANDOM_PDU,
    COMPLETE_PDU,
]


def make_provisioner(sent, **kwargs):
    return Provisioner(
        netkey=NET_KEY,
        key_index=KEY_INDEX,
        iv_index=IV_INDEX,
        unicast_addr=UNICAST,
        send=sent.append,
        keypair=PROV_PRIVATE_KEY,
        random=RANDOM_PROVISIONER,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Full §8.7 sample session replay
# ---------------------------------------------------------------------------


def test_full_spec_sample_session():
    """Replay §8.7 end-to-end; every outgoing PDU must match byte-exactly."""
    sent: list[bytes] = []
    done_calls: list[bool] = []
    p = make_provisioner(sent)
    p.on_done = lambda: done_calls.append(True)

    assert p.state is State.IDLE
    assert p.device_key is None

    p.start()
    assert sent == [INVITE_PDU]
    assert p.state is State.WAIT_CAPABILITIES

    p.handle_pdu(CAPABILITIES_PDU)
    assert sent == [INVITE_PDU, START_PDU, PROV_PUBLIC_KEY_PDU]
    assert p.state is State.WAIT_PUBLIC_KEY

    p.handle_pdu(DEVICE_PUBLIC_KEY_PDU)
    assert sent[-1] == PROV_CONFIRMATION_PDU
    assert p.state is State.WAIT_CONFIRMATION

    p.handle_pdu(DEVICE_CONFIRMATION_PDU)
    assert sent[-1] == PROV_RANDOM_PDU
    assert p.state is State.WAIT_RANDOM

    p.handle_pdu(DEVICE_RANDOM_PDU)
    assert sent[-1] == DATA_PDU
    assert p.state is State.WAIT_COMPLETE
    assert not p.done

    p.handle_pdu(COMPLETE_PDU)
    assert p.state is State.DONE
    assert p.done
    assert done_calls == [True]

    assert sent == [
        INVITE_PDU,
        START_PDU,
        PROV_PUBLIC_KEY_PDU,
        PROV_CONFIRMATION_PDU,
        PROV_RANDOM_PDU,
        DATA_PDU,
    ]
    assert p.device_key == DEVICE_KEY
    assert p.unicast_addr == UNICAST


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_out_of_order_pdu_raises_with_state_and_type():
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    # Device public key while still waiting for Capabilities
    with pytest.raises(ProvisioningError) as excinfo:
        p.handle_pdu(DEVICE_PUBLIC_KEY_PDU)
    assert "PublicKey" in str(excinfo.value)
    assert "WAIT_CAPABILITIES" in str(excinfo.value)
    assert p.state is State.FAILED


def test_pdu_before_start_raises():
    p = make_provisioner([])
    with pytest.raises(ProvisioningError) as excinfo:
        p.handle_pdu(CAPABILITIES_PDU)
    assert "IDLE" in str(excinfo.value)


def test_failed_pdu_raises_with_error_name():
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    p.handle_pdu(CAPABILITIES_PDU)
    with pytest.raises(ProvisioningFailed) as excinfo:
        p.handle_pdu(Failed(error_code=0x04).encode())
    assert "Confirmation Failed" in str(excinfo.value)
    assert excinfo.value.error_code == 0x04
    assert p.state is State.FAILED


def test_device_confirmation_mismatch_raises():
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    p.handle_pdu(CAPABILITIES_PDU)
    p.handle_pdu(DEVICE_PUBLIC_KEY_PDU)
    corrupted = bytearray(CONFIRMATION_DEVICE)
    corrupted[0] ^= 0xFF
    p.handle_pdu(bytes([0x05]) + bytes(corrupted))
    with pytest.raises(ProvisioningError) as excinfo:
        p.handle_pdu(DEVICE_RANDOM_PDU)
    assert "confirmation" in str(excinfo.value).lower()
    # Data must NOT have been sent
    assert sent[-1] == PROV_RANDOM_PDU
    # §5.4.4: a mismatch aborts the session; the failure is terminal and a
    # retransmitted (correct) Random must not be re-verified.
    assert p.state is State.FAILED
    with pytest.raises(ProvisioningError):
        p.handle_pdu(DEVICE_RANDOM_PDU)
    assert sent[-1] == PROV_RANDOM_PDU
    assert p.device_key is None


def test_malformed_pdu_is_fatal_and_shares_error_base():
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    with pytest.raises(ProvisioningPDUError):
        p.handle_pdu(bytes.fromhex("2aff"))  # unknown opcode
    assert p.state is State.FAILED
    # Bearer layers can catch every provisioning-related error via one base.
    assert issubclass(ProvisioningPDUError, BtMeshError)
    assert issubclass(ProvisioningError, BtMeshError)
    assert issubclass(ProvisioningFailed, ProvisioningError)


def test_handle_pdu_after_done_raises_but_state_stays_done():
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    for pdu in DEVICE_SESSION_PDUS:
        p.handle_pdu(pdu)
    assert p.state is State.DONE
    with pytest.raises(ProvisioningError):
        p.handle_pdu(COMPLETE_PDU)
    assert p.state is State.DONE
    assert p.done
    assert p.device_key == DEVICE_KEY


def test_output_input_oob_only_capabilities_falls_back_to_no_oob():
    """No OOB (0x00) is always selectable by the provisioner (§5.4.2.2).

    A device advertising only output/input OOB (the Häfele/Telink lamps do
    exactly this: Blink + Push) must not abort the session — we select
    No OOB and let the device answer Provisioning Failed if it insists.
    """
    caps = Capabilities(
        num_elements=1,
        algorithms=0x0001,
        public_key_type=0x00,
        static_oob_type=0x00,  # no static OOB
        output_oob_size=4,
        output_oob_actions=0x0008,  # Output Numeric
        input_oob_size=0,
        input_oob_actions=0x0000,
    )
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    p.handle_pdu(caps.encode())
    assert p.state is State.WAIT_PUBLIC_KEY
    start_pdu = sent[1]  # Invite, Start, PublicKey
    assert start_pdu[0] == 0x02  # Start opcode
    assert start_pdu[3] == 0x00  # auth_method = No OOB
    assert start_pdu[4] == 0 and start_pdu[5] == 0  # auth_action/size zero


def test_missing_fips_p256_algorithm_raises_diagnostic():
    caps = Capabilities(
        num_elements=1,
        algorithms=0x0002,  # only BTM_ECDH_P256_HMAC_SHA256_AES_CCM (mesh 1.1)
        public_key_type=0x00,
        static_oob_type=0x00,
        output_oob_size=0,
        output_oob_actions=0,
        input_oob_size=0,
        input_oob_actions=0,
    )
    p = make_provisioner([])
    p.start()
    with pytest.raises(ProvisioningError) as excinfo:
        p.handle_pdu(caps.encode())
    assert "FIPS P-256" in str(excinfo.value)


def test_invalid_device_public_key_raises():
    p = make_provisioner([])
    p.start()
    p.handle_pdu(CAPABILITIES_PDU)
    # (0, 0) is not a point on P-256
    with pytest.raises(ProvisioningError):
        p.handle_pdu(bytes([0x03]) + bytes(64))


# ---------------------------------------------------------------------------
# Static OOB
# ---------------------------------------------------------------------------

STATIC_CAPS_PDU = Capabilities(
    num_elements=1,
    algorithms=0x0001,
    public_key_type=0x00,
    static_oob_type=0x01,  # static OOB available
    output_oob_size=0,
    output_oob_actions=0,
    input_oob_size=0,
    input_oob_actions=0,
).encode()


def test_static_oob_selects_static_auth_method():
    sent: list[bytes] = []
    p = make_provisioner(sent, static_oob=bytes(range(16)))
    p.start()
    p.handle_pdu(STATIC_CAPS_PDU)
    # Start: FIPS P-256, no OOB public key, Static OOB method, action 0, size 0
    assert sent[1] == bytes.fromhex("02" + "0000010000")
    assert p.state is State.WAIT_PUBLIC_KEY


def test_static_oob_not_provided_falls_back_to_no_oob():
    sent: list[bytes] = []
    p = make_provisioner(sent)
    p.start()
    p.handle_pdu(STATIC_CAPS_PDU)
    assert sent[1] == START_PDU  # No OOB


def test_static_auth_value_formatting():
    """AuthValue layout only (no public crypto vector for static OOB).

    Value first, zero padding appended, truncated to the leftmost 16 bytes
    (Zephyr provisioner.c, Nordic IOS-nRF-Mesh-Library — see module docstring).
    """
    assert static_auth_value(bytes(range(16))) == bytes(range(16))
    assert static_auth_value(b"abc") == b"abc" + bytes(13)
    assert static_auth_value(bytes(range(20))) == bytes(range(16))
    with pytest.raises(ProvisioningError):
        static_auth_value(b"")


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_init_rejects_bad_arguments():
    with pytest.raises(ProvisioningError):
        Provisioner(
            netkey=bytes(15),
            key_index=0,
            iv_index=0,
            unicast_addr=1,
            send=lambda _: None,
        )
    with pytest.raises(ProvisioningError):
        Provisioner(
            netkey=bytes(16),
            key_index=0x1000,  # key index is 12 bits
            iv_index=0,
            unicast_addr=1,
            send=lambda _: None,
        )
    with pytest.raises(ProvisioningError):
        Provisioner(
            netkey=bytes(16),
            key_index=0,
            iv_index=0,
            unicast_addr=0x8000,  # not a unicast address
            send=lambda _: None,
        )
    with pytest.raises(ProvisioningError):
        Provisioner(
            netkey=bytes(16),
            key_index=0,
            iv_index=0,
            unicast_addr=0,  # unassigned address
            send=lambda _: None,
        )
    with pytest.raises(ProvisioningError):
        make_provisioner([], static_oob=b"")  # empty static OOB
    with pytest.raises(ProvisioningError):
        make_provisioner([], static_oob=bytes(17))  # longer than AuthValue


def test_keypair_on_wrong_curve_rejected():
    with pytest.raises(ProvisioningError) as excinfo:
        Provisioner(
            netkey=NET_KEY,
            key_index=KEY_INDEX,
            iv_index=IV_INDEX,
            unicast_addr=UNICAST,
            send=lambda _: None,
            keypair=ec.generate_private_key(ec.SECP384R1()),
        )
    assert "P-256" in str(excinfo.value)
