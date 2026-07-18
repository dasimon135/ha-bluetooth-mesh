"""Provisioner state machine (Mesh Profile spec §5.4).

Transport-agnostic: the bearer layer feeds device PDUs into
:meth:`Provisioner.handle_pdu` and outgoing PDUs are emitted through the
``send`` callback.  The whole class is synchronous; asyncio (or anything
else) lives in the bearer.

Crypto chain validated end-to-end against the Mesh Profile 1.0.1 §8.7
sample provisioning session (see tests/test_provisioner.py for sources).
ConfirmationInputs = the PDU parameters *without the opcode byte* of
invite ‖ capabilities ‖ start ‖ provisioner public key ‖ device public key
(§5.4.2.1; reproduces the §8.7.2 ConfirmationSalt exactly).

Static OOB AuthValue layout (value first, zero padding appended, truncated
to the leftmost 16 bytes) follows Zephyr subsys/bluetooth/mesh/provisioner.c
``bt_mesh_auth_method_set_static`` and Nordic IOS-nRF-Mesh-Library
ProvisioningManager.swift.
"""

from __future__ import annotations

import enum
import os
from typing import Callable

from cryptography.hazmat.primitives.asymmetric import ec

from .crypto import aes_cmac, ccm_encrypt, k1, s1
from .prov_pdu import (
    Capabilities,
    Complete,
    Confirmation,
    Data,
    Failed,
    Invite,
    ProvisioningPDU,
    PublicKey,
    Random,
    Start,
    decode,
)

AUTH_VALUE_LEN = 16

# Start PDU field values (spec §5.4.1.3, Tables 5.25-5.28)
ALGORITHM_FIPS_P256 = 0x00
PUBLIC_KEY_NO_OOB = 0x00
AUTH_METHOD_NO_OOB = 0x00
AUTH_METHOD_STATIC = 0x01

# Provisioning error codes (spec §5.4.1.9 Table 5.38; Zephyr prov.h PROV_ERR_*)
ERROR_NAMES = {
    0x00: "Prohibited",
    0x01: "Invalid PDU",
    0x02: "Invalid Format",
    0x03: "Unexpected PDU",
    0x04: "Confirmation Failed",
    0x05: "Out of Resources",
    0x06: "Decryption Failed",
    0x07: "Unexpected Error",
    0x08: "Cannot Assign Addresses",
}


class ProvisioningError(Exception):
    """Provisioning cannot proceed (protocol violation, bad input, mismatch)."""


class ProvisioningFailed(ProvisioningError):
    """The device sent a Provisioning Failed PDU."""

    def __init__(self, error_code: int) -> None:
        name = ERROR_NAMES.get(error_code, "Unknown")
        super().__init__(
            f"device reported provisioning failure 0x{error_code:02x} ({name})"
        )
        self.error_code = error_code
        self.error_name = name


class State(enum.Enum):
    IDLE = enum.auto()
    WAIT_CAPABILITIES = enum.auto()
    WAIT_PUBLIC_KEY = enum.auto()
    WAIT_CONFIRMATION = enum.auto()
    WAIT_RANDOM = enum.auto()
    WAIT_COMPLETE = enum.auto()
    DONE = enum.auto()
    FAILED = enum.auto()


def static_auth_value(static_oob: bytes) -> bytes:
    """Format a static OOB value as the 16-byte AuthValue.

    Value first, zero padding appended, truncated to the leftmost 16 bytes
    (Zephyr provisioner.c, Nordic IOS-nRF-Mesh-Library).
    """
    if not static_oob:
        raise ProvisioningError("static OOB value must not be empty")
    return static_oob[:AUTH_VALUE_LEN].ljust(AUTH_VALUE_LEN, b"\x00")


def _coerce_keypair(
    keypair: ec.EllipticCurvePrivateKey | bytes | None,
) -> ec.EllipticCurvePrivateKey:
    if keypair is None:
        return ec.generate_private_key(ec.SECP256R1())
    if isinstance(keypair, ec.EllipticCurvePrivateKey):
        if not isinstance(keypair.curve, ec.SECP256R1):
            raise ProvisioningError("keypair must be on the P-256 curve")
        return keypair
    if isinstance(keypair, (bytes, bytearray)) and len(keypair) == 32:
        return ec.derive_private_key(int.from_bytes(keypair, "big"), ec.SECP256R1())
    raise ProvisioningError(
        "keypair must be a P-256 private key or its 32-byte private value"
    )


class Provisioner:
    """Drives PB-GATT provisioning. Feed device PDUs via handle_pdu();
    outgoing PDUs come back via the send callback."""

    def __init__(
        self,
        netkey: bytes,
        key_index: int,
        iv_index: int,
        unicast_addr: int,
        send: Callable[[bytes], None],
        keypair: ec.EllipticCurvePrivateKey | bytes | None = None,
        random: bytes | None = None,
        static_oob: bytes | None = None,
    ) -> None:
        if len(netkey) != 16:
            raise ProvisioningError(f"netkey must be 16 bytes, got {len(netkey)}")
        if not 0 <= key_index <= 0x0FFF:
            raise ProvisioningError(
                f"key_index must be a 12-bit value, got {key_index:#x}"
            )
        if not 0 <= iv_index <= 0xFFFFFFFF:
            raise ProvisioningError(f"iv_index must be 32-bit, got {iv_index:#x}")
        if not 0x0001 <= unicast_addr <= 0x7FFF:
            raise ProvisioningError(
                f"unicast_addr must be 0x0001-0x7fff, got {unicast_addr:#06x}"
            )
        if random is not None and len(random) != 16:
            raise ProvisioningError(f"random must be 16 bytes, got {len(random)}")

        self._netkey = bytes(netkey)
        self._key_index = key_index
        self._iv_index = iv_index
        self.unicast_addr = unicast_addr
        self._send = send
        self._key = _coerce_keypair(keypair)
        self._random = bytes(random) if random is not None else os.urandom(16)
        self._static_oob = static_oob

        self.state = State.IDLE
        self.done = False
        self.device_key: bytes | None = None
        #: Optional zero-argument callback invoked when Complete is received.
        self.on_done: Callable[[], None] | None = None

        nums = self._key.public_key().public_numbers()
        self._public_key = nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")

        # ConfirmationInputs pieces (PDU parameters without the opcode byte)
        self._invite_params = b""
        self._caps_params = b""
        self._start_params = b""
        self._auth_value = b""
        self._secret = b""
        self._conf_salt = b""
        self._conf_key = b""
        self._device_confirmation = b""

    # -- outgoing ----------------------------------------------------------

    def _emit(self, pdu: ProvisioningPDU) -> bytes:
        encoded = pdu.encode()
        self._send(encoded)
        return encoded

    def start(self) -> None:
        """Begin provisioning by sending Invite(attention_duration=0)."""
        if self.state is not State.IDLE:
            raise ProvisioningError(f"start() called in state {self.state.name}")
        invite = Invite(attention_duration=0)
        self._invite_params = self._emit(invite)[1:]
        self.state = State.WAIT_CAPABILITIES

    # -- incoming ----------------------------------------------------------

    def handle_pdu(self, data: bytes) -> None:
        """Process one Provisioning PDU received from the device."""
        pdu = decode(data)
        if isinstance(pdu, Failed):
            self.state = State.FAILED
            raise ProvisioningFailed(pdu.error_code)
        expected: dict[State, tuple[type, Callable]] = {
            State.WAIT_CAPABILITIES: (Capabilities, self._on_capabilities),
            State.WAIT_PUBLIC_KEY: (PublicKey, self._on_public_key),
            State.WAIT_CONFIRMATION: (Confirmation, self._on_confirmation),
            State.WAIT_RANDOM: (Random, self._on_random),
            State.WAIT_COMPLETE: (Complete, self._on_complete),
        }
        entry = expected.get(self.state)
        if entry is None or not isinstance(pdu, entry[0]):
            raise ProvisioningError(
                f"unexpected {type(pdu).__name__} PDU in state {self.state.name}"
            )
        entry[1](pdu)

    # -- handlers ----------------------------------------------------------

    def _on_capabilities(self, caps: Capabilities) -> None:
        if not caps.algorithms & 0x0001:
            raise ProvisioningError(
                "device does not support the FIPS P-256 Elliptic Curve algorithm; "
                f"capabilities: {caps.describe()}"
            )
        if self._static_oob is not None and caps.static_oob_type & 0x01:
            auth_method = AUTH_METHOD_STATIC
            self._auth_value = static_auth_value(self._static_oob)
        elif caps.static_oob_type & 0x01:
            # Device offers static OOB but we have no value; attempt No OOB.
            auth_method = AUTH_METHOD_NO_OOB
            self._auth_value = bytes(AUTH_VALUE_LEN)
        elif caps.output_oob_size or caps.input_oob_size:
            raise ProvisioningError(
                "device supports only output/input OOB authentication, which "
                "this provisioner cannot perform (provide static_oob or use a "
                f"No OOB capable device); capabilities: {caps.describe()}"
            )
        else:
            auth_method = AUTH_METHOD_NO_OOB
            self._auth_value = bytes(AUTH_VALUE_LEN)

        self._caps_params = caps.encode()[1:]
        start = Start(
            algorithm=ALGORITHM_FIPS_P256,
            public_key=PUBLIC_KEY_NO_OOB,
            auth_method=auth_method,
            auth_action=0,
            auth_size=0,
        )
        self._start_params = self._emit(start)[1:]
        self._emit(PublicKey(x=self._public_key[:32], y=self._public_key[32:]))
        self.state = State.WAIT_PUBLIC_KEY

    def _on_public_key(self, pk: PublicKey) -> None:
        device_public_key = pk.x + pk.y
        if device_public_key == self._public_key:
            raise ProvisioningError("device echoed the provisioner public key")
        try:
            peer = ec.EllipticCurvePublicNumbers(
                int.from_bytes(pk.x, "big"),
                int.from_bytes(pk.y, "big"),
                ec.SECP256R1(),
            ).public_key()
        except ValueError as exc:
            raise ProvisioningError(
                f"device public key is not a valid P-256 point: {exc}"
            ) from exc
        # ECDHSecret = X coordinate of the shared point (32 bytes), which is
        # exactly what exchange() returns.
        self._secret = self._key.exchange(ec.ECDH(), peer)

        inputs = (
            self._invite_params
            + self._caps_params
            + self._start_params
            + self._public_key
            + device_public_key
        )
        self._conf_salt = s1(inputs)
        self._conf_key = k1(self._secret, self._conf_salt, b"prck")
        confirmation = aes_cmac(self._conf_key, self._random + self._auth_value)
        self._emit(Confirmation(value=confirmation))
        self.state = State.WAIT_CONFIRMATION

    def _on_confirmation(self, conf: Confirmation) -> None:
        self._device_confirmation = conf.value
        self._emit(Random(value=self._random))
        self.state = State.WAIT_RANDOM

    def _on_random(self, rand: Random) -> None:
        expected = aes_cmac(self._conf_key, rand.value + self._auth_value)
        if expected != self._device_confirmation:
            raise ProvisioningError(
                "device confirmation does not match its random value "
                "(wrong AuthValue or tampered exchange): expected "
                f"{expected.hex()}, got {self._device_confirmation.hex()}"
            )
        prov_salt = s1(self._conf_salt + self._random + rand.value)
        session_key = k1(self._secret, prov_salt, b"prsk")
        session_nonce = k1(self._secret, prov_salt, b"prsn")[-13:]
        self.device_key = k1(self._secret, prov_salt, b"prdk")

        plaintext = (
            self._netkey
            + self._key_index.to_bytes(2, "big")
            + b"\x00"  # flags: key refresh off, normal IV operation
            + self._iv_index.to_bytes(4, "big")
            + self.unicast_addr.to_bytes(2, "big")
        )
        ciphertext = ccm_encrypt(session_key, session_nonce, plaintext, mic_len=8)
        self._emit(Data(encrypted=ciphertext[:25], mic=ciphertext[25:]))
        self.state = State.WAIT_COMPLETE

    def _on_complete(self, _: Complete) -> None:
        self.state = State.DONE
        self.done = True
        if self.on_done is not None:
            self.on_done()
