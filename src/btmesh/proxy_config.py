"""Proxy configuration messages (Mesh Profile spec §6.5).

A Proxy Server keeps a per-connection **address filter** deciding which network
PDUs it forwards to the client. Its initial state is an *accept list that is
empty* (§6.5.1), so a freshly opened proxy connection forwards NOTHING inbound:
every Status a node sends back is dropped by the proxy before it reaches us.
Configuring the filter — set the type, then add the addresses we want to hear
about — is what makes confirmed state possible at all.

These messages travel over the proxy connection in a Proxy PDU of type
``MSG_TYPE_PROXY_CONFIG``, carrying a network PDU with ``CTL=1``, ``TTL=0`` and
the message below as its transport PDU (see
:meth:`btmesh.node.MeshNode.build_proxy_config_pdu`).
"""

from typing import Iterable, NamedTuple

from .errors import BtMeshError

__all__ = [
    "FILTER_ACCEPT_LIST",
    "FILTER_REJECT_LIST",
    "OP_SET_FILTER_TYPE",
    "OP_ADD_ADDRESSES",
    "OP_REMOVE_ADDRESSES",
    "OP_FILTER_STATUS",
    "ProxyConfigError",
    "FilterStatus",
    "set_filter_type",
    "add_addresses",
    "parse_filter_status",
]

# Filter types (spec §6.5.1). An accept list forwards only the addresses it
# holds; a reject list forwards everything except them.
FILTER_ACCEPT_LIST = 0x00
FILTER_REJECT_LIST = 0x01

# Proxy configuration opcodes (spec §6.5.2).
OP_SET_FILTER_TYPE = 0x00
OP_ADD_ADDRESSES = 0x01
OP_REMOVE_ADDRESSES = 0x02
OP_FILTER_STATUS = 0x03


class ProxyConfigError(BtMeshError):
    """A proxy configuration message could not be built or parsed."""


class FilterStatus(NamedTuple):
    """The server's answer to every filter change (spec §6.5.2.4)."""

    filter_type: int
    list_size: int


def set_filter_type(filter_type: int) -> bytes:
    """Build a Set Filter Type message; this also CLEARS the address list."""
    if filter_type not in (FILTER_ACCEPT_LIST, FILTER_REJECT_LIST):
        raise ProxyConfigError(f"unknown filter type: {filter_type:#04x}")
    return bytes([OP_SET_FILTER_TYPE, filter_type])


def add_addresses(addresses: Iterable[int]) -> bytes:
    """Build an Add Addresses To Filter message (addresses are 16-bit, big-endian)."""
    packed = b""
    count = 0
    for address in addresses:
        if not 0 <= address <= 0xFFFF:
            raise ProxyConfigError(f"address out of range: {address:#x}")
        packed += address.to_bytes(2, "big")
        count += 1
    if count == 0:
        raise ProxyConfigError("no addresses to add")
    return bytes([OP_ADD_ADDRESSES]) + packed


def parse_filter_status(pdu: bytes) -> FilterStatus:
    """Parse a Filter Status message (spec §6.5.2.4)."""
    if len(pdu) != 4:
        raise ProxyConfigError(
            f"Filter Status must be 4 bytes, got {len(pdu)}"
        )
    if pdu[0] != OP_FILTER_STATUS:
        raise ProxyConfigError(
            f"not a Filter Status message: opcode {pdu[0]:#04x}"
        )
    return FilterStatus(
        filter_type=pdu[1], list_size=int.from_bytes(pdu[2:4], "big")
    )
