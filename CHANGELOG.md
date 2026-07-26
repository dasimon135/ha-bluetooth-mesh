# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A reconfigure flow.** Networks change — a node is added, a key refreshed —
  and the only way to import the new export was to delete the entry and re-add
  it, losing every entity id and the history behind it. Pasting a *different*
  network is refused rather than silently repointing every entity.
- **Push discovery.** The integration recovers the moment a proxy for its
  network advertises again, instead of waiting out the retry tick.

### Changed

- **Setup no longer blocks on the first connect.** It awaited a full connect —
  up to the connect timeout plus retries — inside `async_setup_entry`, past the
  point where Home Assistant warns that an integration is slow to set up.
- **The SEQ cursor is written through a debounced store** instead of on every
  command: one flash write per button press wears out an SD card for nothing.
  It is flushed immediately when the entry unloads, and the safety margin
  applied at startup already covers whatever a crash leaves unwritten.
- `iot_class` is now `local_polling`. Nothing pushes: the integration
  subscribes to no unsolicited publication, it asks when the mesh becomes
  reachable.
- **ruff runs in CI**, pinned, and the test job no longer silently excludes the
  `phase0` harness suite. `hacs.json` declares a 2024.11.0 floor, so an older
  core is refused instead of failing at import.

### Fixed

- **A malformed node no longer sinks the whole import.** Exports come from
  another vendor's app; one node missing a field it never promised made the
  entire network unusable behind a flat "not a valid export". Unparseable nodes
  are skipped with a warning naming them — losing *every* node still fails,
  since an empty network would look like success.
- **A network without a `meshUUID` gets a stable identity.** The unique id fell
  back to an empty string, so any second such network aborted as already
  configured. `k3(NetKey)` — the Network ID nodes advertise — is used instead.
- **A damaged stored export fails the setup cleanly** with a message pointing
  at the reconfigure flow, instead of a raw traceback.
- **An unconfirmed GATT subscribe is cancelled when the bearer stops**, rather
  than left running against a client the caller is about to disconnect.
- **A duplicate inbound PDU is dropped** (same source, same SEQ as the one just
  handled). Deliberately not the spec's full replay list: rejecting every SEQ
  below the last would deafen the integration to a node that restarted its
  sequence after a power cut, which is worse than the stale value a replayed
  Status could briefly show.

## [0.3.0] — 2026-07-26

### Added

- **The IV Index is tracked from the subnet's Secure Network Beacon.** It was
  frozen at whatever the `.connect` export claimed (usually 0), and the mesh
  moves on without telling the file. A stale IV Index is fatal in silence:
  every PDU we send is discarded and every PDU we receive fails the IVI check,
  with nothing in the logs to explain it. The node announces the truth on every
  connection; the beacon is now authenticated (`k1(NetKey, s1("nkbk"),
  "id128" || 0x01)`), and a new index is adopted, persisted, and restarts the
  SEQ cursor — which is only required to be unique *within* an IV Index. An
  unauthenticated beacon is refused: adopting a forged one would mute the
  integration.
- **Redacted diagnostics** (`diagnostics.py`): the Network ID the integration
  looks for, every 0x1828 advert Home Assistant currently sees, the IV Index
  and SEQ in use, connection state, and each node's element/model composition.
  No key material: the NetKey, AppKey and DeviceKeys the config entry stores
  verbatim are never echoed, which is asserted by a test.

### Fixed

- **The colour-temperature mirror no longer applies to every vendor.**
  Häfele/ThingOS lamps map Light CTL temperature inversely and the workaround
  mirrors the requested Kelvin around the exposed range; applied to a
  spec-conformant lamp it inverted warm and cool end to end. It is now gated on
  the Häfele company identifier.

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

[0.3.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.3.0
[0.2.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.2.1
[0.2.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.2.0
[0.1.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.1
[0.1.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.0
