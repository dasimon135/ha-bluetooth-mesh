# Phase 1 — `btmesh` Library + `bluetooth_mesh` HA Integration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or executing-plans) to implement this plan task-by-task.

**Goal:** Turn the validated Phase-0 stack into (A) a clean, publishable `btmesh` Python library and (B) a `bluetooth_mesh` Home Assistant custom integration that imports a ThingOS `.connect` network file and exposes Häfele/standard mesh lighting nodes as HA `light` entities with on/off, brightness, and color temperature (CTL).

**Architecture:** Ride on the app's mesh network (proven in Phase 0): import NetKey/AppKey + node unicast addresses from the `.connect` export, connect to the mesh through Home Assistant's own Bluetooth proxies, and drive nodes with **standard SIG** Generic OnOff / Light Lightness / Light CTL access messages (app-keyed). Two deliverables mirror the pymadoka-ng + daikin_madoka split: the `btmesh` PyPI library (HA-independent) and the `bluetooth_mesh` HACS integration.

**Tech Stack:** Python 3.12+, `cryptography`, `habluetooth`/`bleak-retry-connector` (HA-managed Bluetooth), Home Assistant `config_entries`/`light` platform, `pytest` + `pytest-homeassistant-custom-component`.

**Ground truth from Phase 0** (see `docs/plans/2026-07-19-phase0-report.md`):
- Control = standard SIG opcodes app-keyed to a node's element-0 unicast; the lamp answers Generic OnOff Status / Light Lightness Status. Confirmed on hardware (`phase0/hafele_control.py`).
- `.connect` is ThingOS JSON: `netKeys[0].key`, `appKeys[0].key`, `nodes[].unicastAddress`, `nodes[].elements[].models[]` (with `modelId`, `bind`), `nodes[].tos_devices[].name`, `groups[]`. IV Index absent → 0.
- A node has one proxy slot and stops advertising 0x1828 while a phone holds the GATT link.

**Scope (YAGNI):** on/off + brightness + CTL temperature only. NO HSL/RGB, NO scenes/groups control, NO own-network provisioning UI in the integration (the Phase-0 provisioner stays library-only). Color/scenes are a later phase.

---

## Track A — `btmesh` library

### Task A0: Promote `phase0/btmesh_min` to a top-level `btmesh` package

**Files:**
- Move: `phase0/btmesh_min/*.py` → `src/btmesh/*.py` (git mv)
- Move: `phase0/tests/test_{crypto,prov_pdu,proxy_pdu,provisioner,network,transport,access,node}.py` → `tests/`
- Create: top-level `pyproject.toml` (publishable `btmesh` package)
- Modify: `phase0/*.py` scripts — change `from btmesh_min.X` → `from btmesh.X`
- Modify: `phase0/pyproject.toml` — depend on the root `btmesh` (path/editable)

**Steps:**
1. `git mv phase0/btmesh_min src/btmesh` and update the internal imports (they use relative imports `from .errors` etc. — those keep working). Rename package references.
2. Move the 8 library test files to top-level `tests/`. Leave the hardware-harness tests (`test_bearer.py`, `test_script*.py`) with the phase0 scripts, but point them at `btmesh` too.
3. Root `pyproject.toml`:
```toml
[project]
name = "btmesh"
version = "0.1.0"
description = "Pure-Python Bluetooth SIG Mesh stack (rides on an existing mesh network)"
requires-python = ">=3.12"
dependencies = ["cryptography>=42"]

[project.optional-dependencies]
ble = ["bleak>=0.22", "bleak-retry-connector>=3"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/btmesh"]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```
   Note: `bleak`/`habluetooth` only imported lazily inside `bearer.py`, so the core library installs with just `cryptography`.
4. Run `uv sync && uv run pytest` from the repo root → all Phase-0 library tests pass (expect 208-ish). Fix import paths until green.
5. Commit: `refactor(btmesh): promote btmesh_min to top-level publishable package`

### Task A1: API hardening (deferred Phase-0 polish)

**Files:** `src/btmesh/*.py`, `tests/`

Apply the items deferred during Phase 0 review:
1. Add `__all__` to every module listing its public surface.
2. `crypto.k2` returns a `NamedTuple K2Output(nid, encryption_key, privacy_key)` (keep positional compat). Update callers + tests.
3. `transport`: rename `unsegmented_access` → `build_unsegmented_access` for builder/parser symmetry (keep `segment_access_message`, `parse_access_lower`). Update callers.
4. Ensure every module docstring names its spec section.
TDD: existing tests must still pass after each rename (run `uv run pytest` between edits). Commit: `refactor(btmesh): public API surface + naming for Phase 1`

### Task A2: `MeshNetwork` model + `.connect` import

**Files:**
- Create: `src/btmesh/network_model.py`
- Test: `tests/test_network_model.py`
- Test fixture: `tests/fixtures/sample.connect.json` (a SANITIZED ThingOS export — fabricate keys/addresses; do NOT commit the real parnassium-1.connect)

**Model:**
```python
@dataclass(frozen=True)
class MeshModel:
    model_id: int            # SIG (<=0xFFFF) or vendor (company<<16|id)
    bound_appkey_indexes: tuple[int, ...]

@dataclass(frozen=True)
class MeshElement:
    index: int
    unicast: int             # base_unicast + index
    models: tuple[MeshModel, ...]

@dataclass(frozen=True)
class MeshNode:
    uuid: str
    unicast: int             # primary element address
    device_key: bytes
    cid: int
    name: str
    elements: tuple[MeshElement, ...]
    def has_model(self, model_id: int) -> bool: ...
    def element_for_model(self, model_id: int) -> MeshElement | None: ...

@dataclass(frozen=True)
class MeshNetwork:
    net_key: bytes
    net_key_index: int
    app_key: bytes
    app_key_index: int
    iv_index: int
    nodes: tuple[MeshNode, ...]
    @classmethod
    def from_connect(cls, data: dict) -> "MeshNetwork": ...
```

`from_connect` parses: `netKeys[0].{key,index}`, `appKeys[0].{key,index}`, `iv_index` default 0, each `nodes[]` → unicast `int(node["unicastAddress"],16)`, device_key from `deviceKey`, name from `tos_devices[0].name` (fallback `tos_node.type`), elements from `elements[]` with per-element unicast = base+index and models parsed (SIG modelId `len==4` hex → int; vendor `len==8` → company<<16|id). Raise `NetworkModelError(BtMeshError)` on malformed input.

TDD: parse the sanitized fixture; assert node unicast, device_key, that element 0 `has_model(0x1000)` and `0x1300`, that `element_for_model(0x1000).unicast == node.unicast`. Test malformed → raises. Commit: `feat(btmesh): MeshNetwork model + .connect import`

### Task A3: Light CTL access messages

**Files:** `src/btmesh/access.py`, `tests/test_access.py`

**VERIFY opcodes** against a fetched source (Zephyr `subsys/bluetooth/mesh/` or the Mesh Model 1.1 spec) before coding — do NOT trust these from memory:
- Light CTL Set `0x825E` / Set Unack `0x825F` / Status `0x8260`
- Light CTL Temperature Set `0x8264` / Set Unack `0x8265` / Status `0x8266`

Add builders + parsers:
- `light_ctl_set(lightness, temperature, delta_uv, tid)` → opcode + lightness(2 LE) + temperature(2 LE) + delta_uv(2 LE, signed) + tid. `temperature` in Kelvin 0x0320–0x4E20 (800–20000).
- `light_ctl_temperature_set(temperature, delta_uv, tid)` → opcode + temperature(2 LE) + delta_uv(2 LE) + tid.
- `parse_light_ctl_status(payload)` → NamedTuple(present_lightness, present_temperature, target_*?, remaining_time?).
- `parse_light_ctl_temperature_status(payload)`.
- Constants `MODEL_LIGHT_CTL_SERVER = 0x1303`, `MODEL_LIGHT_CTL_TEMP_SERVER = 0x1304`.
TDD: byte-exact builder tests + short/long status parse (spec sample if sourceable, else round-trip). Commit: `feat(btmesh): Light CTL access messages`

### Task A4: High-level async `MeshController`

**Files:**
- Create: `src/btmesh/controller.py`
- Test: `tests/test_controller.py`

A thin async facade over `MeshNode` for the integration. Given a `MeshNetwork`, a connected `GattBearer` (network msg type), and a source address, it wires a `MeshNode` + pump and exposes per-node commands:
```python
class MeshController:
    def __init__(self, network: MeshNetwork, bearer: GattBearer, *, src_addr: int = 0x7FFF): ...
    async def start(self) -> None: ...              # bearer.start + pump
    async def stop(self) -> None: ...
    async def set_onoff(self, unicast: int, on: bool, *, timeout=5.0) -> bool | None: ...
    async def get_onoff(self, unicast: int) -> bool | None: ...
    async def set_lightness(self, unicast: int, level_0_1: float) -> int | None: ...   # returns present lightness
    async def set_ctl_temperature(self, unicast: int, kelvin: int) -> int | None: ...
    @property
    def seq(self) -> int: ...                        # persistable
```
Reuse the ordered-pump pattern from `phase0/provision_and_toggle.py` (BearerPump) — MOVE `BearerPump` into `src/btmesh/pump.py` and import it in both places (DRY). Persist seq via `network`'s caller (expose `.seq`). TID auto-increments internally.
TDD: loopback pair like `test_node.py` — a controller drives a fake node; assert Generic OnOff Set / Light Lightness Set / CTL Temperature Set are emitted and status parsed. Commit: `feat(btmesh): high-level async MeshController`

---

## Track B — `bluetooth_mesh` HA integration

Reference `reference_ha_integration_ci` memory for hassfest/HACS/pytest gotchas. Study a HA BLE integration that uses proxies (e.g. the structure of an `async_ble_device_from_address` + `bleak_retry_connector` integration) before B1.

### Task B0: Integration skeleton (hassfest/HACS-clean)

**Files:**
- Create: `custom_components/bluetooth_mesh/__init__.py`, `manifest.json`, `const.py`, `hacs.json` (repo root), `custom_components/bluetooth_mesh/brands/…` per `reference_ha_brands_local`.
- Test: `tests/ha/test_init.py`

`manifest.json` (keys sorted domain,name,then alpha; `bluetooth` + `http` in dependencies since config flow uploads a file and we use HA bluetooth):
```json
{
  "domain": "bluetooth_mesh",
  "name": "Bluetooth Mesh",
  "codeowners": ["@dasimon135"],
  "config_flow": true,
  "dependencies": ["bluetooth"],
  "documentation": "https://github.com/dasimon135/ha-bluetooth-mesh",
  "iot_class": "local_push",
  "requirements": ["btmesh==0.1.0"],
  "version": "0.1.0"
}
```
`__init__.py`: `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`, `async_setup_entry` forwards to the `light` platform, `async_unload_entry`. `hacs.json`: `{"name": "Bluetooth Mesh", "render_readme": true}` (NO `domains` key).
Verify: run hassfest + HACS action locally if possible, else assert manifest keys sorted in a test. Commit: `feat(ha): bluetooth_mesh integration skeleton`

### Task B1: HA-Bluetooth bearer adapter

**Files:**
- Create: `custom_components/bluetooth_mesh/mesh_transport.py`
- Test: `tests/ha/test_mesh_transport.py`

Inside HA we do NOT use `EsphomeTransport` (that is standalone). Instead:
- Find the proxy: register `bluetooth.async_register_callback(hass, cb, {"service_uuid": "00001828-…"}, BluetoothScanningMode.ACTIVE)` and match adverts whose 0x1828 service-data Network ID == `k3(net_key)` (reuse `btmesh.bearer.proxy_candidates_from_discoveries` / the parsing helpers). Also seed from `bluetooth.async_discovered_service_info(hass)`.
- Connect: `device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)`; `client = await bleak_retry_connector.establish_connection(BleakClientWithServiceCache, device, name)`.
- Wrap the connected client in `btmesh.bearer.GattBearer(client, provisioning=False)` — the SAR framing + notify handling is reused as-is.
This is the one genuinely new integration-specific module. Keep it small; the mesh protocol stays in `btmesh`. Unit-test the Network-ID matching logic with fake `BluetoothServiceInfoBleak`; the connect path is covered by the integration tests. Commit: `feat(ha): HA-bluetooth mesh transport (proxy find + connect)`

### Task B2: Config flow — import `.connect`

**Files:**
- Create: `custom_components/bluetooth_mesh/config_flow.py`, `strings.json`, `translations/en.json`, `translations/fr.json`
- Test: `tests/ha/test_config_flow.py`

Single-step user flow: a file-upload/text field for the `.connect` JSON. Parse with `MeshNetwork.from_connect`; on success create an entry titled `meshName` with data = the parsed network serialized (keys as hex, nodes list). Handle bad JSON → `errors["base"]="invalid_connect"`. Prevent duplicate networks (unique_id = `meshUUID`).
Note (`reference_ha_integration_ci`): `selector` translations at root level; `data_description.<field>` is a string.
TDD (phcc): submit a valid sanitized `.connect` → creates entry; submit garbage → shows error. Commit: `feat(ha): config flow importing a .connect network`

### Task B3: Runtime coordinator

**Files:**
- Create: `custom_components/bluetooth_mesh/coordinator.py`
- Test: `tests/ha/test_coordinator.py`

Owns one `MeshController` (+ `MeshNetwork` rebuilt from entry data). Responsibilities:
- Connect on setup via `mesh_transport`; reconnect on drop (the proxy slot may be taken by the phone — surface a `repairs` issue like daikin_madoka's pattern after N failures, message explaining "turn off the app / free the lamp").
- Persist `controller.seq` to HA storage after each send (replay-safety, as in Phase 0).
- Provide `async_set_onoff/brightness/ctl_temperature(unicast, …)` used by entities.
- Optional light polling of state via Get; mark entities unavailable on connection loss.
TDD: with a fake transport/controller, assert reconnect + seq persistence + unavailable-on-loss. Commit: `feat(ha): mesh runtime coordinator`

### Task B4: `light` platform

**Files:**
- Create: `custom_components/bluetooth_mesh/light.py`
- Test: `tests/ha/test_light.py`

One `LightEntity` per lighting node (a node whose element 0 `has_model(0x1300)` Light Lightness or `0x1000` Generic OnOff). Capabilities:
- Always: on/off.
- `has_model(0x1300)` → `ColorMode.BRIGHTNESS`; map HA `brightness` 0–255 ↔ mesh lightness 0–65535 (`round(b/255*65535)` and inverse).
- `has_model(0x1303)` Light CTL → add `ColorMode.COLOR_TEMP`; map HA Kelvin (`color_temp_kelvin`) directly to mesh CTL temperature (both Kelvin; clamp 800–20000). Set `min/max_color_temp_kelvin` from the node if available else 2700–6500.
- `async_turn_on(**kwargs)`: if `ATTR_COLOR_TEMP_KELVIN` → `set_ctl_temperature`; if `ATTR_BRIGHTNESS` → `set_lightness`; else `set_onoff(True)`. `async_turn_off` → `set_onoff(False)` (or lightness 0). Optimistic state + reconcile from returned status.
- `unique_id = f"{meshUUID}_{unicast:04x}"`; device_info per node (name, cid as manufacturer, model type).
TDD (phcc): a node with Lightness+CTL exposes brightness+color_temp; turn_on with brightness calls controller with the right mesh value; turn_on with kelvin calls set_ctl_temperature. Commit: `feat(ha): light entities (onoff + brightness + CTL)`

### Task B5: CI + validation

**Files:**
- Create: `.github/workflows/validate.yml` (hassfest + HACS), `.github/workflows/tests.yml` (pytest), `requirements-test.txt` (`pytest-homeassistant-custom-component`), `README.md` (English).
- Modify: root `pyproject.toml`/pytest config for the `tests/ha` suite.

Per `reference_ha_integration_ci`: validate.yml runs `home-assistant/actions/hassfest@master` + `hacs/action@main` (`category: integration`); tests.yml sets up Python 3.13, installs requirements-test, runs pytest. Ensure `asyncio_mode = auto`. Commit: `ci(ha): hassfest/HACS/pytest workflows + English README`

---

## Track C — Release (after A+B green)

### Task C0: Publish

1. `btmesh` → PyPI (build + twine), tag `btmesh-v0.1.0`.
2. Update the integration `requirements` to the released `btmesh==0.1.0`.
3. Register the integration as a HACS custom repository on the user's HA (per `project_madoka_v240` pattern); document install in README.
4. Announce (optional): HA community forum.
Commit/release per repo conventions. English-only public content (`feedback_english_only_github`).

---

## Validation checkpoints

- After Track A: `uv run pytest` green; `MeshController` drives a loopback node through onoff/lightness/ctl.
- After Track B: phcc tests green; hassfest + HACS clean; a config entry from the sanitized `.connect` creates a working light entity in a test HA.
- **Hardware smoke (user):** install the integration on the user's HA, import `parnassium-1.connect`, confirm the "Boîtier Mesh" light turns on/off + dims + (if CTL) changes temperature from the HA UI — the real end-to-end proof, mirroring `hafele_control.py`.
