"""Control the Häfele lamp using the app's own network keys (from .connect).

The lamp only exposes its Light Lightness / Generic OnOff server models once
the Häfele app has provisioned+activated it. So instead of fighting the vendor
activation, we ride on the app's network: read the NetKey/AppKey and the lamp's
unicast from the exported .connect file, connect our ESP proxy to that network,
and send STANDARD SIG Generic OnOff / Light Lightness Set messages — the models
are already bound to the app key.

    uv run python hafele_control.py --transport esphome \
        --esphome-host <host> --esphome-key <key> \
        --connect parnassium-1.connect --verbose

The .connect file holds secret keys — it is gitignored; keep it local.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from btmesh.access import (
    OP_GENERIC_ONOFF_STATUS,
    OP_LIGHT_LIGHTNESS_STATUS,
    generic_onoff_set,
    light_lightness_set,
    parse_generic_onoff_status,
    parse_light_lightness_status,
)
from btmesh.bearer import find_proxy_node
from btmesh.crypto import k3
from btmesh.errors import BtMeshError
from btmesh.node import MeshNode
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU

from provision_and_toggle import (
    SCAN_PROXY_S,
    BearerPump,
    EsphomeTransport,
    LocalTransport,
    Phase0Failure,
    _full_payload,
    open_gatt_session,
    print_diagnostic,
    setup_logging,
)

logger = logging.getLogger("phase0")

# A source address unused by the app's network (provisioner is 0x7FFE).
OUR_SRC = 0x7FFF


def load_connect(path: str) -> dict:
    d = json.load(open(path, encoding="utf-8"))
    netkey = bytes.fromhex(d["netKeys"][0]["key"])
    appkey = bytes.fromhex(d["appKeys"][0]["key"])
    node = d["nodes"][0]
    unicast = int(node["unicastAddress"], 16)
    return {
        "netkey": netkey,
        "appkey": appkey,
        "unicast": unicast,
        "iv_index": 0,
        "name": node.get("tos_devices", [{}])[0].get("name", "lamp"),
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transport", choices=["esphome", "local"], required=True)
    p.add_argument("--esphome-host")
    p.add_argument("--esphome-key")
    p.add_argument("--connect", default="parnassium-1.connect")
    p.add_argument("--address", help="override lamp unicast (hex, e.g. 0x000C)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


async def control(args, transport, cfg) -> None:
    unicast = int(args.address, 16) if args.address else cfg["unicast"]
    netkey, appkey = cfg["netkey"], cfg["appkey"]
    network_id = k3(netkey)

    scanner = transport.scanner()
    print(f"Scanning up to {SCAN_PROXY_S:.0f} s for the app network's proxy "
          f"(Network ID {network_id.hex()})...")
    match, candidates = await find_proxy_node(scanner, network_id, timeout=SCAN_PROXY_S)
    if match is None:
        seen = "; ".join(f"{c.device.address}:{c.parameter.hex()}" for c in candidates) or "none"
        raise Phase0Failure(
            "proxy scan",
            "no proxy advertising the app network's Network ID",
            [f"is the lamp powered and in range? 0x1828 adverts seen: {seen}"],
        )
    print(f"  proxy match: {match.device.address}")

    session_handler: list = []

    def dispatch(mt, pl):
        if session_handler:
            session_handler[0](mt, pl)

    client, bearer = await open_gatt_session(
        transport, match.device, provisioning=False, on_message=dispatch
    )
    pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
    pump.on_error = lambda exc: logger.error("network TX failed: %s", exc)

    node = MeshNode(
        netkey=netkey, appkey=appkey, iv_index=cfg["iv_index"],
        src_addr=OUR_SRC, send_network_pdu=pump.put,
    )

    def on_message(mt, pl):
        if mt == MSG_TYPE_NETWORK_PDU:
            node.handle_network_pdu(pl)

    try:
        session_handler.append(on_message)
        pump.start()
        print(f"\n=== CONTROLLING '{cfg['name']}' at 0x{unicast:04X} — WATCH IT ===")
        tid = 0
        got = False
        seq = [
            ("Generic OnOff ON ", generic_onoff_set(True, tid), OP_GENERIC_ONOFF_STATUS, parse_generic_onoff_status),
            ("Generic OnOff OFF", generic_onoff_set(False, tid + 1), OP_GENERIC_ONOFF_STATUS, parse_generic_onoff_status),
            ("Lightness 100%   ", light_lightness_set(0xFFFF, tid + 2), OP_LIGHT_LIGHTNESS_STATUS, parse_light_lightness_status),
            ("Lightness 25%    ", light_lightness_set(0x4000, tid + 3), OP_LIGHT_LIGHTNESS_STATUS, parse_light_lightness_status),
            ("Lightness OFF    ", light_lightness_set(0x0000, tid + 4), OP_LIGHT_LIGHTNESS_STATUS, parse_light_lightness_status),
        ]
        for label, payload, status_op, parser in seq:
            print(f"  {label} ({payload.hex()}) — watch now")
            try:
                resp = await node.request(unicast, payload, status_op, timeout=5, retries=1)
                got = True
                print(f"    ✓ STATUS {resp.opcode:#x}: {parser(_full_payload(resp))}")
            except TimeoutError:
                node.send_access(unicast, payload)
                print("    (no status; sent unacked too — watch the lamp)")
            await asyncio.sleep(2.5)
        print()
        if got:
            print("=== ✓✓✓ CONTROL WORKS — the lamp answered standard SIG "
                  "messages. Häfele lamp is drivable from our stack. ===")
        else:
            print("=== No status replies. Report whether the lamp physically "
                  "reacted at any step. ===")
    finally:
        await pump.stop()
        await bearer.stop()
        try:
            await client.disconnect()
        except Exception as exc:
            logger.debug("disconnect failed (ignored): %s", exc)


async def async_main(args) -> int:
    try:
        cfg = load_connect(args.connect)
    except (OSError, KeyError, ValueError) as exc:
        print(f"could not read {args.connect}: {exc}")
        return 2
    print(f"loaded network from {args.connect}: lamp '{cfg['name']}' at "
          f"0x{cfg['unicast']:04X}, Network ID {k3(cfg['netkey']).hex()}")

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
        await control(args, transport, cfg)
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


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
