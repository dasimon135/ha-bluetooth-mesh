"""Provisioning PDU codec (Mesh Profile spec §5.4.1).

Wire format: first byte = Padding (2 high bits, must be 0) | Type (6 low
bits), followed by type-specific parameters.  Multi-byte integer fields are
big-endian.  Layouts cross-checked against Zephyr subsys/bluetooth/mesh/prov.h
and Silvair python-bluetooth-mesh-network tests/test_provisioning.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


class ProvisioningPDUError(Exception):
    """Malformed or unknown Provisioning PDU.  Carries the raw bytes."""

    def __init__(self, message: str, data: bytes = b"") -> None:
        super().__init__(message)
        self.data = data


# Provisioning PDU types (spec §5.4.1, Table 5.18).
INVITE = 0x00
CAPABILITIES = 0x01
START = 0x02
PUBLIC_KEY = 0x03
CONFIRMATION = 0x05
RANDOM = 0x06
DATA = 0x07
COMPLETE = 0x08
FAILED = 0x09

# Capability bit names (spec §5.4.1.2, Tables 5.19-5.24).
_ALGORITHM_BITS = {
    0: "FIPS P-256 Elliptic Curve",
    1: "BTM_ECDH_P256_HMAC_SHA256_AES_CCM",  # Mesh Protocol 1.1
}
_OUTPUT_OOB_BITS = {
    0: "Blink",
    1: "Beep",
    2: "Vibrate",
    3: "Output Numeric",
    4: "Output Alphanumeric",
}
_INPUT_OOB_BITS = {
    0: "Push",
    1: "Twist",
    2: "Input Numeric",
    3: "Input Alphanumeric",
}


def _bit_names(value: int, names: dict[int, str]) -> str:
    if value == 0:
        return "none"
    parts = [name for bit, name in names.items() if value & (1 << bit)]
    unknown = value & ~sum(1 << bit for bit in names)
    if unknown:
        parts.append(f"unknown bits 0x{unknown:04x}")
    return ", ".join(parts)


@dataclass(frozen=True)
class Invite:
    """Provisioning Invite (spec §5.4.1.1)."""

    attention_duration: int

    def encode(self) -> bytes:
        return bytes([INVITE, self.attention_duration])


@dataclass(frozen=True)
class Capabilities:
    """Provisioning Capabilities (spec §5.4.1.2)."""

    num_elements: int
    algorithms: int
    public_key_type: int
    static_oob_type: int
    output_oob_size: int
    output_oob_actions: int
    input_oob_size: int
    input_oob_actions: int

    def encode(self) -> bytes:
        return bytes([CAPABILITIES]) + struct.pack(
            ">BHBBBHBH",
            self.num_elements,
            self.algorithms,
            self.public_key_type,
            self.static_oob_type,
            self.output_oob_size,
            self.output_oob_actions,
            self.input_oob_size,
            self.input_oob_actions,
        )

    def describe(self) -> str:
        """Human-readable capability summary (go/no-go diagnostics)."""
        plural = "element" if self.num_elements == 1 else "elements"
        lines = [
            f"{self.num_elements} {plural}",
            f"algorithms: {_bit_names(self.algorithms, _ALGORITHM_BITS)}",
            "OOB public key: "
            + ("available" if self.public_key_type & 0x01 else "not available"),
            "static OOB: "
            + ("available" if self.static_oob_type & 0x01 else "not available"),
            f"output OOB: size {self.output_oob_size}, "
            f"actions: {_bit_names(self.output_oob_actions, _OUTPUT_OOB_BITS)}",
            f"input OOB: size {self.input_oob_size}, "
            f"actions: {_bit_names(self.input_oob_actions, _INPUT_OOB_BITS)}",
        ]
        return "; ".join(lines)

    def __repr__(self) -> str:
        return f"Capabilities({self.describe()})"


@dataclass(frozen=True)
class Start:
    """Provisioning Start (spec §5.4.1.3)."""

    algorithm: int
    public_key: int
    auth_method: int
    auth_action: int
    auth_size: int

    def encode(self) -> bytes:
        return bytes(
            [
                START,
                self.algorithm,
                self.public_key,
                self.auth_method,
                self.auth_action,
                self.auth_size,
            ]
        )


@dataclass(frozen=True)
class PublicKey:
    """Provisioning Public Key: P-256 point X || Y (spec §5.4.1.4)."""

    x: bytes
    y: bytes

    def encode(self) -> bytes:
        if len(self.x) != 32 or len(self.y) != 32:
            raise ProvisioningPDUError(
                f"public key coordinates must be 32 bytes each, "
                f"got {len(self.x)}/{len(self.y)}"
            )
        return bytes([PUBLIC_KEY]) + self.x + self.y


@dataclass(frozen=True)
class Confirmation:
    """Provisioning Confirmation: 16-byte value (spec §5.4.1.5)."""

    value: bytes

    def encode(self) -> bytes:
        if len(self.value) != 16:
            raise ProvisioningPDUError(
                f"confirmation must be 16 bytes, got {len(self.value)}"
            )
        return bytes([CONFIRMATION]) + self.value


@dataclass(frozen=True)
class Random:
    """Provisioning Random: 16-byte value (spec §5.4.1.6)."""

    value: bytes

    def encode(self) -> bytes:
        if len(self.value) != 16:
            raise ProvisioningPDUError(
                f"random must be 16 bytes, got {len(self.value)}"
            )
        return bytes([RANDOM]) + self.value


@dataclass(frozen=True)
class Data:
    """Provisioning Data: 25-byte ciphertext + 8-byte MIC (spec §5.4.1.7)."""

    encrypted: bytes
    mic: bytes

    def encode(self) -> bytes:
        if len(self.encrypted) != 25 or len(self.mic) != 8:
            raise ProvisioningPDUError(
                f"data must be 25+8 bytes, got {len(self.encrypted)}+{len(self.mic)}"
            )
        return bytes([DATA]) + self.encrypted + self.mic


@dataclass(frozen=True)
class Complete:
    """Provisioning Complete: no parameters (spec §5.4.1.8)."""

    def encode(self) -> bytes:
        return bytes([COMPLETE])


@dataclass(frozen=True)
class Failed:
    """Provisioning Failed (spec §5.4.1.9)."""

    error_code: int

    def encode(self) -> bytes:
        return bytes([FAILED, self.error_code])


ProvisioningPDU = (
    Invite
    | Capabilities
    | Start
    | PublicKey
    | Confirmation
    | Random
    | Data
    | Complete
    | Failed
)

# opcode -> (dataclass, expected parameter length)
_LAYOUT: dict[int, tuple[type, int]] = {
    INVITE: (Invite, 1),
    CAPABILITIES: (Capabilities, 11),
    START: (Start, 5),
    PUBLIC_KEY: (PublicKey, 64),
    CONFIRMATION: (Confirmation, 16),
    RANDOM: (Random, 16),
    DATA: (Data, 33),
    COMPLETE: (Complete, 0),
    FAILED: (Failed, 1),
}


def decode(data: bytes) -> ProvisioningPDU:
    """Decode a Provisioning PDU, dispatching on the opcode in the first byte."""
    if not data:
        raise ProvisioningPDUError("empty provisioning PDU", data)
    if data[0] & 0xC0:
        raise ProvisioningPDUError(
            f"non-zero padding bits in first byte 0x{data[0]:02x}", data
        )
    opcode = data[0] & 0x3F
    if opcode not in _LAYOUT:
        raise ProvisioningPDUError(f"unknown provisioning PDU type 0x{opcode:02x}", data)
    cls, expected_len = _LAYOUT[opcode]
    params = data[1:]
    if len(params) != expected_len:
        raise ProvisioningPDUError(
            f"{cls.__name__} expects {expected_len} parameter bytes, "
            f"got {len(params)}",
            data,
        )
    if cls is Invite:
        return Invite(attention_duration=params[0])
    if cls is Capabilities:
        fields = struct.unpack(">BHBBBHBH", params)
        return Capabilities(*fields)
    if cls is Start:
        return Start(*params)
    if cls is PublicKey:
        return PublicKey(x=params[:32], y=params[32:])
    if cls is Confirmation:
        return Confirmation(value=params)
    if cls is Random:
        return Random(value=params)
    if cls is Data:
        return Data(encrypted=params[:25], mic=params[25:])
    if cls is Complete:
        return Complete()
    return Failed(error_code=params[0])
