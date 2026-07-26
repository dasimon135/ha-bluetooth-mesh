"""Passive mesh sniffer — decrypt a network's traffic with its keys (path B).

Given the Häfele app's NetKey (and AppKey), connect our ESP proxy to that
network and decrypt every access message that flows by. Häfele lamps are
relays (features 0x0003), so the vendor on/off message the app sends gets
relayed across the mesh and our proxy sees it. Toggle a light in the Connect
Mesh app during capture and read the exact vendor opcode + params off the
decrypted output — no guessing.

    uv run python mesh_sniff.py --transport esphome \
        --esphome-host <host> --esphome-key <key> \
        --netkey <32-hex> --appkey <32-hex> --seconds 90 --verbose

Then, in the Häfele app, turn a light ON and OFF a few times and watch the
"<< ACCESS" lines here.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from provision_and_toggle import (
    SCAN_PROXY_S,
    EsphomeTransport,
    LocalTransport,
    Phase0Failure,
    open_gatt_session,
    print_diagnostic,
    setup_logging,
)

from btmesh.bearer import find_proxy_node
from btmesh.crypto import k3
from btmesh.errors import BtMeshError
from btmesh.node import MeshNode
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU

logger = logging.getLogger("phase0")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transport", choices=["esphome", "local"], required=True)
    p.add_argument("--esphome-host")
    p.add_argument("--esphome-key")
    p.add_argument("--netkey", required=True, help="app NetKey, 32 hex chars")
    p.add_argument("--appkey", help="app AppKey, 32 hex chars (for access decrypt)")
    p.add_argument("--iv-index", default="0", help="IV Index (default 0)")
    p.add_argument("--seconds", type=float, default=90.0, help="capture window")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _describe(opcode: int) -> str:
    if opcode >= 0xC00000:  # 3-octet vendor opcode
        op_byte = opcode >> 16
        company = ((opcode & 0xFF) << 8) | ((opcode >> 8) & 0xFF)
        return f"VENDOR op-byte {op_byte:#04x} company {company:#06x}"
    return f"opcode {opcode:#x}"


async def sniff(args, transport) -> None:
    netkey = bytes.fromhex(args.netkey)
    appkey = bytes.fromhex(args.appkey) if args.appkey else None
    iv_index = int(args.iv_index, 0)
    network_id = k3(netkey)

    scanner = transport.scanner()
    print(
        f"Scanning up to {SCAN_PROXY_S:.0f} s for a proxy of the app's network "
        f"(Network ID {network_id.hex()})..."
    )
    match, candidates = await find_proxy_node(scanner, network_id, timeout=SCAN_PROXY_S)
    if match is None:
        seen = "; ".join(
            f"{c.device.address}({c.parameter.hex()})" for c in candidates
        ) or "none"
        raise Phase0Failure(
            "sniff scan",
            "no proxy advertising the app's Network ID was seen",
            [
                "the NetKey is wrong (Network ID = k3(netkey) did not match)",
                "no app-network node with GATT Proxy is in range of the ESP proxy",
                f"0x1828 adverts seen: {seen}",
            ],
        )
    print(f"  proxy match: {match.device.address}")

    # A receive-only node loaded with the app's keys; src_addr is a spare
    # unicast that won't collide (we never transmit).
    node = MeshNode(
        netkey=netkey,
        appkey=appkey or bytes(16),
        iv_index=iv_index,
        src_addr=0x7FFF,
        send_network_pdu=lambda raw: None,
    )

    seen_access: list[tuple[int, int, bytes]] = []

    def on_message(msg):
        tag = _describe(msg.opcode)
        line = (
            f"<< ACCESS  src {msg.src:#06x}  {tag}  params {msg.params.hex()}"
        )
        if msg.opcode >= 0xC00000:
            line = ">>> " + line + "  <<< VENDOR"
        print(line)
        seen_access.append((msg.src, msg.opcode, msg.params))

    node.on_message = on_message

    def handle(msg_type, payload):
        if msg_type != MSG_TYPE_NETWORK_PDU:
            return
        try:
            node.handle_network_pdu(payload)
        except BtMeshError as exc:
            logger.debug("undecodable network PDU (other key/net?): %s", exc)

    client, bearer = await open_gatt_session(
        transport, match.device, provisioning=False, on_message=handle
    )
    try:
        print()
        print(f"=== CAPTURING for {args.seconds:.0f} s — NOW toggle a light "
              "ON/OFF in the Häfele app a few times ===")
        await asyncio.sleep(args.seconds)
    finally:
        await bearer.stop()
        try:
            await client.disconnect()
        except Exception as exc:
            logger.debug("disconnect failed (ignored): %s", exc)

    print()
    vendor = [s for s in seen_access if s[1] >= 0xC00000]
    if vendor:
        print(f"=== Captured {len(vendor)} vendor access message(s). These are "
              "Häfele's real vendor commands — decode ON vs OFF from the params. ===")
    elif seen_access:
        print(f"=== Captured {len(seen_access)} access message(s) but no vendor "
              "opcode — check the AppKey, or toggle again during capture. ===")
    else:
        print("=== No access messages decrypted. Likely wrong NetKey/AppKey, or "
              "no traffic during the window. ===")


async def async_main(args) -> int:
    if len(bytes.fromhex(args.netkey)) != 16:
        print("--netkey must be 32 hex chars (16 bytes)")
        return 2
    if args.appkey and len(bytes.fromhex(args.appkey)) != 16:
        print("--appkey must be 32 hex chars (16 bytes)")
        return 2
    if args.transport == "esphome":
        if not args.esphome_host or not args.esphome_key:
            print("--transport esphome needs --esphome-host and --esphome-key")
            return 2
        transport = EsphomeTransport(args.esphome_host, args.esphome_key)
    else:
        transport = LocalTransport()

    try:
        await transport.start()
    except BtMeshError as exc:
        print_diagnostic("transport", exc, ["proxy unreachable / wrong key / no slot"])
        return 1
    try:
        await sniff(args, transport)
        return 0
    except Phase0Failure as failure:
        print_diagnostic(failure.phase, failure, failure.causes)
        return 1
    except (BtMeshError, TimeoutError, OSError) as exc:
        print_diagnostic("unclassified", exc, ["see provision_run.log for hex dumps"])
        return 1
    finally:
        try:
            await transport.stop()
        except Exception as exc:
            logger.debug("transport stop failed (ignored): %s", exc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
