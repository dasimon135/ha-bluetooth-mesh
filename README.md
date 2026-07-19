# Bluetooth Mesh for Home Assistant

A pure-Python **Bluetooth SIG Mesh** stack and a **Home Assistant** integration
that lets HA control Bluetooth Mesh lighting — Häfele Connect Mesh (Loox),
other ThingOS-based luminaires, and standard SIG-Mesh lights — **with no extra
hardware** beyond what most installs already have: an ESPHome Bluetooth proxy or
a local Bluetooth adapter.

Home Assistant has no native Bluetooth Mesh support, which orphans entire
product families of "app-only" mesh lights. Existing workarounds need either a
discontinued vendor gateway or an experimental BlueZ `bluetooth-meshd` setup
that cannot run on Home Assistant OS. This project removes both requirements.

> **Status:** early. Phase 0 feasibility is validated on real hardware — a
> Häfele Connect Mesh lamp is turned on/off **and** dimmed from the pure-Python
> stack through an ESPHome Bluetooth proxy, with correct Generic OnOff / Light
> Lightness status replies. The Home Assistant integration is a working
> skeleton (config flow + coordinator + light platform) under active
> development.

## Two deliverables

This repository ships two things, mirroring the `pymadoka-ng` / `daikin_madoka`
split:

1. **`btmesh`** (`src/btmesh/`) — a Home-Assistant-independent Python library
   implementing the mesh stack: crypto (k1–k4 derivations, AES-CMAC, AES-CCM,
   network obfuscation), network/transport/access layers, proxy-PDU
   segmentation/reassembly, a provisioner, and a GATT bearer that runs over
   `bleak` / `habluetooth` (so it works through ESPHome Bluetooth proxies).
2. **`bluetooth_mesh`** (`custom_components/bluetooth_mesh/`) — a HACS custom
   integration: config flow, storage, a connection coordinator, and Home
   Assistant entities (starting with `light`).

## How it works

```
Home Assistant (pure-Python mesh stack: btmesh)
  │  bleak / habluetooth
  ▼
ESPHome BLE proxy (existing fleet)          ← or any local BT adapter
  │  GATT: Mesh Proxy Service (proxy protocol tunnel)
  ▼
A mesh node with GATT Proxy enabled (any powered lamp)
  │  advertising bearer (mesh relay)
  ▼
The rest of the mesh network
```

One GATT connection serves the whole network: the stack maintains a single
tunnel to one proxy node, and the mesh relays messages to every other node.
No BlueZ meshd, no dedicated ESP32 firmware — it works on Home Assistant OS in
a VM with no local radio.

## Coexistence with the vendor app (shared keys)

Rather than replicating a vendor's proprietary activation step, this project
**rides on the mesh network the vendor app already created**. The app exports
its network as a `.connect` JSON (ThingOS format) containing the NetKey, the
AppKey, and each node's unicast address. The integration imports that file,
connects its proxy to the same network, and sends **standard**, app-keyed mesh
messages (Generic OnOff Set, Light Lightness Set, …) to each node's unicast
address.

Because both sides share the same keys, the vendor app and Home Assistant
control the **same lamps in parallel** — you keep your app, and HA drives the
same lights. (Operational note: a mesh node has a single GATT-proxy slot, so a
lamp stops advertising the Mesh Proxy Service while a phone holds the
connection; power-cycle the lamp so it advertises for the proxy.)

## Installation

1. **Add the repository to HACS** as a custom repository
   (HACS → Integrations → ⋯ → Custom repositories), category **Integration**,
   then install *Bluetooth Mesh* and restart Home Assistant.
2. **Export your network** from the vendor app as a `.connect` file.
3. **Add the integration** (Settings → Devices & Services → Add Integration →
   *Bluetooth Mesh*) and import the `.connect` file when prompted. Keep this
   file private — it contains your mesh network keys and should never be
   committed to a repository.

A Bluetooth transport is required: an ESPHome Bluetooth proxy on your network,
or a local Bluetooth adapter usable by Home Assistant.

## Development

```bash
# Library only (fast; no Home Assistant):
uv sync
uv run pytest tests            # btmesh library test suite

# Full suite (library + HA integration) in a HA-equipped environment:
pip install -e .
pip install -r requirements-test.txt
pytest tests                   # library + tests/ha/ (Home Assistant) together
```

The `tests/ha/` tree is guarded with `pytest.importorskip` so it stays skipped
when Home Assistant is not installed, and runs for real when it is. CI installs
Home Assistant and runs the combined suite on Linux; see
`.github/workflows/tests.yml` (pytest) and `.github/workflows/validate.yml`
(hassfest + HACS).

### Vendored library

`src/btmesh/` is the canonical library (the source of truth, published to PyPI).
Because HACS installs `custom_components/bluetooth_mesh/` as-is, the integration
ships a **vendored copy** of the library at
`custom_components/bluetooth_mesh/btmesh/`, so it installs with no external
`btmesh` dependency (only `cryptography`, which Home Assistant already provides).
After changing anything under `src/btmesh/`, re-sync the vendored copy:

```bash
python scripts/sync_vendored_btmesh.py
```

## Documentation

Design and feasibility write-ups live in [`docs/plans/`](docs/plans/):

- [`2026-07-18-btmesh-design.md`](docs/plans/2026-07-18-btmesh-design.md) —
  design document (problem, architecture, key decisions).
- [`2026-07-18-phase0-feasibility.md`](docs/plans/2026-07-18-phase0-feasibility.md) —
  Phase 0 feasibility plan.
- [`2026-07-19-phase0-report.md`](docs/plans/2026-07-19-phase0-report.md) —
  Phase 0 report (the hardware-validated breakthrough).
- [`2026-07-19-phase1-library-and-ha-integration.md`](docs/plans/2026-07-19-phase1-library-and-ha-integration.md) —
  Phase 1 plan (library + HA integration).

## Credit and honesty

This project grew out of reverse-engineering a Häfele Connect Mesh setup after
its vendor gateway was discontinued. The mesh crypto and codec layers are
implemented from the public Bluetooth SIG Mesh specification and validated
against the specification's official sample vectors; the coexistence approach
was discovered empirically against real hardware. It is an independent,
unofficial project and is not affiliated with or endorsed by Häfele, ThingOS,
or the Bluetooth SIG.

## License

No license has been chosen yet; all rights are reserved until one is added.
