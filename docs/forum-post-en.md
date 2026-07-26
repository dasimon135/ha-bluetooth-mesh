<!--
Draft for community.home-assistant.io → "Share your Projects!"
Not auto-posted. Copy/paste and adjust the screenshots/links before posting.
Suggested title: [Custom integration] Bluetooth Mesh lighting (Häfele Connect
Mesh / ThingOS) — no vendor gateway, uses your ESPHome proxies
-->

## Bluetooth Mesh lighting in Home Assistant — no vendor gateway

Home Assistant has no native **Bluetooth SIG Mesh** support, which strands whole
families of "app-only" mesh lights — including **Häfele Connect Mesh (Loox)**
and other **ThingOS**-based luminaires. The usual options are a discontinued
vendor gateway, or an experimental BlueZ `bluetooth-meshd` setup that won't run
on Home Assistant OS.

So I built a small **pure-Python Bluetooth Mesh stack** plus a **HACS custom
integration** that drives these lamps directly — **using the ESPHome Bluetooth
proxies you probably already have** (or a local adapter). No extra hardware, no
meshd, works on HA OS in a VM with no local radio.

👉 **Repo:** https://github.com/dasimon135/ha-bluetooth-mesh

### How it works

The vendor app exports its network as a `.connect` file (NetKey, AppKey, node
addresses). The integration imports it, connects a proxy to the **same**
network, and sends **standard, app-keyed** SIG mesh messages (Generic OnOff,
Light Lightness, Light CTL). One GATT connection to any one powered lamp reaches
the whole mesh — the network relays the rest.

It's validated end-to-end on real hardware: a Häfele tunable-white lamp
controlled from HA through an ESPHome proxy, with a kept-alive connection that
makes commands feel instant.

### What works today

- On/off, **brightness**, and **colour temperature** (tunable white), one HA
  `light` per node.
- Instant response (the proxy connection is kept alive; configurable timeout if
  you also want to keep using the vendor app — a mesh node has a single proxy
  slot).

### Honest limitations

- **No RGB / full-colour** yet: my hardware is tunable-white only, so I left
  colour unimplemented rather than ship it untested. **If you have a colour mesh
  lamp and want to help validate, please shout** — it's a clean addition.
- Colour temperature is not read back from the lamp yet, so it reflects the last
  command. On/off and brightness *are* read from the mesh — including changes
  you made from the vendor app while HA was away.

### Install

HACS → custom repository (category *Integration*) → install → restart → add the
integration and paste your `.connect` export. Details and screenshots in the
README.

Feedback, testers, and issues welcome — especially anyone with ThingOS-based
lights that aren't Häfele, or colour hardware. 🙏
