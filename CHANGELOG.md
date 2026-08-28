# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.8] — 2026-08-28

### Fixed

- **Setup failures and import errors are no longer English-only.** Two strings
  reached the user interface without passing through a translation. The
  `ConfigEntryError` raised when a stored `.connect` export cannot be read
  renders on the integration card, and it was built with an f-string, so a
  French install showed an English sentence there; it now carries the
  `corrupt_connect_export` translation key. The config-flow rejection reason
  was worse — the sentence around it was translated while the reason injected
  into it was hand-written English, producing a half-translated line.

  A paste that is not JSON at all and a well-formed document that is not an
  export are now two separate errors, `invalid_json` and `invalid_connect`,
  because their fixes differ: the first is usually a truncated or word-wrapped
  paste, the second is the wrong file. Only the parser's own detail stays in
  English — it names JSON fields that are English in the export itself.

  A test now compares the key set of every shipped translation against
  `strings.json`, so a key added to one file and forgotten in another fails
  the suite instead of silently rendering in English for that language.

## [0.4.7] — 2026-08-24

### Fixed

- **Reassembly is per source; two peers segmenting at once no longer destroy
  each other's transfer** ([#11](https://github.com/dasimon135/ha-bluetooth-mesh/issues/11)).
  `MeshNode` held a single `SegmentAssembler`, and the transfer identity it
  keyed on carried no source address. A segment from one node arriving
  mid-transfer from another did not interleave, it *reset*: the first node's
  partial reassembly was dropped and its acknowledgment timer cancelled with
  it. Neither message was ever delivered, and nothing anywhere said so.

  This was original Phase-0 behaviour, on the stated grounds that the stack
  talked to a single peer. That justification expired on both halves: the proxy
  address filter now lets Status traffic from any node reach us, and since
  0.4.6 we acknowledge segmented messages — which actively invites peers to
  send them.

  Reassembly state and both SAR timers are now held per source address. The
  table is bounded (`MAX_TRACKED_SEGMENT_SOURCES`), evicting an idle peer
  before one with a transfer in flight, and a stranded transfer is dropped
  outright when its incomplete timer expires rather than merely reset.

## [0.4.6] — 2026-08-24

### Added

- **The transport acknowledges segmented messages** (spec §3.5.3.3). `SegmentAck`
  had a parser and no builder: nothing anywhere emitted one. A peer that
  segments its reply and waits to be acknowledged therefore gave up, and the
  exchange failed with no error anywhere — the request timed out exactly as if
  the node had never received it ([#9](https://github.com/dasimon135/ha-bluetooth-mesh/issues/9)).

  Now a segmented message addressed to our own unicast is acknowledged the
  moment it is complete, which is the ack the sender is blocked on. An
  incomplete one arms the §3.5.3.3 acknowledgment timer (150 ms + 50 ms per TTL
  hop) and then reports the block-ack bitfield, so the peer retransmits the
  segment that is actually missing rather than the whole message; the
  incomplete timer abandons a stranded transfer after 10 s. A transfer that is
  already delivered is re-acknowledged rather than reassembled a second time —
  a peer whose ack was lost retransmits, and that must not surface as a
  duplicate message.

  Messages sent to a group or virtual address are deliberately **not**
  acknowledged: every subscriber would answer the sender at once.

  Nothing in the integration depended on this — every lighting command and
  Status fits in one segment, which is why it went unnoticed. It blocked the
  first device-keyed exchange that does not, which is why v0.4.4 had to abandon
  Config Composition Data as a reachability probe and fall back to Config Relay.

### Changed

- **The diagnostics probe no longer disowns its own `composition` field.**
  v0.4.4 shipped a note telling the reader that a null composition proves
  nothing, which was true then: the Status is segmented and nothing could ever
  complete it. It completes now, so on a node whose `answered` is true a null
  composition is a finding rather than an artefact of our transport, and the
  note says so.

### Fixed

- **A rejected `.connect` paste now says why.** The import and reconfigure forms
  collapsed a truncated paste, a file that is not an export at all, and an
  export whose every node is unparseable into one `invalid_connect` — three
  different problems with three different fixes, rendered identically. The
  parser's own message is carried through to the form.

- **A reassembly timer could outlive the connection.** `MeshController.stop()`
  now closes the node, so a pending acknowledgment cannot fire against a bearer
  that is already gone — which would have burned a persisted sequence number on
  an ack nothing could carry.

## [0.4.5] — 2026-08-24

### Added

- **The source address is configurable** (options flow, `0` = derive it). One
  thing a `.connect` export structurally cannot tell us is which unicast the
  vendor app gave *itself*: exports carry no provisioner node. If that address
  is the one we transmit from, every message we send is discarded as a replay by
  nodes holding a sequence number for it — before any model sees it, with
  nothing in any log to say so, and unaffected by re-importing the export.
  Moving off it was impossible until now; it is the last hypothesis standing in
  [#7](https://github.com/dasimon135/ha-bluetooth-mesh/issues/7).

  An address that is not a unicast, or that a node of the imported network
  already owns, is refused with a warning and the derived one is used instead —
  honouring it would mute the integration in exactly the way the option exists
  to escape.

## [0.4.4] — 2026-08-24

### Fixed

- **The 0.4.3 composition probe reported a false negative on a healthy node.**
  Run against the reference lamp — where on/off, brightness and state read-back
  all work — it answered `answered: false`. A Composition Data Status is
  segmented, and this stack transmits no Segment Acks (`SegmentAck` in
  `transport.py` is parse-only; `node.py` says so in its docstring), so a long
  status never completes and the node looks absent. As shipped, the probe
  invited exactly the wrong conclusion.

### Changed

- **Reachability is now a Config Relay Get.** Request and Status both fit in a
  single unsegmented message, so a silence is a real silence. It is device-keyed
  like before — no AppKey binding, no Light LC mode, no vendor model involved —
  so it still separates "the message never arrived" from "the node received it
  and did nothing". Its content earns its place too: a node with the Relay
  feature off forwards nothing from our proxy connection into the rest of the
  mesh, which is invisible from every other angle.
- **The composition is still requested, and now correctly framed.** It is the
  only place the node's own account of itself can be compared against the
  export, so it stays — but the dump states, in the dump, that a null
  composition proves nothing.

### Added

- `MeshController.get_relay()`, `Config Relay Get` / `Config Relay Status` in
  the access layer (opcodes verified against Zephyr's `foundation.h`).

## [0.4.3] — 2026-08-23

### Added

- **A composition probe in the diagnostics dump.** Every silent mesh failure
  conflates two questions — did the message reach the node, and did the node
  choose to act on it — and nothing in the dump could tell them apart. A
  `Config Composition Data Get` is answered by a node's Config Server under its
  **device key**, without consulting an AppKey binding, a Light LC mode or a
  vendor model. So an answer proves the round trip and points at the model
  layer; silence points at the transport. It also reports what each node says
  it *is*: the rest of the dump is the vendor app's account of the network,
  parsed from the export, and this is the only place the two can be compared.

  Bounded on purpose: skipped entirely when no proxy connection is held (a
  download must not spend the connect timeout dialling an absent proxy), capped
  at the first 6 nodes, and whatever is left out is reported rather than
  silently dropped.

- `MeshController.get_composition()` in the library, and every node's device key
  registered on the runtime node so Foundation-model traffic can be addressed
  to any of them.

## [0.4.2] — 2026-08-23

Four ways a command could be discarded without a word, and no way to tell them
apart. Reported in [#7](https://github.com/dasimon135/ha-bluetooth-mesh/issues/7):
the proxy connects, the frames go out, the lamp never reacts and nothing
answers. None of these is confirmed as that reporter's cause yet — all four are
real, all four look identical from the outside, and that is the actual defect.

### Fixed

- **A command is addressed to the element that hosts the model**, not to the
  node's primary address. An element silently ignores an opcode it has no model
  for — it neither acts nor answers — so a node that lays its lighting servers
  out across several elements took every command in silence. Light CTL
  Temperature was already routed this way; on/off, lightness and CTL now are
  too.
- **The application key is chosen from what the models bind.** A node matches
  an incoming message's AID against the keys each of its models was bound to
  and discards anything else at the upper transport layer. The export's first
  key was used on faith, which is wrong for any network holding more than one.
  Every key in the export is now parsed, and the one the driven models actually
  bind is the one commands are encrypted with.
- **The source address steps aside for a node that already owns it.** We
  transmit from unicast `0x7FFF`; if the export gives that address to a node,
  that node's peers already hold a replay-protection entry for it and drop
  everything we send — permanently, and re-importing the export does not help.
  The address is now taken from the export's free range instead of assumed.
- **A subnet that never beacons says so.** The `.connect` export carries no IV
  Index, so 0 is an assumption and the Secure Network Beacon is the only thing
  that can confirm it. Silence now produces one warning naming the unverified
  index, instead of nothing at all.
- **The deprecated config-entry update listener is gone.** Home Assistant stops
  honouring it in 2026.12. The options flow is an `OptionsFlowWithReload` and
  reloads the entry itself.

### Changed

- **A node whose lighting servers are not on element 0 now gets a light.**
  Capability detection always scanned every element; entity creation did not,
  so such a node was hidden entirely rather than exposed with fewer features.
  Server models only — a remote's client models still yield no entity.
- **Diagnostics answer the "why is nothing happening" question.** Added: the
  unicast we transmit from, the AppKey index in use next to the ones the export
  carries and the ones the models ask for, and each model's `bind` list. The
  per-element `models` entries are now objects (`{"id", "bind"}`) rather than
  bare strings.

## [0.4.1] — 2026-07-26

### Changed

- Documentation caught up with the code. The README still described the 0.1.0
  integration, and both forum drafts carried a claim that had become false —
  "optimistic state: parallel changes from the vendor app aren't read back".
  On/off and brightness have been read from the mesh since 0.2.0; colour
  temperature genuinely is still last-command-wins, so that is what the stated
  limitation now says. The README also documents reading state, `unknown`
  instead of a guessed `off`, the reconfigure flow, diagnostics, and the
  corrected development commands.

## [0.4.0] — 2026-07-26

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

[0.4.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.4.1
[0.4.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.4.0
[0.3.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.3.0
[0.2.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.2.1
[0.2.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.2.0
[0.1.1]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.1
[0.1.0]: https://github.com/dasimon135/ha-bluetooth-mesh/releases/tag/v0.1.0
