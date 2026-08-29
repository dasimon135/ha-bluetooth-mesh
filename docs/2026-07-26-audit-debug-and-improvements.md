# Debug pass & improvement roadmap — 2026-07-26

Full read-through of `custom_components/bluetooth_mesh/` + `src/btmesh/` at
`4062840`, with the test suite and the packaging/CI surface. Baseline: **255
passed, 6 skipped** locally (the 6 skips are the `tests/ha/` tree — no
`homeassistant` in the library venv; CI installs it and runs them for real).
`src/btmesh` and the vendored `custom_components/bluetooth_mesh/btmesh` are
currently **byte-identical** (12/12 modules).

Severity: **P1** = user-visible breakage, **P2** = wrong behaviour in a
plausible setup, **P3** = hardening / polish.

---

## Status as of 2026-08-24 (re-verified against the tree, not against this file)

This roadmap had drifted badly: most items below were fixed in the 0.2–0.4
line without the heading being updated, so reading it top-down overstated
what was left by about a dozen entries. Every heading has now been checked
against the code and carries the evidence.

**Still open:**

* §2.8 — provisioning from Home Assistant (`RestoreEntity` turned out to be
  the wrong fix and was superseded; see the entry). Tracked as issue #12; it is
  now the only substantial item left here.
* §3.5 — moot: the tag in question is many releases behind.

Everything else is done. One correction on 2026-08-28: §1.6's second half was
still listed as open here, but it had shipped in 0.4.8 the same week; only that
entry was re-checked against the tree, so the date above still marks the last
full sweep.

---

## 1. Confirmed bugs

### 1.1 (P1) A failed `controller.start()` leaks a live BLE connection — **FIXED**

> Fixed on 2026-07-26 as described below, with two regression tests
> (`test_failed_controller_start_disconnects_the_client`,
> `test_failed_controller_construction_disconnects_the_client`).


`coordinator.py:257-274`. `async_connect_bearer()` returns the client into a
**local** variable; `self._client` is only assigned *after*
`await controller.start()` succeeds. The `except` path calls `_teardown()`,
which clears `self._controller` / `self._client` — both still `None` — so the
freshly connected client is never disconnected.

Failure paths that hit this: `GattBearer.start()` raising `BearerError` on a
genuine subscribe error (`bearer.py:162-166`), and `CONNECT_TIMEOUT` (20 s)
firing while `start()` is in flight.

Impact is exactly the failure mode the whole on-demand/keep-alive design exists
to avoid: a zombie link pins the lamp's **single proxy slot** (locking out both
Home Assistant and the Häfele app) and burns an ESPHome proxy connection slot.
It is self-reinforcing — the next `_ensure_connected()` cannot get in, so the
coordinator marks itself unavailable and raises the `proxy_unreachable` repair
while it is itself holding the slot.

Fix: keep the local handle and close it in the failure path.

```python
client = None
try:
    async with asyncio.timeout(CONNECT_TIMEOUT):
        client, bearer = await async_connect_bearer(self.hass, address)
        controller = MeshController(...)
        await controller.start()
except Exception as exc:  # noqa: BLE001
    logger.debug("mesh connect failed: %s", exc)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.disconnect()
    await self._teardown()
    self._set_unavailable()
    return None
```

Regression test to add: `async_connect_bearer` returns a mock client,
`MeshController.start` raises → assert `client.disconnect` was awaited.

### 1.2 (P1) A dead TX pump is invisible — the coordinator keeps a wedged link — **FIXED**

> Fixed on 2026-07-26: `MeshController` now exposes `failed` / `failure` (wired
> to `BearerPump.on_error`) and the coordinator drops the link both after a
> command and before reusing a held one. Four regression tests:
> `test_dead_tx_pump_is_reported_as_failed`,
> `test_healthy_controller_is_not_failed`,
> `test_dead_transport_drops_the_held_connection`,
> `test_dead_transport_is_never_reused_by_the_next_command`.


`pump.py:48-59`, `controller.py:79`, `coordinator.py:296-306`.

`BearerPump._run()` swallows the first `send()` failure, records it in
`.failure`, calls `.on_error`, and **stops consuming the queue forever**.
`MeshController` never sets `on_error` and never inspects `.failure`. Because
commands are best-effort (`request()` → `TimeoutError` → `return None`), the
coordinator's `except` branch in `_run_connected` never fires, so `_teardown()`
is never called and the dead controller is reused indefinitely.

Reproduced (pure library, no HA):

```
results (None = unconfirmed, no exception): None None None
frames the bearer accepted: 2
pump failure recorded: RuntimeError('GATT write failed: link wedged')
pump task done: True
```

Symptom for the user: the entity stays **available**, every tap appears to work
in the UI, and nothing happens on the lamp until the entry is reloaded. The
only accidental escape hatch is `getattr(self._client, "is_connected", True)`
in `_ensure_connected` — which does not help when the write fails while the
GATT link still reports connected (proxied ESPHome link torn down remotely,
characteristic gone after a re-pair, MTU/segmentation error).

Note this is a **regression against phase0**, which does handle it:
`phase0/provision_and_toggle.py:289` checks `pump.failure` and
`phase0/hafele_control.py:110` wires `on_error`. The productised controller
dropped it.

Fix: in `MeshController.__init__`, `self._pump.on_error = self._on_pump_error`
setting a `self._failed` flag; expose `failed` (or `pump.failure`) and make
`_ensure_connected()` treat a failed controller as dead (teardown + reconnect),
or let commands raise once failed so `_run_connected`'s existing `except`
already tears down.

### 1.3 (P2) Colour temperature is mirrored for *every* lamp — **DONE (v0.3.0, redone in v0.5.1)**

> First gated on the Häfele company identifier (`_INVERTED_CTL_CIDS`), then —
> in 0.5.1 — moved to the per-lamp option this entry offered as its alternative.
> Issue #7 produced a Häfele lamp that the CID gate was itself inverting, which
> means the quirk varies *within* a vendor, by model or firmware, and no company
> identifier can predict it. The suggestion below to default "to the quirk only
> for known-affected CIDs" was therefore the wrong half of its own choice: the
> CID now decides nothing, and existing entries were seeded once from the old
> rule so nothing flipped on upgrade.

`light.py:174-188`. `_mesh_kelvin()` unconditionally sends `min + max - K` to
work around the Häfele/ThingOS inverted CTL mapping. The README advertises
"standard SIG-Mesh lights"; on a spec-compliant lamp this inverts warm and cool
end-to-end. Gate it on the vendor CID (`node.cid == 0x07E9`, already parsed and
already used for the manufacturer name) or expose an "invert colour
temperature" option, defaulting to the quirk only for known-affected CIDs.

### 1.4 (P2) Availability only refreshes through an accidental 30 s poll — **DONE (v0.2.0)**

> `MeshCoordinator.async_add_listener()` + `should_poll = False`.

`light.py:145-148` reads `self._coordinator.available`, but the coordinator has
no listener registry and never notifies entities when `_set_available()` /
`_set_unavailable()` flips. What saves it today is that `LightEntity` defaults
to `should_poll = True`, so HA polls every 30 s (light `SCAN_INTERVAL`) with no
`async_update` defined and writes state afterwards. So: availability lags up to
30 s, and every entity is polled for nothing.

Fix (both halves together, or availability stops updating at all): add
`async_add_listener` / `_notify_listeners()` to `MeshCoordinator`, have
`MeshLight` subscribe in `async_added_to_hass`, and set
`_attr_should_poll = False`.

### 1.5 (P2) Empty `meshUUID` collapses the config-entry unique_id — **FIXED**

> `Network.identifier` (`network_model.py`) falls back to `k3(net_key).hex()`;
> `config_flow.py` and `light.py:131` both key off it.

`config_flow.py:74` uses `network.uuid`, which `network_model.py:148` builds
with `str(data.get("meshUUID", ""))`. An export without that field yields
unique_id `""`, so a *second, different* network aborts with
`already_configured`. `MeshLight._attr_unique_id` (`light.py:101`) inherits the
same weakness — two networks would produce colliding entity unique_ids.

Fix: fall back to `k3(net_key).hex()` — the Network ID, which is the network's
real on-air identity and is already computed everywhere else.

### 1.6 (P3) One malformed node aborts the whole import — **FIXED**

> Both halves shipped, the second in 0.4.8. `Network.from_connect` collects and
> logs the unparseable nodes and hard-fails only when *none* survive; `_parse()`
> in `config_flow.py` now returns an `(error_key, detail)` pair instead of a
> bare `None`, so a paste that is not JSON at all (`invalid_json`) is told apart
> from a well-formed document that is not an export (`invalid_connect`), and the
> parser's own detail reaches the form through `description_placeholders`.

`network_model.py:257-277` requires `unicastAddress`, `deviceKey` and `cid` on
**every** node. A single odd entry (a provisioner record from another app
version, a node exported without `cid`) makes the whole import fail, and
`config_flow.py:71` collapses both `JSONDecodeError` and `NetworkModelError`
into one generic `invalid_connect` message — the user has no way to know which
node is at fault.

Fix: skip + `logger.warning` unparseable nodes (keep hard failures for the
keys), and surface `str(exc)` through `description_placeholders` in the form.

### 1.7 (P3) Corrupt entry data raises instead of failing cleanly — **FIXED**

> `__init__.py:async_setup_entry` catches `JSONDecodeError` / `NetworkModelError`
> / `KeyError` and raises `ConfigEntryError` naming the reconfigure flow.

`__init__.py:22` → `MeshCoordinator.__init__` → `json.loads` /
`Network.from_connect` can raise on a corrupted entry, producing a raw
traceback instead of a proper setup failure. Wrap and raise
`ConfigEntryError` so the UI shows a message and offers a repair path.

---

## 2. Architecture gaps (the high-value work)

### 2.1 (P1 feature) Configure the proxy filter — the single biggest upgrade — **IMPLEMENTED**

> Implemented on 2026-07-26: `btmesh.proxy_config` + `MeshNode.build/parse_proxy_config_pdu`
> + `MeshController._configure_filter()` (accept list, our own address).
>
> **HARDWARE-VALIDATED** the same day on the Häfele lamp through the
> `atomebuanderie` ESPHome proxy. Status replies now come back in **145–310 ms**
> where nothing ever came back before, on every command, with no timeout. Two
> findings from that run:
>
> * The lamp **applies the filter but never sends a Filter Status**, though the
>   spec says it shall. Blocking on that confirmation therefore added its full
>   2 s timeout to *every* connection and logged a warning claiming replies "may
>   not be forwarded" while they demonstrably were. Filter setup is now
>   fire-and-forget: the ordered TX pump is what guarantees both messages
>   precede the first command (visible in the trace as `0x02, 0x02, 0x00` 1 ms
>   apart), so waiting bought nothing.
> * The **Secure Network Beacon is right there** on every connect —
>   `RX type=0x01 … 000b8150e5fb81e99f 00000000 …` — carrying `IV Index = 0`.
>   That both confirms the currently hard-coded assumption and shows §2.2 is
>   directly implementable from data the lamp already sends us.
>
> `STATUS_TIMEOUT` was left at 1.5 s: measured round trips are 145–310 ms, so it
> is ample, and a silent node must not slow every button press.


`proxy_pdu.py:42` defines `MSG_TYPE_PROXY_CONFIG = 0x02`, and **nothing else in
the tree implements Proxy Configuration**. That is the root cause documented in
`coordinator.py:99-105`: the node forwards no Status replies, so every GET
times out, all state is optimistic, and `STATUS_TIMEOUT` is squeezed to 1.5 s
just to keep the UI responsive.

Per spec §6.5 a proxy starts with an **empty accept-list filter** — it forwards
nothing until the client sets it. Two messages after connect (`Set Filter Type`
0x00, then `Add Addresses To Filter` 0x01 with our `SRC_ADDR = 0x7FFF`) would
unlock:

* confirmed state instead of optimistic (`get_onoff`, `get_lightness` become
  usable — they are already implemented and unused);
* brightness / colour-temperature read-back without the mid-fade drift hacks;
* detection of changes made from the vendor app or a wall switch;
* a genuinely `local_push` integration (see §3.2).

Scope: one codec module (~80 lines + tests) in `src/btmesh`, one call in
`MeshController.start()`, and re-opening `STATUS_TIMEOUT`.

### 2.2 (P1 robustness) Track the IV Index from the Secure Network Beacon — **DONE (v0.3.0)**

> Implemented and hardware-validated: the lamp's beacon authenticates against
> the imported NetKey and confirms IV Index 0. Adoption restarts the SEQ cursor
> and drops the live link so the next connection rebuilds on the new index.

The IV Index is hard-coded to whatever the export says, defaulting to **0**
(`network_model.py:112-113, 153`), and `network.decode()` rejects any PDU whose
IVI bit does not match (`network.py:144`). If the mesh ever runs an IV Update —
or the network was provisioned with a non-zero IV Index — the integration goes
permanently and silently deaf, with no diagnostic beyond "unavailable".

The node sends a Secure Network Beacon (`MSG_TYPE_MESH_BEACON = 0x01`, carrying
IV Index + IV Update flag + Key Refresh flag) right after the proxy connection
opens — and `MeshController._on_message` (`controller.py:108-112`) currently
logs it as "ignoring proxy message type 0x01". Parse it, authenticate it with
the beacon key (`k1`, already implemented), adopt the IV Index, and persist it
next to the SEQ cursor. Self-healing, ~60 lines.

### 2.3 (P2) Push discovery is implemented but dead — **FIXED**

> Wired at `coordinator.py:374` (`self._discovery_unsub = async_register_proxy_callback(...)`).

`mesh_transport.py:169` `async_register_proxy_callback()` is production-ready
and unit-tested — and called from nowhere. Today recovery waits for the 15 s
`PROBE_INTERVAL_UNAVAILABLE` tick. Wiring it in the coordinator would trigger a
reconnect the moment a matching 0x1828 advert reappears.

### 2.4 (P2) One `.storage` write per button press — **FIXED**

> `coordinator.py:725` uses `async_delay_save`; `SEQ_SAFETY_MARGIN` is 32.

`coordinator.py:314` calls `await self._store.async_save(...)` on **every**
command. On HA OS / Green / Yellow that is an SD or eMMC write per tap. Switch
to `Store.async_delay_save` (HA flushes it on shutdown) and raise
`SEQ_SAFETY_MARGIN` above the worst-case number of commands inside the debounce
window (32 is already comfortable for a 10 s delay).

### 2.5 (P2) Setup blocks on a full connect — **FIXED**

> `coordinator.py:368` fires the first probe as `async_create_background_task`.

`async_start()` (`coordinator.py:186`) awaits `_async_probe()` inside
`async_setup_entry`, so a cold/absent proxy stalls the entry for up to
`CONNECT_TIMEOUT` (20 s) plus `establish_connection` retries — past HA's 10 s
"setup is taking too long" warning, delaying startup. Fire the first probe as a
background task (`entry.async_create_background_task`) and let the 15 s retry
loop converge.

### 2.6 (P3) `GattBearer.start()` can orphan a `start_notify` task — **FIXED**

> `bearer.py` keeps `self._subscribe_task`; `stop()` cancels *and awaits* it.

`bearer.py:145-161`: on the `START_NOTIFY_TIMEOUT` path the shielded task is
deliberately left running with a result-swallowing callback, but `stop()`
(`bearer.py:169`) never cancels it. Keep the handle on the instance and cancel
it in `stop()`.

### 2.7 (P3) No RX replay/dedup cache — **FIXED**

> `node.py` keeps `_last_rx_seq: dict[int, int]`, deliberately a duplicate guard
> rather than the spec's replay list (see the comment there for why).

`node.handle_network_pdu` accepts any PDU that authenticates, with no per-SRC
SEQ tracking. Low risk while nothing is forwarded to us — but it becomes real
the day §2.1 lands and statuses actually flow. Add a small `{src: last_seq}`
window.

### 2.8 (P3) Missing HA surfaces

* ~~**`diagnostics.py`**~~ — **DONE (v0.3.0)**, redaction asserted by test.
* ~~**`async_step_reconfigure`**~~ — **DONE**, with a unique-id mismatch guard
  so pasting a *different* network cannot silently repoint every entity.
* **Originally:** this integration's entire support burden is "which
  proxy sees what". A redacted dump (Network ID, IV Index, SEQ cursor,
  keep-alive, node/element/model inventory, `discovered_proxies()` output,
  connection state) would replace most log requests. **Must redact** NetKey,
  AppKey and every DeviceKey — `CONF_CONNECT_JSON` holds them all verbatim.
* ~~**`RestoreEntity`**~~ — **SUPERSEDED**, deliberately not implemented. The
  complaint was that a lamp came back as `off` after a restart; the fix was to
  stop claiming `off` at all (`is_on` returns `None` → `unknown`) and to read
  the lamp for real from `async_refresh_state()` as soon as the coordinator
  becomes available, which §2.1 made possible. Restoring a remembered state
  would be *less* honest than `unknown`: it asserts a value nobody has checked,
  and another integration acting on it — a light group syncing its members is
  enough — makes the invention true.
* **Provisioning** — **STILL OPEN**, and now the only substantial item left on
  this roadmap. `provisioner.py` is complete in the library but unreachable
  from HA; adding an unprovisioned node still requires the vendor app.

---

## 3. Packaging, CI and hygiene

### 3.1 (P1) Nothing enforces the vendored copy is in sync — **FIXED**

> Fixed on 2026-07-26: `scripts/sync_vendored_btmesh.py --check` reports drift
> and exits non-zero; the Tests workflow runs it before installing.


HACS ships `custom_components/bluetooth_mesh/btmesh/`, `src/btmesh/` is
canonical, and `scripts/sync_vendored_btmesh.py` is a **manual** step. They are
identical today, but a forgotten run would ship a stale stack to users while CI
stays green. Add a `--check` mode (diff, non-zero exit) and a CI step. Cheapest
high-value fix in this document.

### 3.2 (P2) `iot_class` is wrong — **FIXED**

> `manifest.json` declares `local_polling`. It stays honest: §2.1 landed, but
> state is still read by explicit Get, not pushed.

`manifest.json` declares `local_push`, but nothing pushes: state is optimistic
and availability comes from a periodic probe. It should be `local_polling`
today — and can honestly become `local_push` once §2.1 lands.

### 3.3 (P2) No lint job — **FIXED**

> `[tool.ruff]` in `pyproject.toml`, `ruff==0.16.0` job in `tests.yml`.

No `[tool.ruff]` section and no lint step in either workflow, while the sibling
`daikin_madoka` repo runs ruff 0.16. Add the config + a job; the code is
already clean enough that it should pass on day one.

### 3.4 (P3) `phase0/tests` never runs in CI — **FIXED**

> The workflow runs a bare `pytest -q`, so `testpaths` governs both sides.

`pyproject.toml` sets `testpaths = ["tests", "phase0/tests"]`, but
`.github/workflows/tests.yml` runs `pytest tests -q`. Either add it to CI or
drop it from `testpaths` so local and CI agree.

### 3.5 (P3) The `v0.1.0` tag predates the brand assets — **MOOT**

> Overtaken by events: v0.4.5 is current and carries the assets.

`v0.1.0` points at `3d9a964`; the icons landed in `4062840`. HACS installs the
tagged release, so users currently get no brand icon. Cut `v0.1.1`.

### 3.6 (P3) `hacs.json` declares no minimum core version — **FIXED**

> `"homeassistant": "2024.11.0"`.

The integration uses `entry.runtime_data` and PEP 695 `type` aliases →
HA ≥ 2024.6 / Python 3.12. Add `"homeassistant": "2024.6.0"` so HACS blocks
installs that would fail at import time.

### 3.7 (note) `bleak-retry-connector` is imported but not declared

`mesh_transport.py:23` imports it at module scope without listing it in
`manifest.json` requirements. It is guaranteed present via
`"dependencies": ["bluetooth"]` — the accepted HA pattern — so this is fine;
noted only so it is not "fixed" by accident.

---

## 4. What is solid (verified, no action needed)

* **Crypto** (`crypto.py`): `k1`–`k4`, `s1`, CCM are spec-faithful and covered.
* **Network layer**: nonce construction, PECB obfuscation, NID/IVI gating,
  24-bit SEQ overflow guard (`network.py:74`) are all correct.
* **Transport**: SAR segmentation, SeqZero/SegO/SegN packing, `_seq_auth()`
  rewind, and the reassembler's key-change reset are right.
* **SEQ/TID persistence**: the safety margin is genuinely applied **once**
  (`_load_seq`, `coordinator.py:442`), cursors are persisted even on a failed
  command, and the TID carry-over across controllers is correct — the subtle
  parts are handled and regression-tested.
* **Concurrency**: one `asyncio.Lock` around every connect/command/teardown;
  `async_stop` cannot race a re-armed idle timer (`_arm_idle` checks
  `_stopped`).
* **Translations**: `strings.json`, `en.json`, `fr.json` have identical key
  sets, including `data_description` and the repair issue.
* **`.gitignore`**: `*.connect` / `*.apk` excluded, no key material or vendor
  bundle tracked, `dist/` untracked.

---

## 5bis. Status — everything above is shipped

Every finding in this document has been addressed and released, except the two
items below, which were considered and deliberately left:

* **Provisioning** (§2.8) — adding an *unprovisioned* node is a feature, not a
  fix: it needs its own flow, its own design, and hardware with an
  unprovisioned lamp to validate against. Still the right shape for a future
  headline release.
* **`RestoreEntity`** (§2.8) — restoring the last known state now works
  *against* the integration. v0.2.1 removed the fabricated "off" precisely
  because another integration can act on an invented state and make it true,
  and the lamp is read for real within seconds of the mesh becoming reachable.
  Restoring would put a stale value back into exactly that window, for no gain.

§2.7 shipped as a **duplicate filter rather than the spec's replay list**:
rejecting every SEQ below the last would deafen the integration to a node that
restarted its sequence after a power cut, while the worst a replayed Status can
do is show a stale value for a moment. The lesser guarantee is the safer trade.

Shipped in v0.1.1 → v0.4.0, each release hardware-validated on a Häfele Connect
Mesh lamp through an ESPHome Bluetooth proxy.

## 5. Suggested order

1. ~~§1.1 client leak + §1.2 dead pump~~ and ~~§3.1 vendoring `--check`~~ —
   **done 2026-07-26**.
2. ~~§2.1 proxy filter~~, ~~§1.3 CTL mirror~~, ~~§1.4 availability listener~~,
   ~~§2.2 IV Index tracking~~, ~~§2.8 diagnostics~~ — **shipped in v0.2.0–v0.3.0,
   all hardware-validated.**
3. §3.2 `iot_class` (still `local_push`; `local_polling` is the honest value
   until unsolicited publications are subscribed to), §3.3 ruff.
4. §1.5 unique_id fallback, §1.6 tolerant node parsing, §1.7 ConfigEntryError.
5. §2.4 delayed SEQ save, §2.5 background first probe, §2.3 push discovery.
6. §2.8 remainder: reconfigure flow, RestoreEntity, provisioning.
