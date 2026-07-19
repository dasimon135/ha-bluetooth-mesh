# Phase 0 Feasibility Report — Häfele Connect Mesh over ESPHome proxy

**Date:** 2026-07-19
**Hardware:** 1× Häfele Connect Mesh LED controller (Device UUID
`186cc8fbb5ec45d593676549eb03b268`, BLE addr `C3:EB:49:65:67:53`), reached
through the `atomebuanderie` ESPHome Bluetooth proxy (`192.168.1.22`).
**Verdict:** **CONDITIONAL GO** — the entire standard Bluetooth Mesh stack is
proven on real hardware; the one blocker is that Häfele lamps expose **no
standard lighting models**, so end-to-end light control needs vendor-model
support (a Phase 1 research task).

## What worked on real hardware

Milestone A (**PROVISIONING COMPLETE**) was reached and everything up to
model binding works against the physical lamp through the ESPHome proxy:

| Stage | Result |
|-------|--------|
| Unprovisioned beacon scan (0x1827) | ✅ seen, OOB info 0x0000 (No OOB) |
| PB-GATT connect via ESPHome proxy | ✅ (needs bleak-retry-connector + power-cycle timing) |
| Provisioning Invite → Capabilities | ✅ decoded: 1 element, FIPS P-256, Blink+Push OOB |
| No-OOB auth selection | ✅ (device offers only output/input OOB; provisioner selects No OOB per §5.4.2.2) |
| ECDH public-key exchange | ✅ |
| Confirmation + Random exchange | ✅ device confirmation verified |
| Provisioning Data (encrypted) | ✅ device key derived, unicast 0x0003 assigned |
| GATT Proxy advert (0x1828) after provisioning | ✅ Network ID `e047818c394a5222` (= k3(netkey)) matched |
| Mesh Proxy reconnect | ✅ |
| Config AppKey Add (segmented, device-key) | ✅ **Success (~200 ms)** — validates network+transport+segmentation+device-key crypto on air |
| Config Composition Data Get (multi-segment status) | ✅ parsed |

This exercises, on hardware, every layer built in Tasks 1–11: crypto, PB-GATT
provisioning, network obfuscation, upper/lower transport with segmentation,
the proxy protocol SAR, and the config foundation model — including the
sequence-number persistence and multi-proxy reconnection design.

## The blocker: no standard lighting models

Composition Data Page 0 of the lamp:

```
CID 0x07E9 (Häfele), PID 0x1510, VID 0x3005, CRPL 200, features 0x0003
element 0:
  SIG models:    0x0000 (Config Server), 0x0002 (Health Server),
                 0x1013, 0x1200, 0x1201, 0x1202  (property/time infrastructure)
  vendor models: 0x07E9:0x1000, 0x07E9:0x1002,
                 0x07E9:0x1006, 0x07E9:0x100B
```

There is **no Generic OnOff Server (0x1000) and no Light Lightness Server
(0x1300)** as SIG models. Config Model App Bind for 0x1000 returns
`0x02 Invalid Model`. All actual light control lives in the four Häfele
vendor models (company ID **0x07E9**), whose model numbers deliberately
mirror the generic SIG models (0x1000 OnOff, 0x1002 Level, 0x1006 Power
OnOff, 0x100B …).

The firmware is built on the **Telink SIG Mesh SDK**, whose vendor model
(`VENDOR_MD_LIGHT_S` / `VENDOR_MD_LIGHT_C`) uses a 3-octet vendor opcode
(`VD_GROUP_G_SET` / `_GET` / `_STATUS` / `_SET_NOACK`) plus a 1-octet
**sub-opcode** (`0x00–0x7F`, e.g. `VD_GROUP_G_ON`) that selects
on/off / luminance / temperature. Häfele registered their own company ID
(0x07E9) on top of this scheme.

## Why this is a CONDITIONAL GO, not a NO-GO

The hard, risky part — a working pure-Python SIG Mesh stack that provisions
and configures a real vendor node through an ESPHome proxy — is **done and
validated on hardware**. The remaining work is bounded: implement the Häfele
vendor model's opcode + sub-opcode set and a matching client model. That is
engineering + a bit of reverse engineering, not open-ended research.

## Getting the vendor opcodes (Phase 1, options)

Investigated 2026-07-19:

- ❌ **Häfele wall remote sniff** — user has no remote (controls via the phone
  app only).
- ❌ **Existing HA integrations** (guillaumeseur / qnimbus haefele_connect_mesh)
  — read their source: both proxy through the Häfele **gateway** (cloud REST
  or local MQTT) and send high-level JSON (`{"lightness": 0.75}`); the gateway
  firmware builds the vendor mesh message, so the raw vendor opcodes are never
  in their code. Also require the gateway the user does not have. Dead end.
  Useful semantic finding: control maps to OnOff + Lightness (0–65535), so the
  vendor models 0x1000/0x1002 mirror the generic/lightness semantics closely.

Remaining viable paths:

- **A — Probe our own lamp (no new tools, recommended first).** The lamp is in
  *our* mesh with *our* keys. Add vendor-model support to the stack: (1) Config
  Model App Bind for the **vendor** model `0x07E9:0x1000` (4-byte model ID form
  with company ID), then (2) send candidate vendor access messages
  (3-octet opcode `0b11xxxxxx E9 07` + Telink-style sub-op) and watch the lamp
  physically react. Small, bounded opcode space; fully legitimate; needs the
  user only to watch the lamp. Telink SDK vendor model = `VENDOR_MD_LIGHT_S`
  with `VD_GROUP_G_SET/_GET/_STATUS` + 1-octet sub-op (0x00–0x7F) — Häfele
  likely kept the Telink structure.
- **B — Capture the phone app + extract keys.** Factory-reset the lamp back
  into the Häfele app, capture the app↔lamp GATT traffic (Android HCI snoop
  log), and decrypt it with the app's netkey/appkey (extractable from the
  app's own data on the user's phone). Yields the exact bytes Häfele sends,
  but needs phone tooling and key extraction. Fallback if A stalls.
- **C — Telink SIG Mesh SDK headers** (`vendor/common/vendor_model.h`) for the
  `VD_GROUP_G_*` numeric values, to seed the probe in A. Obtain via the Telink
  wiki SDK zip.

## Fixes made during this run (all committed, 203 tests green)

- `bleak-retry-connector` for GATT connect + full connect/subscribe retry cycle.
- Connect as soon as the target 0x1827 beacon appears (Telink accept-window timing).
- Select No OOB when the device offers only output/input OOB.
- Composition Data Get enumeration + Light Lightness fallback + vendor-only NO-GO diagnostic.
- `--toggle-only` reconfigures until a bind has actually succeeded.

## Recommendation

Proceed to **Phase 1**, but re-scope its first milestone to **"decode and
drive the Häfele vendor model"** before the library extraction. Start with
option 1 (sniff a Häfele remote inside our own network) or option 2 (Telink
SDK headers). Once one vendor on/off message toggles the lamp, the go/no-go is
a full GO and the library/HA-integration roadmap resumes as designed.
