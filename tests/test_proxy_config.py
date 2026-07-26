"""Proxy configuration message codec tests (Mesh Profile spec §6.5).

A proxy server starts every connection with an EMPTY accept list, so it
forwards nothing to the client until the client configures the filter. These
messages are what unlock Status replies reaching us at all.
"""

import pytest

from btmesh.proxy_config import (
    FILTER_ACCEPT_LIST,
    FILTER_REJECT_LIST,
    OP_ADD_ADDRESSES,
    OP_FILTER_STATUS,
    OP_SET_FILTER_TYPE,
    FilterStatus,
    ProxyConfigError,
    add_addresses,
    parse_filter_status,
    set_filter_type,
)


def test_set_filter_type_accept_list():
    assert set_filter_type(FILTER_ACCEPT_LIST) == bytes([OP_SET_FILTER_TYPE, 0x00])


def test_set_filter_type_reject_list():
    assert set_filter_type(FILTER_REJECT_LIST) == bytes([OP_SET_FILTER_TYPE, 0x01])


def test_set_filter_type_rejects_unknown_type():
    with pytest.raises(ProxyConfigError):
        set_filter_type(0x02)


def test_add_addresses_packs_big_endian():
    assert add_addresses([0x7FFF, 0xC000]) == bytes(
        [OP_ADD_ADDRESSES, 0x7F, 0xFF, 0xC0, 0x00]
    )


def test_add_addresses_rejects_an_empty_list():
    with pytest.raises(ProxyConfigError):
        add_addresses([])


def test_add_addresses_rejects_an_out_of_range_address():
    with pytest.raises(ProxyConfigError):
        add_addresses([0x1FFFF])


def test_parse_filter_status():
    status = parse_filter_status(bytes([OP_FILTER_STATUS, 0x00, 0x00, 0x01]))
    assert status == FilterStatus(filter_type=FILTER_ACCEPT_LIST, list_size=1)


def test_parse_filter_status_rejects_another_opcode():
    with pytest.raises(ProxyConfigError):
        parse_filter_status(bytes([OP_SET_FILTER_TYPE, 0x00, 0x00, 0x01]))


def test_parse_filter_status_rejects_a_truncated_message():
    with pytest.raises(ProxyConfigError):
        parse_filter_status(bytes([OP_FILTER_STATUS, 0x00, 0x00]))
