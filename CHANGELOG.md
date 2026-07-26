# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-07-26

### Fixed

- **A failed connect no longer leaks a live BLE link.** The proxy client is
  connected before the mesh controller is brought up on top of it; if that
  second step failed (GATT subscribe error, connect timeout), the client was
  unreachable from the teardown path and stayed connected — pinning the lamp's
  single proxy slot, locking out both Home Assistant and the vendor app, and
  making the coordinator report an unreachable proxy while itself holding it.
- **A dead mesh transport is now detected instead of being reused forever.** A
  failed GATT write kills the TX pump, which then stops transmitting for good.
  Commands are best-effort, so they simply timed out like an unconfirmed
  Status: the entity stayed *available* while every command silently did
  nothing until the config entry was reloaded. `MeshController` now exposes
  `failed` / `failure`, and the coordinator drops and re-establishes the link
  as soon as the transport dies.

### Added

- The `logo.png` / `logo@2x.png` brand assets, which landed after the `v0.1.0`
  tag and therefore never reached anyone installing the tagged release.

### Changed

- CI verifies that the vendored `custom_components/bluetooth_mesh/btmesh/` copy
  matches `src/btmesh/` (`scripts/sync_vendored_btmesh.py --check`), so a
  forgotten re-vendor cannot ship a stale stack while the suite stays green.

## [0.1.0] — 2026-07-20

First public release. A pure-Python Bluetooth SIG Mesh stack (`btmesh`) and a
Home Assistant custom integration (`bluetooth_mesh`), validated end-to-end on
real hardware against a Häfele Connect Mesh tunable-white lamp through an
ESPHome Bluetooth proxy.

### Added

- **Mesh stack (`btmesh`)**: k1–k4 derivations, AES-CMAC/AES-CCM, network
  obfuscation; network/transport/access layers; proxy-PDU segmentation and
  reassembly; a provisioner; and a GATT bearer over `bleak` / `habluetooth`
  (works through ESPHome Bluetooth proxies). Validated against the SIG spec
  sample vectors.
- **Home Assistant integration (`bluetooth_mesh`)**: config flow importing a
  ThingOS/Häfele `.connect` network export; a connection coordinator; and a
  `light` platform exposing on/off, brightness, and colour temperature (Light
  CTL) per node composition.
- **Coexistence model**: rides on the network the vendor app already
  provisioned (shared NetKey/AppKey), sending standard app-keyed SIG messages.
- **Kept-alive proxy connection** for instant commands, with a configurable
  keep-alive timeout (options flow) to hand the lamp's single proxy slot back
  to the vendor app when idle. `0` = always connected.
- **Local brand icon** (`brand/`) for Home Assistant ≥ 2026.3.

### Known limitations

- RGB / full-colour lamps (Light HSL / xyL) are not implemented — the reference
  hardware is tunable-white only.
- Optimistic state: brightness/temperature reflect the last command; changes
  made from the vendor app in parallel are not read back until HA's next
  command.

[0.1.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.1
[0.1.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.0
