"""Phase 0 feasibility harness: provision one BLE Mesh node, then toggle it.

Milestones (the go/no-go evidence):
  A — provisioning completes over PB-GATT (device key derived, netkey placed)
  B — Generic OnOff round-trips through the GATT proxy (GO)

Every frame on the air is hex-dumped at DEBUG level into provision_run.log
(and to the console with --verbose). State (keys, addresses, SEQ) persists in
a JSON file; SEQ is saved after every network PDU send because a replayed SEQ
after a crash would be silently ignored by the node.

Known Phase 0 limitations: strict IV Index match (no IV-update recovery), no
segment-ack TX (peers retransmit segmented responses — harmless for the short
statuses used here), no replay cache.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time
from pathlib import Path

from btmesh_min.access import (
    OP_CONFIG_APPKEY_STATUS,
    OP_CONFIG_MODEL_APP_STATUS,
    OP_GENERIC_ONOFF_STATUS,
    STATUS_NAMES,
    config_appkey_add,
    config_model_app_bind,
    encode_opcode,
    generic_onoff_set,
    parse_config_appkey_status,
    parse_config_model_app_status,
    parse_generic_onoff_status,
)
from btmesh_min.bearer import (
    IDENTIFICATION_NODE_IDENTITY,
    BearerError,
    EsphomeTransport,
    GattBearer,
    connect_local,
    find_proxy_node,
    scan_unprovisioned,
)
from btmesh_min.crypto import k3
from btmesh_min.errors import BtMeshError
from btmesh_min.node import MeshNode, ReceivedMessage
from btmesh_min.provisioner import Provisioner
from btmesh_min.proxy_pdu import MSG_TYPE_NETWORK_PDU, MSG_TYPE_PROVISIONING_PDU

logger = logging.getLogger("phase0")

LOG_FILE = "provision_run.log"
GENERIC_ONOFF_SERVER_MODEL = 0x1000
SCAN_UNPROVISIONED_S = 10.0
SCAN_PROXY_S = 15.0
PROVISIONING_TIMEOUT_S = 60.0

LIMITATIONS_NOTE = (
    "Known Phase 0 limitations: strict IV Index match (no IV-update recovery); "
    "no segment-ack TX, so segmented RESPONSES from the node are retransmitted "
    "by the peer (harmless for our short statuses); no replay cache."
)

CONFIG_TIMEOUT_CAUSES = [
    "wrong device key or unicast address (state file out of sync with the node "
    "— factory-reset the node and delete the state file to start over)",
    "IV Index mismatch (strict match required in Phase 0)",
    "SEQ replay: if the state file was deleted or rolled back, the node "
    "silently drops our PDUs — bump \"seq\" in the state file well past any "
    "previously used value",
    "proxy output filter dropped the response (whitelist should auto-include "
    "our source address, but a vendor stack may differ)",
    "a segmented response was lost (we never ack segments; the node retries, "
    "but long responses can still time out)",
]


class Phase0Failure(Exception):
    """A phase failed with a known diagnosis; carries the triage causes."""

    def __init__(self, phase: str, message: str, causes: list[str]) -> None:
        super().__init__(message)
        self.phase = phase
        self.causes = causes


# ----------------------------------------------------------------- plumbing


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision a BLE Mesh node over PB-GATT and toggle its "
        "Generic OnOff state through the GATT proxy (Phase 0 feasibility run).",
    )
    parser.add_argument(
        "--transport", choices=("esphome", "local"), required=True,
        help="BLE path: 'esphome' via an ESPHome Bluetooth proxy, 'local' via "
        "the PC's own adapter",
    )
    parser.add_argument("--esphome-host", help="ESPHome proxy hostname/IP")
    parser.add_argument("--esphome-key", help="ESPHome API encryption (noise) PSK")
    parser.add_argument(
        "--device-uuid",
        help="target Device UUID (32 hex chars); default: first 0x1827 beacon",
    )
    parser.add_argument(
        "--static-oob", metavar="HEX",
        help="static OOB authentication value (1-16 bytes, hex)",
    )
    parser.add_argument(
        "--state", default="phase0_state.json",
        help="state file: keys, addresses, SEQ (default: %(default)s)",
    )
    parser.add_argument(
        "--toggle-only", action="store_true",
        help="skip provisioning and config; use the device from the state file",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="DEBUG logging (frame hex dumps) on the console as well",
    )
    args = parser.parse_args(argv)

    if args.transport == "esphome" and not args.esphome_host:
        parser.error("--esphome-host is required with --transport esphome")
    if args.device_uuid:
        args.device_uuid = args.device_uuid.replace("-", "").lower()
        try:
            if len(bytes.fromhex(args.device_uuid)) != 16:
                raise ValueError
        except ValueError:
            parser.error("--device-uuid must be 16 bytes of hex")
    if args.static_oob is not None:  # explicit "" must error, not mean no-OOB
        try:
            if not 1 <= len(bytes.fromhex(args.static_oob)) <= 16:
                raise ValueError
        except ValueError:
            parser.error("--static-oob must be 1-16 bytes of hex")
    return args


def setup_logging(verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname).1s %(name)s: %(message)s"))
    logfile = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    logfile.setLevel(logging.DEBUG)
    logfile.setFormatter(
        logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s")
    )
    root.addHandler(console)
    root.addHandler(logfile)
    # Third-party DEBUG floods the interop log; keep it to the essentials.
    for noisy in ("aioesphomeapi", "habluetooth", "bleak", "bleak_esphome"):
        logging.getLogger(noisy).setLevel(logging.INFO if verbose else logging.WARNING)
    logger.info("---- provision_and_toggle run started ----")


_STATE_FIELD_TYPES = {
    "netkey": str,
    "appkey": str,
    "iv_index": int,
    "netkey_index": int,
    "appkey_index": int,
    "provisioner_addr": int,
    "next_unicast": int,
    "seq": int,
    "devices": dict,
}


def _validate_state(state: dict) -> None:
    for field, expected in _STATE_FIELD_TYPES.items():
        if not isinstance(state.get(field), expected):
            raise ValueError(
                f"field {field!r} is missing or not a {expected.__name__}"
            )
    for key_field in ("netkey", "appkey"):
        if len(bytes.fromhex(state[key_field])) != 16:  # ValueError on bad hex
            raise ValueError(f"field {key_field!r} is not 16 bytes of hex")


def load_or_create_state(path: Path) -> dict:
    if path.exists():
        # The triage texts invite hand-editing this file, so a typo must come
        # back as a diagnostic, not a raw traceback.
        try:
            state = json.loads(path.read_text())
            _validate_state(state)
        except (ValueError, TypeError) as exc:
            raise Phase0Failure(
                "startup",
                f"state file {path} is unreadable: {exc}",
                [
                    "fix the field named in the error (the file is hand-editable "
                    "JSON), or",
                    "delete the file to start over with fresh keys — the node "
                    "must then be factory-reset and reprovisioned",
                ],
            ) from exc
        logger.info(
            "loaded state %s (seq=%d, %d device(s))",
            path, state["seq"], len(state["devices"]),
        )
        return state
    state = {
        "netkey": secrets.token_bytes(16).hex(),
        "appkey": secrets.token_bytes(16).hex(),
        "iv_index": 0,
        "netkey_index": 0,
        "appkey_index": 0,
        "provisioner_addr": 0x0001,
        "next_unicast": 0x0002,
        "seq": 0,
        "devices": {},  # {"0x0002": {"device_key", "uuid", "address"}}
    }
    save_state(path, state)
    logger.info("created fresh state %s (new netkey/appkey)", path)
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def print_diagnostic(phase: str, exc: BaseException, causes: list[str]) -> None:
    print()
    print("=" * 64)
    print(f"DIAGNOSTIC — phase failed: {phase}")
    print(f"Error: {exc}")
    if causes:
        print("Likely causes / next steps:")
        for cause in causes:
            print(f"  - {cause}")
    print(LIMITATIONS_NOTE)
    print(f"Frame-level hex dumps: {LOG_FILE}")
    print("=" * 64)
    logger.error("phase %r failed: %s", phase, exc)


def _full_payload(msg: ReceivedMessage) -> bytes:
    """Rebuild the on-air access payload for the access.parse_* helpers."""
    return encode_opcode(msg.opcode) + msg.params


class LocalTransport:
    """PC adapter via plain bleak, mirroring EsphomeTransport's surface."""

    async def start(self) -> None:
        pass

    def scanner(self):
        from bleak import BleakScanner

        return BleakScanner()

    async def connect(self, address_or_device, timeout: float = 30.0):
        return await connect_local(address_or_device, timeout)

    async def stop(self) -> None:
        pass


class BearerPump:
    """Ordered bridge from sync producers to the async bearer.

    The provisioner and MeshNode emit PDUs from synchronous callbacks; the
    bearer's send() is async. An asyncio.Queue drained by a single task
    preserves emission order (and serializes send(): concurrent sends would
    interleave SAR frames). A send failure kills the pump: it is recorded in
    ``failure``, passed to ``on_error``, and nothing further is transmitted.
    """

    def __init__(self, bearer: GattBearer, msg_type: int) -> None:
        self._bearer = bearer
        self._msg_type = msg_type
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.failure: BaseException | None = None
        self.on_error = None  # Callable[[BaseException], None]

    def put(self, pdu: bytes) -> None:
        self._queue.put_nowait(pdu)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                pdu = await self._queue.get()
                await self._bearer.send(self._msg_type, pdu)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.failure = exc
            logger.error("bearer TX pump died: %s", exc)
            if self.on_error is not None:
                self.on_error(exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def _timeout_causes(pump: BearerPump, extra: list[str] | None = None) -> list[str]:
    """Triage list for a request timeout, truthful about a dead TX pump.

    If the pump died, nothing was actually transmitted — that fact must come
    FIRST, before the on-air explanations, or the diagnostic lies.
    """
    causes = list(CONFIG_TIMEOUT_CAUSES) + (extra or [])
    if pump.failure is not None:
        causes.insert(
            0,
            f"bearer TX pump died: {pump.failure} — the request was never "
            "transmitted; the causes below do not apply",
        )
    return causes


async def _disconnect_quietly(client) -> None:
    try:
        await client.disconnect()
    except Exception as exc:
        logger.debug("disconnect failed (ignored): %s", exc)


GATT_SESSION_ATTEMPTS = 3


async def open_gatt_session(
    transport,
    device,
    *,
    provisioning: bool,
    on_message,
    attempts: int = GATT_SESSION_ATTEMPTS,
) -> tuple[object, GattBearer]:
    """Connect and subscribe, retrying the whole cycle on failure.

    Mesh nodes (Telink firmwares especially) routinely drop the link right
    after the first connect; a fresh connect+subscribe cycle is the reliable
    recovery, on top of establish_connection's own per-connect retries.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            logger.info("retrying GATT session (%d/%d)...", attempt, attempts)
            await asyncio.sleep(2.0)
        try:
            client = await transport.connect(device)
        except BearerError as exc:
            last = exc
            logger.warning("connect attempt %d/%d failed: %s", attempt, attempts, exc)
            continue
        bearer = GattBearer(client, provisioning=provisioning)
        try:
            await bearer.start(on_message)
        except BearerError as exc:
            last = exc
            logger.warning(
                "subscribe attempt %d/%d failed: %s", attempt, attempts, exc
            )
            await _disconnect_quietly(client)
            continue
        return client, bearer
    raise BearerError(
        f"could not establish a GATT session after {attempts} attempts: {last}"
    )


# ------------------------------------------------------- provisioning phase


def _provisioning_causes(message: str) -> list[str]:
    causes = []
    if "OOB" in message:
        causes += [
            "the device demands OOB authentication this provisioner cannot do "
            "(output/input OOB) — see the 'capabilities:' line logged above",
            "if it lists static OOB, rerun with --static-oob HEX (value from "
            "the vendor app/label)",
        ]
    if "confirmation does not match" in message:
        causes += [
            "wrong --static-oob value (AuthValue mismatch)",
            "device expected a different auth method",
        ]
    causes += [
        "the Provisioner instance is dead after any error — simply rerun the "
        "script for a fresh session",
    ]
    return causes


async def provision(args, state: dict, state_path: Path, transport) -> int:
    """Scan, connect PB-GATT, drive the provisioning handshake. Milestone A."""
    netkey = bytes.fromhex(state["netkey"])
    scanner = transport.scanner()
    print(f"Scanning {SCAN_UNPROVISIONED_S:.0f} s for unprovisioned devices (0x1827)...")
    # Connect as soon as the (matching) beacon shows up: Telink lamps only
    # accept connections in a short window after power-up.
    beacons = await scan_unprovisioned(
        scanner,
        timeout=SCAN_UNPROVISIONED_S,
        stop_on=bytes.fromhex(args.device_uuid) if args.device_uuid else None,
        stop_on_any=not args.device_uuid,
    )
    for b in beacons:
        print(
            f"  0x1827 beacon {b.device.address}: "
            f"Device UUID {b.uuid.hex()}, OOB info 0x{b.oob_info:04x}"
        )
    if not beacons:
        raise Phase0Failure(
            "provisioning scan",
            "no 0x1827 unprovisioned-device beacon seen",
            [
                "device is already provisioned — factory-reset it to restore "
                "the beacon (vendor app or power-cycle sequence)",
                "device supports only PB-ADV, not PB-GATT (out of Phase 0 scope)",
                "device unpowered or out of radio range of the proxy/adapter",
            ],
        )
    if args.device_uuid:
        want = bytes.fromhex(args.device_uuid)
        matching = [b for b in beacons if b.uuid == want]
        if not matching:
            raise Phase0Failure(
                "provisioning scan",
                f"no beacon with Device UUID {args.device_uuid}",
                ["check the UUID against the beacon list above"],
            )
        target = matching[0]
    else:
        target = beacons[0]
        if len(beacons) > 1:
            print(
                f"  multiple beacons; picking {target.uuid.hex()} "
                "(use --device-uuid to override)"
            )

    print(f"Connecting PB-GATT to {target.device.address}...")
    # The real handler is installed once the session objects exist below; the
    # lamp sends nothing before our Provisioning Invite, so nothing is lost.
    session_handler: list = []

    def dispatch(msg_type: int, payload: bytes) -> None:
        if session_handler:
            session_handler[0](msg_type, payload)
        else:
            logger.debug("PDU before session ready: 0x%02x %s", msg_type, payload.hex())

    client, bearer = await open_gatt_session(
        transport, target.device, provisioning=True, on_message=dispatch
    )
    pump = BearerPump(bearer, MSG_TYPE_PROVISIONING_PDU)

    unicast = state["next_unicast"]
    static_oob = bytes.fromhex(args.static_oob) if args.static_oob else None
    prov = Provisioner(
        netkey,
        state["netkey_index"],
        state["iv_index"],
        unicast,
        send=pump.put,
        static_oob=static_oob,
    )

    done = asyncio.Event()
    errors: list[BaseException] = []

    def fail(exc: BaseException) -> None:
        errors.append(exc)
        done.set()

    prov.on_done = done.set
    pump.on_error = fail

    def on_message(msg_type: int, payload: bytes) -> None:
        if msg_type != MSG_TYPE_PROVISIONING_PDU:
            logger.debug("ignoring message type 0x%02x during provisioning", msg_type)
            return
        try:
            prov.handle_pdu(payload)
        except BaseException as exc:  # session is dead; surface to the waiter
            fail(exc)

    try:
        session_handler.append(on_message)
        pump.start()
        prov.start()
        try:
            await asyncio.wait_for(done.wait(), timeout=PROVISIONING_TIMEOUT_S)
        except TimeoutError:
            raise Phase0Failure(
                "provisioning",
                f"no progress for {PROVISIONING_TIMEOUT_S:.0f} s "
                f"(state {prov.state.name})",
                [
                    "device stopped responding mid-handshake (link loss?)",
                    "device stack rejects our PDUs silently — check the hex "
                    "dumps for what was last sent/received",
                ],
            ) from None
        if errors:
            exc = errors[0]
            if isinstance(exc, BtMeshError):
                raise Phase0Failure(
                    "provisioning",
                    f"{exc} (state {prov.state.name})",
                    _provisioning_causes(str(exc)),
                ) from exc
            raise exc
    finally:
        await pump.stop()
        await bearer.stop()
        await _disconnect_quietly(client)

    if not prov.done or prov.device_key is None:  # no assert: dies under -O
        raise Phase0Failure(
            "provisioning",
            f"session ended without Complete (state {prov.state.name})",
            ["internal error — check the hex dumps for the last exchange"],
        )
    print()
    print("=== MILESTONE A: PROVISIONING COMPLETE ===")
    print(f"  unicast address: 0x{unicast:04x}")
    print(f"  device key:      {prov.device_key.hex()}")
    print()
    state["devices"][f"0x{unicast:04x}"] = {
        "device_key": prov.device_key.hex(),
        "uuid": target.uuid.hex(),
        "address": target.device.address,
    }
    # Advance past every element of the node so a future device in this state
    # file cannot overlap the lamp's secondary elements.
    state["next_unicast"] = unicast + max(1, prov.capabilities.num_elements)
    save_state(state_path, state)
    return unicast


# ------------------------------------------------------------- proxy phase


def pick_device(state: dict, args) -> int:
    """--toggle-only: select a previously provisioned device from the state."""
    if not state["devices"]:
        raise Phase0Failure(
            "startup",
            "--toggle-only, but the state file has no provisioned device",
            ["run once without --toggle-only to provision first"],
        )
    if args.device_uuid:
        for addr_hex, entry in state["devices"].items():
            if entry["uuid"] == args.device_uuid:
                return int(addr_hex, 16)
        raise Phase0Failure(
            "startup",
            f"no device with UUID {args.device_uuid} in the state file",
            ["check the state file's devices table"],
        )
    addr_hex = next(reversed(state["devices"]))  # most recently provisioned
    return int(addr_hex, 16)


async def _find_proxy_target(state: dict, transport, unicast: int):
    netkey = bytes.fromhex(state["netkey"])
    network_id = k3(netkey)
    scanner = transport.scanner()
    print(
        f"Scanning up to {SCAN_PROXY_S:.0f} s for the node's GATT Proxy advert "
        f"(0x1828, Network ID {network_id.hex()})..."
    )
    match, candidates = await find_proxy_node(scanner, network_id, timeout=SCAN_PROXY_S)
    if match is not None:
        print(f"  Network ID match from {match.device.address}")
        return match.device
    # Single-candidate fallback ONLY for Node Identity: a type-0x00 advert
    # with a non-matching Network ID is cryptographically proven foreign,
    # so it must never be "assumed ours" no matter how alone it is.
    if (
        len(candidates) == 1
        and candidates[0].identification_type == IDENTIFICATION_NODE_IDENTITY
    ):
        cand = candidates[0]
        print(
            f"  no Network ID match; single 0x1828 advertiser "
            f"{cand.device.address} uses Node Identity — assuming it is our "
            "node (identity verification needs crypto outside Phase 0 scope)"
        )
        return cand.device
    if candidates:
        listing = "; ".join(
            f"{c.device.address} type 0x{c.identification_type:02x} "
            f"{c.parameter.hex()}"
            for c in candidates
        )
        raise Phase0Failure(
            "proxy scan",
            f"{len(candidates)} 0x1828 advertisers, none matching our Network ID",
            [
                f"candidates seen: {listing}",
                "type-0x00 candidates with a different Network ID are "
                "cryptographically proven foreign (neighbor's mesh)",
                "our node may advertise Node Identity alongside other "
                "advertisers — rerun, or power the other networks down for "
                "the test",
            ],
        )
    # Nothing advertised 0x1828 at all: capture what the airwaves DID carry.
    entry = state["devices"].get(f"0x{unicast:04x}", {})
    seen = []
    for addr, (dev, adv) in (
        scanner.discovered_devices_and_advertisement_data.items()
    ):
        marker = " <- provisioned address" if addr == entry.get("address") else ""
        services = ", ".join(sorted(adv.service_data)) or "no service data"
        seen.append(f"{addr}: {services}{marker}")
    raise Phase0Failure(
        "proxy scan",
        "no 0x1828 proxy advert seen",
        [
            "node may not enable GATT Proxy by default — it must be switched "
            "on via the vendor app, or the device only supports the ADV bearer",
            "node still rebooting after provisioning — retry with --toggle-only",
            "advertisements captured during the scan window: "
            + ("; ".join(seen) if seen else "none at all"),
        ],
    )


async def configure(
    node: MeshNode, unicast: int, appkey: bytes, state: dict, pump: BearerPump
) -> None:
    print("Config: AppKey Add (netkey idx 0, appkey idx 0)...")
    t0 = time.perf_counter()
    try:
        resp = await node.request(
            unicast,
            config_appkey_add(state["netkey_index"], state["appkey_index"], appkey),
            OP_CONFIG_APPKEY_STATUS,
            dev_key=True,
            timeout=10,
        )
    except TimeoutError as exc:
        raise Phase0Failure("config AppKey Add", str(exc), _timeout_causes(pump))
    status = parse_config_appkey_status(_full_payload(resp))
    if status.status != 0x00:
        name = STATUS_NAMES.get(status.status, "Unknown")
        raise Phase0Failure(
            "config AppKey Add",
            f"AppKey Status 0x{status.status:02x} ({name})",
            [
                "0x06 Key Index Already Stored with a DIFFERENT key means the "
                "node kept an old appkey — factory-reset it and start over",
            ],
        )
    print(f"  AppKey Status: Success ({(time.perf_counter() - t0) * 1000:.0f} ms)")

    print(
        f"Config: Model App Bind (element 0x{unicast:04x}, "
        f"model 0x{GENERIC_ONOFF_SERVER_MODEL:04x} Generic OnOff Server)..."
    )
    t0 = time.perf_counter()
    try:
        resp = await node.request(
            unicast,
            config_model_app_bind(
                unicast, state["appkey_index"], GENERIC_ONOFF_SERVER_MODEL
            ),
            OP_CONFIG_MODEL_APP_STATUS,
            dev_key=True,
            timeout=10,
        )
    except TimeoutError as exc:
        raise Phase0Failure(
            "config Model App Bind", str(exc), _timeout_causes(pump)
        )
    status = parse_config_model_app_status(_full_payload(resp))
    if status.status != 0x00:
        name = STATUS_NAMES.get(status.status, "Unknown")
        raise Phase0Failure(
            "config Model App Bind",
            f"Model App Status 0x{status.status:02x} ({name}) binding model "
            f"0x{GENERIC_ONOFF_SERVER_MODEL:04x}",
            [
                "the element may not host a Generic OnOff Server — some lamps "
                "expose only Light Lightness (0x1300) or vendor models; Generic "
                "OnOff is Phase 0's go/no-go target, so treat this as a NO-GO "
                "for this device",
                "enumerate the models with Config Composition Data Get "
                "(btmesh_min.access.config_composition_data_get) to confirm",
            ],
        )
    print(f"  Model App Status: Success ({(time.perf_counter() - t0) * 1000:.0f} ms)")


async def toggle_loop(node: MeshNode, unicast: int, pump: BearerPump) -> None:
    print("Toggling Generic OnOff: 3 ON/OFF cycles, 2 s apart...")
    tid = 0
    for cycle in range(1, 4):
        for onoff in (1, 0):
            t0 = time.perf_counter()
            try:
                resp = await node.request(
                    unicast,
                    generic_onoff_set(bool(onoff), tid),
                    OP_GENERIC_ONOFF_STATUS,
                    timeout=5,
                )
            except TimeoutError as exc:
                raise Phase0Failure(
                    f"toggle (cycle {cycle}, {'ON' if onoff else 'OFF'})",
                    str(exc),
                    _timeout_causes(
                        pump,
                        ["AppKey bind lost (rerun without --toggle-only)"],
                    ),
                )
            ms = (time.perf_counter() - t0) * 1000
            status = parse_generic_onoff_status(_full_payload(resp))
            print(
                f"  cycle {cycle} {'ON ' if onoff else 'OFF'}: "
                f"present={status.present_onoff} ({ms:.0f} ms)"
            )
            tid = (tid + 1) & 0xFF
            await asyncio.sleep(2.0)
    print()
    print("=== MILESTONE B: TOGGLE OK — GO ===")


async def mesh_session(
    args, state: dict, state_path: Path, transport, unicast: int
) -> None:
    """Connect the proxy, configure (unless --toggle-only), toggle. Milestone B."""
    entry = state["devices"][f"0x{unicast:04x}"]
    netkey = bytes.fromhex(state["netkey"])
    appkey = bytes.fromhex(state["appkey"])
    device_key = bytes.fromhex(entry["device_key"])

    target = await _find_proxy_target(state, transport, unicast)
    print(f"Connecting Mesh Proxy to {target.address}...")
    session_handler: list = []

    def dispatch(msg_type: int, payload: bytes) -> None:
        if session_handler:
            session_handler[0](msg_type, payload)
        else:
            logger.debug("PDU before session ready: 0x%02x %s", msg_type, payload.hex())

    client, bearer = await open_gatt_session(
        transport, target, provisioning=False, on_message=dispatch
    )
    pump = BearerPump(bearer, MSG_TYPE_NETWORK_PDU)
    pump.on_error = lambda exc: logger.error("network TX failed: %s", exc)

    def send_network_pdu(raw: bytes) -> None:
        pump.put(raw)
        # Persist SEQ after EVERY send: a replayed SEQ after a crash would be
        # silently ignored by the node.
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

    def on_message(msg_type: int, payload: bytes) -> None:
        if msg_type == MSG_TYPE_NETWORK_PDU:
            node.handle_network_pdu(payload)
        else:
            logger.debug(
                "ignoring proxy message type 0x%02x: %s", msg_type, payload.hex()
            )

    try:
        session_handler.append(on_message)
        pump.start()
        if not args.toggle_only:
            await configure(node, unicast, appkey, state, pump)
        await toggle_loop(node, unicast, pump)
    finally:
        await pump.stop()
        await bearer.stop()
        await _disconnect_quietly(client)


# ------------------------------------------------------------------- main


async def async_main(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    try:
        state = load_or_create_state(state_path)
    except Phase0Failure as failure:
        print_diagnostic(failure.phase, failure, failure.causes)
        return 1

    if args.transport == "esphome":
        transport = EsphomeTransport(args.esphome_host, args.esphome_key)
    else:
        transport = LocalTransport()

    try:
        await transport.start()
    except BearerError as exc:
        print_diagnostic(
            "transport",
            exc,
            [
                "ESPHome host unreachable (name/IP, Wi-Fi)",
                "wrong --esphome-key (API noise PSK)",
                "proxy has no free BLE connection slot",
            ],
        )
        return 1

    try:
        if args.toggle_only:
            unicast = pick_device(state, args)
            print(f"--toggle-only: using device 0x{unicast:04x} from {state_path}")
        else:
            unicast = await provision(args, state, state_path, transport)
        await mesh_session(args, state, state_path, transport, unicast)
        return 0
    except Phase0Failure as failure:
        print_diagnostic(failure.phase, failure, failure.causes)
        return 1
    except (BtMeshError, TimeoutError, OSError) as exc:
        print_diagnostic(
            "unclassified",
            exc,
            [f"see the DEBUG hex dumps in {LOG_FILE} for the last frames on the air"],
        )
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
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
