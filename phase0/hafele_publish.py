"""Read or set where a Häfele box's lighting servers publish their status.

A server model with a publish address reports every state change there,
including the ones a wired wall switch makes on the box itself. Without one,
nothing outside the box ever hears about them.

    uv run python hafele_publish.py --connect network.connect \
        --esphome-host 192.168.222.160 --esphome-key <key> \
        --box 0x0049 --proxy F4:87:D2:4D:1C:09 --get-only

    # publish OnOff + Lightness status of element 0x0049 to 0x7FFF, TTL 5
    uv run python hafele_publish.py ... --element 0x0049 --publish 0x7FFF --ttl 5

    # undo: --publish 0x0000

The box's single GATT slot must be free (pause the Home Assistant entry).
Uses its own source address and keeps its SEQ in --state, so repeated runs
are never dropped as replays. The .connect file holds secret keys; keep it local.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from provision_and_toggle import SCAN_PROXY_S, open_gatt_session, setup_logging

from btmesh.access import (
    OP_CONFIG_MODEL_PUBLICATION_STATUS,
    config_model_publication_get,
    config_model_publication_set,
    encode_opcode,
    parse_config_model_publication_status,
)
from btmesh.bearer import EsphomeTransport, proxy_candidates_from_discoveries
from btmesh.crypto import k3
from btmesh.node import MeshNode
from btmesh.proxy_config import FILTER_ACCEPT_LIST, add_addresses, set_filter_type
from btmesh.proxy_pdu import MSG_TYPE_NETWORK_PDU, MSG_TYPE_PROXY_CONFIG
from btmesh.pump import BearerPump

logger = logging.getLogger("phase0")

# Unused by the app (provisioner 0x7FFD) and by Home Assistant (0x7FFF).
OUR_SRC = 0x7FF0
# Generic OnOff Server, Light Lightness Server.
DEFAULT_MODELS = "1000,1300"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--connect", required=True)
    p.add_argument("--esphome-host", required=True)
    p.add_argument("--esphome-key", required=True)
    p.add_argument("--box", required=True, help="node unicast, e.g. 0x0049")
    p.add_argument("--proxy", required=True, help="the box's proxy BLE address")
    p.add_argument("--element", help="element address (default: the box's primary)")
    p.add_argument("--models", default=DEFAULT_MODELS, help="hex SIG model ids")
    p.add_argument("--publish", help="publish address, e.g. 0x7FFF (0x0000 = off)")
    p.add_argument("--ttl", type=lambda v: int(v, 0), default=5)
    p.add_argument("--get-only", action="store_true")
    p.add_argument("--state", default=".hafele_publish_seq.json")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


async def find_proxy(scanner, address: str, network_id: bytes, timeout: float):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    await scanner.start()
    try:
        while loop.time() < deadline:
            for cand in proxy_candidates_from_discoveries(
                scanner.discovered_devices_and_advertisement_data
            ):
                if cand.device.address.upper() == address.upper():
                    if cand.parameter != network_id:
                        logger.info("%s advertises %s", address, cand.parameter.hex())
                    return cand
            await asyncio.sleep(1.0)
    finally:
        await scanner.stop()
    return None


async def run(args) -> int:
    doc = json.loads(Path(args.connect).read_text(encoding="utf-8"))
    netkey = bytes.fromhex(doc["netKeys"][0]["key"])
    appkey = bytes.fromhex(doc["appKeys"][0]["key"])
    box = int(args.box, 16)
    node_doc = next(n for n in doc["nodes"] if int(n["unicastAddress"], 16) == box)
    devkey = bytes.fromhex(node_doc["deviceKey"])
    element = int(args.element, 16) if args.element else box
    models = [int(m, 16) for m in args.models.split(",")]

    state = Path(args.state)
    seq = json.loads(state.read_text())["seq"] if state.exists() else 0

    transport = EsphomeTransport(args.esphome_host, args.esphome_key)
    await transport.start()
    client = None
    try:
        print(f"Scanning for {args.proxy} ...")
        cand = await find_proxy(transport.scanner(), args.proxy, k3(netkey), SCAN_PROXY_S)
        if cand is None:
            print("box not seen (is its slot still held by Home Assistant?)")
            return 1

        handler: list = []
        client, bearer = await open_gatt_session(
            transport, cand.device, provisioning=False,
            on_message=lambda mt, pl: handler and handler[0](mt, pl),
        )
        pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
        node = MeshNode(
            netkey=netkey, appkey=appkey, iv_index=0, src_addr=OUR_SRC,
            send_network_pdu=pump.put, seq=seq,
        )
        node.add_device(box, devkey)
        handler.append(
            lambda mt, pl: node.handle_network_pdu(pl)
            if mt == MSG_TYPE_NETWORK_PDU else None
        )
        pump.start()
        for message in (set_filter_type(FILTER_ACCEPT_LIST), add_addresses([OUR_SRC])):
            pump.put(node.build_proxy_config_pdu(message), msg_type=MSG_TYPE_PROXY_CONFIG)

        async def ask(payload: bytes, label: str) -> None:
            try:
                resp = await node.request(
                    box, payload, OP_CONFIG_MODEL_PUBLICATION_STATUS,
                    dev_key=True, timeout=5, retries=2,
                )
            except TimeoutError:
                print(f"  {label}: no answer")
                return
            status = parse_config_model_publication_status(
                encode_opcode(resp.opcode) + resp.params
            )
            print(f"  {label}: {status}")

        try:
            for model in models:
                await ask(config_model_publication_get(element, model), f"get {model:04X}")
                if args.get_only or args.publish is None:
                    continue
                await ask(
                    config_model_publication_set(
                        element, int(args.publish, 16), model, ttl=args.ttl
                    ),
                    f"set {model:04X} -> {args.publish}",
                )
        finally:
            state.write_text(json.dumps({"seq": node.ctx.seq + 16}))
            await pump.stop()
            await bearer.stop()
        return 0
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("disconnect failed (ignored): %s", exc)
        await transport.stop()


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
