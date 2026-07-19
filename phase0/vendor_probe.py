"""Vendor-model probe for the Häfele Connect Mesh lamp (Phase 0, path A).

The lamp exposes no standard lighting SIG models; control lives in Häfele
vendor models (company 0x07E9) built on the Telink SIG Mesh SDK. This script
binds the app key to the vendor model(s) and sends the Telink vendor
``VD_GROUP_G_SET`` on/off message (``C2 E9 07 <sub_op> <tid>``) so you can
watch whether the lamp physically reacts.

Run against an already-provisioned lamp (state from provision_and_toggle):

    uv run python vendor_probe.py --transport esphome \
        --esphome-host <host> --esphome-key <key> --verbose

Watch the lamp during the ON/OFF sequence and report what happens.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from btmesh_min.access import (
    HAEFELE_COMPANY_ID,
    OP_CONFIG_APPKEY_STATUS,
    OP_CONFIG_MODEL_APP_STATUS,
    STATUS_NAMES,
    VD_GROUP_G_STATUS,
    config_appkey_add,
    config_model_app_bind_vendor,
    parse_config_appkey_status,
    parse_config_model_app_status,
    vendor_group_onoff_set,
    vendor_opcode,
)
from btmesh_min.errors import BtMeshError
from btmesh_min.node import MeshNode
from btmesh_min.proxy_pdu import MSG_TYPE_NETWORK_PDU

# Reuse the provisioned-session plumbing from the main harness.
from provision_and_toggle import (
    BearerPump,
    EsphomeTransport,
    LocalTransport,
    Phase0Failure,
    _find_proxy_target,
    _full_payload,
    load_or_create_state,
    open_gatt_session,
    pick_device,
    print_diagnostic,
    save_state,
    setup_logging,
)

logger = logging.getLogger("phase0")

# Häfele's four vendor models on the primary element (from composition data).
DEFAULT_VENDOR_MODELS = [0x1000, 0x1002, 0x1006, 0x100B]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transport", choices=["esphome", "local"], required=True)
    p.add_argument("--esphome-host")
    p.add_argument("--esphome-key")
    p.add_argument("--device-uuid")
    p.add_argument("--state", default="phase0_state.json")
    p.add_argument(
        "--company", default=hex(HAEFELE_COMPANY_ID),
        help="vendor company ID (default 0x07E9 Häfele)",
    )
    p.add_argument(
        "--models", default=",".join(hex(m) for m in DEFAULT_VENDOR_MODELS),
        help="comma-separated vendor model IDs to bind",
    )
    p.add_argument("--cycles", type=int, default=2, help="ON/OFF cycles")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


async def probe(args, state, state_path, transport, unicast: int) -> None:
    company = int(args.company, 0)
    models = [int(m, 0) for m in args.models.split(",")]
    entry = state["devices"][f"0x{unicast:04x}"]
    netkey = bytes.fromhex(state["netkey"])
    appkey = bytes.fromhex(state["appkey"])
    device_key = bytes.fromhex(entry["device_key"])

    target = await _find_proxy_target(state, transport, unicast)
    print(f"Connecting Mesh Proxy to {target.address}...")

    session_handler: list = []

    def dispatch(msg_type, payload):
        if session_handler:
            session_handler[0](msg_type, payload)

    client, bearer = await open_gatt_session(
        transport, target, provisioning=False, on_message=dispatch
    )
    pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
    pump.on_error = lambda exc: logger.error("network TX failed: %s", exc)

    def send_network_pdu(raw: bytes) -> None:
        pump.put(raw)
        state["seq"] = node.ctx.seq
        save_state(state_path, state)

    node = MeshNode(
        netkey=netkey,
        appkey=appkey,
        iv_index=state["iv_index"],
        src_addr=state["provisioner_addr"],
        send_network_pdu=send_network_pdu,
        seq=state["seq"],
    )
    node.add_device(unicast, device_key)

    # Log EVERY inbound access message, not just the request()-matched one:
    # if the lamp replies with an unexpected vendor opcode/format, we must see
    # it — that reveals Häfele's actual vendor protocol.
    def log_inbound(msg):
        print(
            f"    << inbound from {msg.src:#06x}: opcode {msg.opcode:#x} "
            f"params {msg.params.hex()}"
        )

    node.on_message = log_inbound

    def on_message(msg_type, payload):
        if msg_type == MSG_TYPE_NETWORK_PDU:
            node.handle_network_pdu(payload)

    try:
        session_handler.append(on_message)
        pump.start()

        # AppKey Add (idempotent — same key re-Add is Success per §4.3.2.37).
        print("Config: AppKey Add (idempotent)...")
        resp = await node.request(
            unicast,
            config_appkey_add(state["netkey_index"], state["appkey_index"], appkey),
            OP_CONFIG_APPKEY_STATUS,
            dev_key=True,
            timeout=10,
        )
        st = parse_config_appkey_status(_full_payload(resp))
        print(f"  AppKey Status: {STATUS_NAMES.get(st.status, st.status)}")

        # Bind the app key to each vendor model so the node's vendor model
        # accepts our app-keyed vendor messages.
        bound = []
        for model_id in models:
            print(
                f"Config: Model App Bind vendor {company:#06x}:{model_id:#06x}..."
            )
            try:
                resp = await node.request(
                    unicast,
                    config_model_app_bind_vendor(
                        unicast, state["appkey_index"], company, model_id
                    ),
                    OP_CONFIG_MODEL_APP_STATUS,
                    dev_key=True,
                    timeout=10,
                )
                s = parse_config_model_app_status(_full_payload(resp))
                name = STATUS_NAMES.get(s.status, str(s.status))
                print(f"  bind {model_id:#06x}: {name}")
                if s.status == 0x00:
                    bound.append(model_id)
            except TimeoutError:
                print(f"  bind {model_id:#06x}: no reply (timeout)")
        if not bound:
            print(
                "  WARNING: no vendor model accepted the app-key bind — the "
                "on/off attempt below will likely be ignored."
            )
        state["vendor_bound_models"] = bound
        save_state(state_path, state)

        # Send the Telink vendor on/off. The opcode is model-independent at the
        # access layer; binding above authorized our app key on the vendor
        # model that registers VD_GROUP_G_SET.
        status_op = vendor_opcode(VD_GROUP_G_STATUS, company)
        print()
        print("=== VENDOR ON/OFF PROBE — WATCH THE LAMP ===")
        print(f"    opcode VD_GROUP_G_SET = C2 {company & 0xFF:02X} "
              f"{(company >> 8) & 0xFF:02X}, sub_op 1=ON/0=OFF + tid")
        tid = 0
        got_status = False
        for cycle in range(1, args.cycles + 1):
            for on in (True, False):
                label = "ON " if on else "OFF"
                payload = vendor_group_onoff_set(company, on, tid)
                print(f"  cycle {cycle}: sending VENDOR {label} "
                      f"({payload.hex()}) — watch now")
                try:
                    resp = await node.request(
                        unicast, payload, status_op, timeout=4, retries=1
                    )
                    got_status = True
                    print(f"    → VD_GROUP_G_STATUS reply: params={resp.params.hex()}")
                except TimeoutError:
                    # Fire-and-forget still may have driven the lamp; the user's
                    # eyes are the oracle. Send NoAck too in case ack form is
                    # what the firmware dislikes.
                    node.send_access(unicast, vendor_group_onoff_set(company, on, tid, ack=False))
                    print("    → no status reply (sent NoAck too; watch the lamp)")
                tid = (tid + 1) & 0xFF
                await asyncio.sleep(3.0)
        print()
        if got_status:
            print("=== GOT A VENDOR STATUS REPLY — the vendor model responds. "
                  "If the lamp also toggled, this is a full GO. ===")
        else:
            print("=== No vendor status reply. Report whether the lamp "
                  "physically reacted — that determines next steps. ===")
    finally:
        await pump.stop()
        await bearer.stop()
        try:
            await client.disconnect()
        except Exception as exc:
            logger.debug("disconnect failed (ignored): %s", exc)


async def async_main(args) -> int:
    state_path = __import__("pathlib").Path(args.state)
    try:
        state = load_or_create_state(state_path)
    except Phase0Failure as failure:
        print_diagnostic(failure.phase, failure, failure.causes)
        return 1

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
        unicast = pick_device(state, args)
        print(f"Using provisioned device 0x{unicast:04x} from {state_path}")
        await probe(args, state, state_path, transport, unicast)
        return 0
    except Phase0Failure as failure:
        print_diagnostic(failure.phase, failure, failure.causes)
        return 1
    except (BtMeshError, TimeoutError, OSError) as exc:
        print_diagnostic("unclassified", exc, ["see provision_run.log for hex dumps"])
        return 1
    finally:
        save_state(state_path, state)
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
