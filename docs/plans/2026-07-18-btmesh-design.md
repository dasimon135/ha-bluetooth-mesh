# Bluetooth Mesh for Home Assistant — Design Document

**Date:** 2026-07-18
**Status:** Validated design (brainstorming session)
**Author:** dasimon135 (with Claude)

## Problem

Home Assistant has no support for Bluetooth SIG Mesh. Entire product families are
orphaned: Häfele Connect Mesh (Loox lighting), Telink-based luminaires, countless
"app-only" mesh lights. Existing attempts either require a discontinued vendor
gateway (all Häfele HACS integrations) or an experimental BlueZ `bluetooth-meshd`
setup that cannot run on Home Assistant OS (dominikberse/homeassistant-bluetooth-mesh).

This project delivers the first Bluetooth Mesh stack usable by any HA user — with
no extra hardware beyond what most installs already have (an ESPHome Bluetooth
proxy or a local BT adapter).

**Personal driver:** the author's Häfele Connect Mesh LED strips (gateway
discontinued) become the primary test bed. The previously chosen Gledopto Zigbee
bypass is postponed in favor of keeping the mesh alive.

## Key decisions (validated)

1. **Own mesh network** — devices are factory-reset out of the vendor app and
   provisioned into a network whose keys are generated and stored by the
   integration. No key extraction, no coexistence with the vendor app (a node
   can be handed back via Config Node Reset).
2. **Pure-Python mesh stack over the GATT bearer** — the stack runs inside HA
   and talks to the mesh through the standard Mesh Proxy Service (GATT) of any
   powered node, reached via `bleak`/`habluetooth` — i.e. through existing
   ESPHome Bluetooth proxies. No BlueZ meshd, no dedicated ESP32 firmware, works
   on HAOS-in-a-VM with no radio.
3. **Two deliverables**, mirroring the pymadoka-ng / daikin_madoka split:
   - `btmesh-py` *(name tentative — check PyPI availability)*: HA-independent
     Python library implementing the mesh stack.
   - `bluetooth_mesh` HA custom integration (HACS): config flow, storage,
     entities, connection management.

## Architecture

```
HA (Python mesh stack)
  │  bleak / habluetooth
  ▼
ESPHome BLE proxy (existing fleet)          ← or any local BT adapter
  │  GATT: Mesh Proxy Service (proxy protocol tunnel)
  ▼
Mesh node with GATT Proxy enabled (any powered lamp)
  │  advertising bearer (mesh relay)
  ▼
Rest of the mesh network
```

**One GATT connection serves the whole network.** The stack maintains a single
tunnel to one proxy node; the mesh relays messages to every other node.

### Library layers (`btmesh-py`)

- **Crypto**: k1–k4 derivations, AES-CCM, network obfuscation — implemented with
  the `cryptography` package, no compiled C.
- **Network layer**: PDU encrypt/decrypt, IV index, sequence numbers.
- **Transport (lower/upper)**: segmentation/reassembly, acks.
- **Access layer + models**: Config Client; Generic OnOff, Light Lightness,
  Light CTL, Light HSL clients (covers mono / tunable-white / RGB strips).
- **Provisioning (PB-GATT)**: full procedure — invite, ECDH key exchange,
  NetKey distribution, unicast address assignment. OOB auth steps supported.
- **Bearer abstraction**: `Bearer` interface with a `bleak` implementation;
  other bearers can be added later.

The library is fully testable without HA.

### HA integration (`bluetooth_mesh`)

- Mesh database (NetKey, AppKey, IV index, sequence numbers, node list,
  composition data) in HA storage.
- `light` entities with capabilities derived from each node's models.
- Connection manager (see Runtime below), repairs issues, diagnostics.

## Provisioning UX

Everything happens in the HA UI — this is where prior art fails.

1. **Network creation** (first install): NetKey/AppKey/IV index generated
   automatically; no questions asked.
2. **Discovery**: unprovisioned nodes beacon; ESPHome proxies already relay
   these advertisements to `habluetooth`. Each device triggers a standard HA
   discovery flow: "Häfele LED strip detected — add to mesh?"
3. **One-click provisioning**: PB-GATT via the best-RSSI proxy, ECDH exchange,
   NetKey handoff, unicast address assignment. OOB authentication (blink codes,
   numeric) surfaces as a flow step when required.
4. **Automatic post-provisioning configuration** (invisible to the user):
   Config Client adds the AppKey, binds it to the light models found in the
   composition data, and **enables GATT Proxy** on the node (required for our
   bearer).
5. **Result**: a named `light` entity with the right capabilities.

**Leaving the network**: deleting the device in HA sends Config Node Reset —
the node becomes provisionable again (by us or the vendor app).

**One-time prerequisite**: factory-reset the strips out of the Häfele app.

## Runtime

**Command path** (`light.turn_on` → strip): entity builds the access message
(e.g. Light CTL Set, with TID for dedup) → AppKey then NetKey encryption,
sequence increment → proxy-protocol encapsulation → GATT write on the connected
proxy node → mesh relays to the target. Status messages return through the same
tunnel; we also subscribe to spontaneous node publications (physical remotes).

**Connection strategy** (multi-proxy experience reused from pymadoka-ng):

- Persistent connection to the best-RSSI proxy node, light keepalive.
- On loss: automatic failover to another proxy-capable node, via any available
  ESPHome proxy. Every mains-powered lamp is a potential entry point.
- **Sequence number persisted aggressively** (every write + periodic flush).
  Replaying a sequence number after a crash gets all our messages rejected —
  the classic mesh trap. On startup, resume from last persisted value + safety
  margin; IV Index update recovery implemented in v1, not "later".

**Expected latency**: ~100–300 ms per command through an ESPHome proxy — on par
with a phone mesh app, fine for lighting.

## Error handling

- Command without status reply → retry with same TID (idempotent), then mark
  entity `unavailable` after N failures. Periodic lightweight Light Get as
  per-node health check.
- No reachable GATT entry point → all entities `unavailable` + explicit repairs
  issue (same pattern as daikin_madoka).
- Keys live in HA storage — same trust model as any integration's credentials.

## Risks (by severity)

1. **Real-world interop of Häfele/Telink firmware** (top risk): GATT proxy that
   sleeps, tight timeouts, exotic OOB. → Mitigated by Phase 0 below.
2. **Stack size**: months, not weeks. → Strict v1 scope: PB-GATT only (no
   PB-ADV), light models only, no heartbeat, no friend/low-power.
3. **Tunnel throughput/reliability via ESPHome proxy**: GATT stability proven by
   pymadoka-ng; proxy-protocol segmentation throughput validated in Phase 0.

## Testing

- **Crypto/network/transport**: the Mesh spec publishes official sample data —
  every crypto function and sample PDU becomes a unit test *before*
  implementation (TDD).
- **Upper layers**: mock bearer replaying recorded exchanges — CI without radio.
- **HA integration**: `pytest-homeassistant-custom-component`.
- **Hardware**: Häfele strips + 2–3 cheap generic mesh nodes (~10 €) for
  cross-vendor interop.

## Roadmap

- **Phase 0 — Feasibility (go/no-go)**: throwaway script that provisions ONE
  Häfele lamp and toggles it via an ESPHome proxy. If this passes, the rest is
  engineering, not research.
- **Phase 1 — `btmesh-py` core**: crypto + network + transport + PB-GATT
  provisioning, validated against spec sample data.
- **Phase 2 — Models**: Config Client, Generic OnOff, Lightness, CTL, HSL.
- **Phase 3 — HA integration**: discovery/provisioning config flow, `light`
  entities, multi-proxy reconnection, repairs.
- **Phase 4 — Release**: PyPI + HACS, English docs, HA community forum
  announcement.

## Open items

- Strip type (mono / CCT / RGB) — determines which light model gets exercised
  first; the design supports all.
- Final library/integration naming and PyPI availability check.
- Whether Häfele remotes/switches get re-provisioned into the new network.
