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

### 1.3 (P2) Colour temperature is mirrored for *every* lamp

`light.py:174-188`. `_mesh_kelvin()` unconditionally sends `min + max - K` to
work around the Häfele/ThingOS inverted CTL mapping. The README advertises
"standard SIG-Mesh lights"; on a spec-compliant lamp this inverts warm and cool
end-to-end. Gate it on the vendor CID (`node.cid == 0x07E9`, already parsed and
already used for the manufacturer name) or expose an "invert colour
temperature" option, defaulting to the quirk only for known-affected CIDs.

### 1.4 (P2) Availability only refreshes through an accidental 30 s poll

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

### 1.5 (P2) Empty `meshUUID` collapses the config-entry unique_id

`config_flow.py:74` uses `network.uuid`, which `network_model.py:148` builds
with `str(data.get("meshUUID", ""))`. An export without that field yields
unique_id `""`, so a *second, different* network aborts with
`already_configured`. `MeshLight._attr_unique_id` (`light.py:101`) inherits the
same weakness — two networks would produce colliding entity unique_ids.

Fix: fall back to `k3(net_key).hex()` — the Network ID, which is the network's
real on-air identity and is already computed everywhere else.

### 1.6 (P3) One malformed node aborts the whole import

`network_model.py:257-277` requires `unicastAddress`, `deviceKey` and `cid` on
**every** node. A single odd entry (a provisioner record from another app
version, a node exported without `cid`) makes the whole import fail, and
`config_flow.py:71` collapses both `JSONDecodeError` and `NetworkModelError`
into one generic `invalid_connect` message — the user has no way to know which
node is at fault.

Fix: skip + `logger.warning` unparseable nodes (keep hard failures for the
keys), and surface `str(exc)` through `description_placeholders` in the form.

### 1.7 (P3) Corrupt entry data raises instead of failing cleanly

`__init__.py:22` → `MeshCoordinator.__init__` → `json.loads` /
`Network.from_connect` can raise on a corrupted entry, producing a raw
traceback instead of a proper setup failure. Wrap and raise
`ConfigEntryError` so the UI shows a message and offers a repair path.

---

## 2. Architecture gaps (the high-value work)

### 2.1 (P1 feature) Configure the proxy filter — the single biggest upgrade

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

### 2.2 (P1 robustness) Track the IV Index from the Secure Network Beacon

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

### 2.3 (P2) Push discovery is implemented but dead

`mesh_transport.py:169` `async_register_proxy_callback()` is production-ready
and unit-tested — and called from nowhere. Today recovery waits for the 15 s
`PROBE_INTERVAL_UNAVAILABLE` tick. Wiring it in the coordinator would trigger a
reconnect the moment a matching 0x1828 advert reappears.

### 2.4 (P2) One `.storage` write per button press

`coordinator.py:314` calls `await self._store.async_save(...)` on **every**
command. On HA OS / Green / Yellow that is an SD or eMMC write per tap. Switch
to `Store.async_delay_save` (HA flushes it on shutdown) and raise
`SEQ_SAFETY_MARGIN` above the worst-case number of commands inside the debounce
window (32 is already comfortable for a 10 s delay).

### 2.5 (P2) Setup blocks on a full connect

`async_start()` (`coordinator.py:186`) awaits `_async_probe()` inside
`async_setup_entry`, so a cold/absent proxy stalls the entry for up to
`CONNECT_TIMEOUT` (20 s) plus `establish_connection` retries — past HA's 10 s
"setup is taking too long" warning, delaying startup. Fire the first probe as a
background task (`entry.async_create_background_task`) and let the 15 s retry
loop converge.

### 2.6 (P3) `GattBearer.start()` can orphan a `start_notify` task

`bearer.py:145-161`: on the `START_NOTIFY_TIMEOUT` path the shielded task is
deliberately left running with a result-swallowing callback, but `stop()`
(`bearer.py:169`) never cancels it. Keep the handle on the instance and cancel
it in `stop()`.

### 2.7 (P3) No RX replay/dedup cache

`node.handle_network_pdu` accepts any PDU that authenticates, with no per-SRC
SEQ tracking. Low risk while nothing is forwarded to us — but it becomes real
the day §2.1 lands and statuses actually flow. Add a small `{src: last_seq}`
window.

### 2.8 (P3) Missing HA surfaces

* **`diagnostics.py`** — this integration's entire support burden is "which
  proxy sees what". A redacted dump (Network ID, IV Index, SEQ cursor,
  keep-alive, node/element/model inventory, `discovered_proxies()` output,
  connection state) would replace most log requests. **Must redact** NetKey,
  AppKey and every DeviceKey — `CONF_CONNECT_JSON` holds them all verbatim.
* **`async_step_reconfigure`** — re-importing a `.connect` export (node added,
  key refresh) currently means deleting the entry and losing every entity id
  and its history.
* **`RestoreEntity`** — after a restart every lamp shows `off` at brightness
  `None` regardless of reality, because the optimistic cache starts empty
  (`light.py:139-141`). Restoring last state is the honest default until §2.1
  makes real read-back possible.
* **Provisioning** — `provisioner.py` is complete in the library but
  unreachable from HA; adding an unprovisioned node still requires the vendor
  app. Natural v0.3 headline.

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

### 3.2 (P2) `iot_class` is wrong

`manifest.json` declares `local_push`, but nothing pushes: state is optimistic
and availability comes from a periodic probe. It should be `local_polling`
today — and can honestly become `local_push` once §2.1 lands.

### 3.3 (P2) No lint job

No `[tool.ruff]` section and no lint step in either workflow, while the sibling
`daikin_madoka` repo runs ruff 0.16. Add the config + a job; the code is
already clean enough that it should pass on day one.

### 3.4 (P3) `phase0/tests` never runs in CI

`pyproject.toml` sets `testpaths = ["tests", "phase0/tests"]`, but
`.github/workflows/tests.yml` runs `pytest tests -q`. Either add it to CI or
drop it from `testpaths` so local and CI agree.

### 3.5 (P3) The `v0.1.0` tag predates the brand assets

`v0.1.0` points at `3d9a964`; the icons landed in `4062840`. HACS installs the
tagged release, so users currently get no brand icon. Cut `v0.1.1`.

### 3.6 (P3) `hacs.json` declares no minimum core version

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

## 5. Suggested order

1. ~~§1.1 client leak + §1.2 dead pump~~ and ~~§3.1 vendoring `--check`~~ —
   **done 2026-07-26**.
2. §3.2 `iot_class`, §3.3 ruff — short, protects everything after it.
3. §1.3 CTL mirror gating, §1.4 availability listener, §1.5 unique_id fallback.
4. §2.4 delayed SEQ save, §2.5 background first probe, §2.3 push discovery.
5. **§2.1 proxy filter** — then re-open `STATUS_TIMEOUT`, use the existing
   getters, and flip `iot_class` to `local_push` for real.
6. §2.2 Secure Network Beacon / IV Index tracking, then §2.8 diagnostics +
   reconfigure + restore.
