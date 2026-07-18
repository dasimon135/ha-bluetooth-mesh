# Phase 0 — Feasibility Script Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A standalone Python script that provisions ONE Häfele Connect Mesh lamp over PB-GATT and toggles it with Generic OnOff, through an ESPHome Bluetooth proxy — the go/no-go gate for the whole btmesh project.

**Architecture:** A minimal but real mesh stack (`phase0/btmesh_min/`): crypto primitives → provisioning PDU codec + state machine → network/transport/access layers → GATT bearer (bleak, with ESPHome-proxy backend via `bleak-esphome`). Two milestones: **A** — Provisioning Complete received (validates Häfele interop, the top project risk); **B** — lamp toggles via Generic OnOff (validates the full crypto path). Crypto/codec modules are written TDD against official Mesh spec sample data and will seed Phase 1.

**Tech Stack:** Python 3.12+, `cryptography` (AES-CMAC, AES-CCM, ECDH P-256), `bleak`, `bleak-esphome` + `aioesphomeapi` (proxy transport), `pytest` + `pytest-asyncio`.

**Not in scope (YAGNI):** PB-ADV, relay/friend/heartbeat, IV update, multi-node, retransmission sophistication, key refresh. Segment acks are handled minimally (send all segments, wait for the status reply, one retry).

---

## Prerequisites (user actions, before Tasks 10+)

- Factory-reset ONE Häfele lamp from the Häfele Connect app (removes it from the vendor network; it starts beaconing as unprovisioned).
- ESPHome proxy reachable on the LAN: hostname/IP + native API encryption key at hand.
- Reference material for test vectors: Bluetooth **Mesh Profile Specification 1.0.1, Section 8 (Sample data)**. Free download from bluetooth.com; the same vectors appear in the Zephyr source tree (`tests/bluetooth/mesh/` and `subsys/bluetooth/mesh/` reference values). When a task below says "spec §8.x vector", fetch the exact bytes from there — do NOT invent vectors.

## Task 0: Project scaffolding

**Files:**
- Create: `phase0/pyproject.toml`, `phase0/btmesh_min/__init__.py`, `phase0/tests/__init__.py`, `phase0/.gitignore`

**Step 1: Create the package layout**

```toml
# phase0/pyproject.toml
[project]
name = "btmesh-phase0"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = [
    "cryptography>=42",
    "bleak>=0.22",
    "bleak-esphome>=2.0",
    "aioesphomeapi>=24",
]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

`phase0/.gitignore`: `.venv/`, `__pycache__/`, `phase0_state.json`, `*.log`

**Step 2: Create venv and install**

Run: `cd phase0 && uv sync` (or `python -m venv .venv && .venv/Scripts/pip install -e . --group dev`)
Expected: resolves and installs without error.

**Step 3: Smoke test**

Run: `cd phase0 && uv run pytest` → Expected: `no tests ran` (exit code 5 is fine).

**Step 4: Commit**

```bash
git add phase0/ && git commit -m "chore(phase0): scaffolding"
```

## Task 1: Crypto primitives — s1, k1, k2, k3, k4

**Files:**
- Create: `phase0/btmesh_min/crypto.py`
- Test: `phase0/tests/test_crypto.py`

**Step 1: Write failing tests using spec §8.1 vectors**

The s1 vector is canonical: `s1(b"test") == b7 3c ef bd 64 1e f2 ea 59 8c 2b 6e fb 62 f7 9c`. Fetch the k1 (§8.1.2), k2 (§8.1.3/8.1.4), k3 (§8.1.5), k4 (§8.1.6) vectors from the spec — each test asserts the exact spec output.

```python
# phase0/tests/test_crypto.py
from btmesh_min.crypto import s1, k1, k2, k3, k4

def test_s1_spec_8_1_1():
    assert s1(b"test") == bytes.fromhex("b73cefbd641ef2ea598c2b6efb62f79c")

def test_k1_spec_8_1_2():
    # N, SALT, P and expected output copied verbatim from spec §8.1.2
    ...

def test_k2_master_spec_8_1_3():
    # k2(NetKey, b"\x00") -> (NID, EncryptionKey, PrivacyKey); assert all three
    ...

def test_k3_spec_8_1_5():
    ...

def test_k4_spec_8_1_6():
    ...
```

**Step 2: Run** `uv run pytest tests/test_crypto.py -v` → FAIL (module missing).

**Step 3: Implement**

```python
# phase0/btmesh_min/crypto.py
"""Mesh security functions (Mesh Profile spec §3.8.2)."""
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.hazmat.primitives.ciphers.algorithms import AES

ZERO = bytes(16)

def aes_cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(AES(key))
    c.update(data)
    return c.finalize()

def s1(m: bytes) -> bytes:
    return aes_cmac(ZERO, m)

def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    return aes_cmac(aes_cmac(salt, n), p)

def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    salt = s1(b"smk2")
    t = aes_cmac(salt, n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    nid = t1[15] & 0x7F
    return nid, t2, t3  # (NID, EncryptionKey, PrivacyKey)

def k3(n: bytes) -> bytes:
    salt = s1(b"smk3")
    t = aes_cmac(salt, n)
    return aes_cmac(t, b"id64\x01")[8:]

def k4(n: bytes) -> int:
    salt = s1(b"smk4")
    t = aes_cmac(salt, n)
    return aes_cmac(t, b"id6\x01")[15] & 0x3F
```

**Step 4: Run** → PASS. If a k-function fails against the spec vector, fix the implementation, never the vector.

**Step 5: Commit** `git commit -m "feat(phase0): mesh key derivation functions with spec vectors"`

## Task 2: AES-CCM helpers

**Files:**
- Modify: `phase0/btmesh_min/crypto.py`
- Test: `phase0/tests/test_crypto.py`

**Step 1: Failing tests** — use the network-layer encryption intermediate values from spec §8.3 (EncryptionKey + network nonce + plaintext → ciphertext+MIC) as the CCM vector.

**Step 2–4: Implement with `cryptography`'s AESCCM (tag lengths 4 and 8), run, PASS**

```python
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

def ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, mic_len: int, aad: bytes = b"") -> bytes:
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, aad)

def ccm_decrypt(key: bytes, nonce: bytes, data: bytes, mic_len: int, aad: bytes = b"") -> bytes:
    return AESCCM(key, tag_length=mic_len).decrypt(nonce, data, aad)  # raises InvalidTag
```

**Step 5: Commit**

## Task 3: Provisioning PDU codec

**Files:**
- Create: `phase0/btmesh_min/prov_pdu.py`
- Test: `phase0/tests/test_prov_pdu.py`

**Step 1: Failing round-trip tests** for each PDU: Invite(0x00), Capabilities(0x01), Start(0x02), PublicKey(0x03), Confirmation(0x05), Random(0x06), Data(0x07), Complete(0x08), Failed(0x09). Parse the Capabilities sample from spec §8.7 provisioning sample data.

**Step 3: Implement** — small dataclasses + `encode()/decode()`; Capabilities fields: num_elements, algorithms(2), pubkey_type, static_oob, output_oob_size, output_oob_actions(2), input_oob_size, input_oob_actions(2). Log-friendly `__repr__` (we need to READ what the Häfele lamp announces — this is diagnosis data for the go/no-go).

**Step 5: Commit**

## Task 4: GATT Proxy PDU SAR framing

**Files:**
- Create: `phase0/btmesh_min/proxy_pdu.py`
- Test: `phase0/tests/test_proxy_pdu.py`

Proxy PDU header byte = SAR(2 bits: complete/first/continuation/last) | message type (0x00 network, 0x01 beacon, 0x02 proxy config, 0x03 provisioning). Implement `segment(msg_type, payload, mtu)` and a `Reassembler` class fed notification-by-notification. TDD with synthetic cases (payload < MTU, = MTU, spanning 3 frames). Commit.

## Task 5: Provisioning state machine

**Files:**
- Create: `phase0/btmesh_min/provisioner.py`
- Test: `phase0/tests/test_provisioner.py`

**Step 1: Failing test replaying the FULL provisioning sample of spec §8.7** — the spec gives every PDU of a complete no-OOB session including both key pairs and randoms. The state machine must accept injected (deterministic) provisioner keypair + random so the test can assert every outgoing byte.

**Step 3: Implement** — transport-agnostic class:

```python
class Provisioner:
    """Drives PB-GATT provisioning. Feed device PDUs via handle_pdu();
    outgoing PDUs come back via the send callback."""
    def __init__(self, netkey, key_index, iv_index, unicast_addr, send,
                 keypair=None, random=None, static_oob=None): ...
```

Flow: Invite(attention=0) → on Capabilities: choose FIPS P-256/no-OOB (if `static_oob` given and device requires it, use it; if device requires output/input OOB → raise with a clear diagnostic) → Start → PublicKey exchange (ECDH via `cryptography` P-256; public key = X||Y raw 64 bytes) → ConfirmationInputs = invite‖capabilities‖start‖pubkey_prov‖pubkey_dev (PDU payloads without the opcode byte? **verify against §8.7** — the spec sample settles it) → ConfirmationSalt = s1(inputs); ConfirmationKey = k1(ecdh_secret, salt, b"prck"); confirmation = aes_cmac(key, random16 + authvalue16) → exchange, verify device confirmation after Random exchange → ProvisioningSalt = s1(conf_salt + rand_prov + rand_dev); SessionKey = k1(secret, salt, b"prsk"); SessionNonce = k1(secret, salt, b"prsn")[-13:]; DeviceKey = k1(secret, salt, b"prdk") → Data = ccm_encrypt(session_key, nonce, netkey+key_index_be+flags+iv_index_be+unicast_be, mic_len=8) → await Complete. Expose `.device_key`, `.unicast_addr`.

**Step 4: PASS against the full spec session. Step 5: Commit.**

## Task 6: Network layer

**Files:**
- Create: `phase0/btmesh_min/network.py`
- Test: `phase0/tests/test_network.py`

TDD against spec §8.3 message samples (encode our known cases; decode the sample network PDUs). Encode: build 9-byte header IVI|NID, CTL|TTL, SEQ(3), SRC(2) + DST(2)+TransportPDU; CCM-encrypt DST+transport with EncryptionKey and network nonce `00 | ctl_ttl | seq | src | 0000 | iv_index` (MIC 4 for access, 8 for control); obfuscate first 6 header bytes after IVI/NID with PECB = AES-ECB(PrivacyKey, 5×00 | iv_index | first 7 bytes of ciphertext). Decode = reverse. Keep a `NetworkContext` dataclass (netkey → nid/enc/privacy via k2, iv_index, seq counter with `next_seq()`). Commit.

## Task 7: Upper + lower transport

**Files:**
- Create: `phase0/btmesh_min/transport.py`
- Test: `phase0/tests/test_transport.py`

TDD against spec §8.3 upper-transport samples. Upper: app nonce `01|00|seq|src|dst|iv` (AppKey, aid=k4) or device nonce `02|00|seq|src|dst|iv` (DeviceKey, aid=0, akf=0); CCM MIC 4. Lower: unsegmented access `0|AKF|AID` + payload (≤15 bytes); segmented `1|AKF|AID` + SZMIC=0|SeqZero(13)|SegO(5)|SegN(5) + 12-byte segments. **Each segment consumes its own network SEQ; SeqZero = SEQ of the first segment (13 LSBs); the upper-transport nonce uses that first SEQ.** Also implement parsing of incoming unsegmented access messages and of Segment Acknowledgment (control opcode 0x00) — parse-and-log only. Commit.

## Task 8: Access messages (Config + Generic OnOff)

**Files:**
- Create: `phase0/btmesh_min/access.py`
- Test: `phase0/tests/test_access.py`

Encoders (TDD, byte-exact):
- `config_appkey_add(netkey_idx, appkey_idx, appkey)` — opcode `0x00`, indexes packed into 3 bytes little-endian-ish per §4.3.1.1 (verify packing against spec sample §8.3.x: two 12-bit indexes → 3 bytes).
- `config_model_app_bind(element_addr, appkey_idx, model_id)` — opcode `0x803D`, SIG model, Generic OnOff Server = `0x1000`.
- `generic_onoff_set(onoff, tid)` — opcode `0x8202` (acked variant).

Decoders: Config AppKey Status (`0x8003`), Config Model App Status (`0x803E`), Generic OnOff Status (`0x8204`) → named tuples with status code. Commit.

## Task 9: Node facade — glue the stack

**Files:**
- Create: `phase0/btmesh_min/node.py`
- Test: `phase0/tests/test_node.py`

`MeshNode(netkey, appkey, iv_index, src_addr, send_network_pdu)` — provisioner-side runtime: `send_access(dst, payload, *, dev_key=None)` chooses device/app credentials, segments if >15 bytes, encrypts, emits network PDUs; `handle_network_pdu(raw)` decrypts, reassembles, decodes access replies into an asyncio queue; `request(dst, payload, expected_opcode, timeout=5, retries=1)` — send, await matching reply, one retry. Unit-test loopback style: encode with the node, decode with the test's own use of layers (or a second node instance with src/dst swapped). Commit.

## Task 10: GATT bearer (bleak + ESPHome proxy)

**Files:**
- Create: `phase0/btmesh_min/bearer.py`
- Test: manual (hardware) — no unit tests beyond import.

```python
PROV_SERVICE  = "00001827-0000-1000-8000-00805f9b34fb"
PROV_DATA_IN  = "00002adb-0000-1000-8000-00805f9b34fb"   # write-without-response
PROV_DATA_OUT = "00002adc-0000-1000-8000-00805f9b34fb"   # notify
PROXY_SERVICE  = "00001828-0000-1000-8000-00805f9b34fb"
PROXY_DATA_IN  = "00002add-0000-1000-8000-00805f9b34fb"
PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"
```

`GattBearer`: connect (bleak), subscribe Data Out → `Reassembler` → callback; `send(msg_type, payload)` → SAR segments → write-without-response to Data In (MTU = `client.mtu_size - 3`, fallback 20). Two transport factories:
- `local`: plain `BleakScanner`/`BleakClient` (PC's own adapter — useful for bench debugging).
- `esphome`: `aioesphomeapi.APIClient(host, port, password=None, noise_psk=key)` + `bleak_esphome.connect_scanner(...)` to register the proxy backend, then the same bleak calls run through the proxy. **Check bleak-esphome's current standalone API from its README (it moves); pin what works.** Scanning: unprovisioned devices advertise Service UUID 0x1827 with service data = Device UUID(16) + OOB info(2) — filter on that.

Commit.

## Task 11: Main script — provision + configure + toggle

**Files:**
- Create: `phase0/provision_and_toggle.py`

CLI: `--transport esphome|local`, `--esphome-host`, `--esphome-key`, `--device-uuid` (optional; else pick first 0x1827 beacon found), `--static-oob HEX` (optional), `--state phase0_state.json`, `--toggle-only`.

Flow:
1. Load or create `phase0_state.json`: netkey, appkey, iv_index=0, provisioner addr=0x0001, next unicast=0x0002, seq (persist after EVERY send — the design doc's sequence-replay trap applies even here).
2. Scan → log every 0x1827 beacon (UUID + OOB info) → connect PB-GATT → run `Provisioner` → **MILESTONE A: log "PROVISIONING COMPLETE, device_key=…"**; save device_key + unicast to state.
3. Reconnect: same device, now advertising the **Proxy Service (0x1828)** with Network ID (k3(netkey)) service data — wait up to ~10 s for it to appear. Connect, open `MeshNode` with bearer msg type 0x00.
4. Config AppKey Add (device key, segmented) → expect AppKey Status success (0x00). Config Model App Bind (Generic OnOff Server 0x1000) → expect Model App Status success.
5. Loop: Generic OnOff Set ON, wait status, sleep 2 s, OFF, status. **MILESTONE B: log "TOGGLE OK — GO".** `--toggle-only` skips to step 5 using persisted state (validates key/seq persistence across runs).

Log EVERYTHING hex-dumped at DEBUG (`--verbose`): every proxy frame in/out. This log is the interop evidence for the go/no-go.

Commit.

## Task 12: Hardware run + go/no-go report

**Steps:**
1. User factory-resets one lamp in the Häfele app.
2. Run: `uv run python provision_and_toggle.py --transport esphome --esphome-host <proxy> --esphome-key <key> --verbose |& tee run1.log`
3. Expected: MILESTONE A then B. Re-run with `--toggle-only` → still toggles (state persistence proven).
4. Write `docs/plans/2026-07-18-phase0-report.md`: capabilities announced by the lamp, OOB mode used, latency measured per command, failures encountered, **GO / NO-GO verdict**. Commit log + report.

**Failure triage:**
- No 0x1827 beacon → lamp not reset, or proxy not relaying service-data adverts (test `--transport local` to isolate).
- Capabilities demand output/input OOB → check lamp/QR for a static code; retry `--static-oob`; else document as blocker.
- Confirmation mismatch → ConfirmationInputs assembly (re-check against §8.7 byte-for-byte).
- Provisioning OK but no 0x1828 advert → node has GATT Proxy off by default → Config GATT Proxy Set via… device key over network — but we need a connection to send it. Fall back: some stacks keep the GATT connection up after provisioning or advertise Node Identity briefly; document what the lamp actually does in the report (this is exactly the interop data Phase 0 exists to collect).
- AppKey Status ≠ 0x00 or no reply → segmentation/ack handling; inspect Segment Acknowledgment logs.
