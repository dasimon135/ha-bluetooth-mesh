# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-07-26

### Fixed

- **A light no longer reports *off* before anything has been read.** The blank
  cache used to claim the lamp was off, which is not a harmless default: any
  other integration acting on that fabricated value — a light group syncing its
  members is enough — switches the lamp off for real, and the invented state
  becomes true. A light is now `unknown` until a read answers or a command is
  issued.

  Note this is a visible behaviour change: an automation testing
  `state == 'off'` will not match while the state is unknown.

## [0.2.0] — 2026-07-26

### Added

- **The proxy address filter is now configured on every connection**, which is
  what makes confirmed state possible at all. A Proxy Server starts each
  connection with an accept list that is *empty* (spec §6.5.1) — it forwards
  nothing inbound until told otherwise — so until now no Status reply ever
  reached Home Assistant and every value shown was purely optimistic.
  `MeshController.start()` sets the filter type and claims its own address, so
  a Set is confirmed by the lamp and `get_onoff` / `get_lightness` become
  usable. Best-effort: a proxy that does not answer only costs the
  confirmation, never the connection.

  Hardware-validated on a Häfele Connect Mesh lamp through an ESPHome
  Bluetooth proxy: Status replies now come back in 145–310 ms where nothing
  ever came back before. Note the lamp applies the filter but never sends the
  Filter Status the spec asks for, so the setup is deliberately
  fire-and-forget — both messages are queued ahead of the first command on the
  ordered TX pump, which is what actually guarantees the filter is in place.
- `btmesh.proxy_config`: Set Filter Type / Add Addresses To Filter / Filter
  Status codecs, plus `MeshNode.build_proxy_config_pdu` and
  `MeshNode.parse_proxy_config_pdu` for the `CTL=1, TTL=0` network PDU they
  travel in.
- **Lamps are read, not guessed.** The optimistic cache starts blank, so a lamp
  that was physically lit came back as *off* after every restart and stayed
  wrong until someone touched it. Each light now reads Generic OnOff — and
  Light Lightness when it is on and dimmable — as soon as the mesh becomes
  reachable, and again after every reconnection, which also catches what
  changed while Home Assistant was away. Colour temperature is not read back
  yet. An unanswered read leaves the cache untouched rather than inventing a
  state.

### Fixed

- **A Set no longer reports a mid-fade value.** With Status replies now
  arriving, a lamp answering mid-transition would have dragged the brightness
  slider to the value it was passing through. Set commands return the
  *target* — where the lamp is heading — and fall back to the present value
  only when no transition is running.
- **Availability changes reach the UI immediately.** Entities read the
  coordinator's availability directly, so a change only surfaced through Home
  Assistant's default 30-second entity poll. The coordinator now notifies its
  entities on an availability transition and the lights no longer poll at all.

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

[0.2.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.2.1
[0.2.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.2.0
[0.1.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.1
[0.1.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.0
