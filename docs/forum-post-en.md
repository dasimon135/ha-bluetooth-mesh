<!--
Final text for community.home-assistant.io t/1018107, post #1.

Merges the GitHub-funnel banner already live there (added 2026-09-06) with a
corrected body. Two claims in the live post are out of date:

* "Optimistic state: parallel changes from the vendor app aren't read back" has
  been false since v0.2.0 (2026-07-26) — state IS read back, and the manifest
  declares `iot_class: local_polling`;
* colour temperature has been read from the lamp, along with its real Kelvin
  range, since v0.6.0 (2026-08-29).

The live post also lost its paragraph breaks at some point (every line is
followed by a blank one, which splits the bullet lists into separate
paragraphs). Pasting this file replaces that too.

David edits the post; nothing here is auto-published.
-->

> ### 📍 News and support live on GitHub
>
> I maintain this integration alone and unpaid, so everything is tracked in one place: **[report a bug or ask a question](https://github.com/dasimon135/ha-bluetooth-mesh/issues/new/choose)**.
>
> New versions no longer need announcing here: **HACS offers you the update**, release notes included. Otherwise, the RSS feed `https://github.com/dasimon135/ha-bluetooth-mesh/releases.atom`, or **Watch → Releases** on the repository.
>
> This thread stays open for user-to-user help. The [README](https://github.com/dasimon135/ha-bluetooth-mesh#readme) is the reference and stays current — this post does not.

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
- **State is read from the lamp, not assumed.** A mesh proxy forwards nothing
  inbound until the client configures its address filter; the integration
  configures it, so on/off, brightness and colour temperature are read back when
  the mesh becomes reachable and after every reconnection — **including changes
  you made from the vendor app while Home Assistant was away**. A light whose
  state has not been read yet reports `unknown` rather than guessing `off`.
- The lamp is asked once for the **Kelvin range it actually tracks**, so the
  slider offers that lamp's real extremes instead of a conventional 2700–6500
  guess.
- Instant response (the proxy connection is kept alive; configurable timeout if
  you also want to keep using the vendor app — a mesh node has a single proxy
  slot).

### Honest limitations

- **No RGB / full-colour** yet: my hardware is tunable-white only, so I left
  colour unimplemented rather than ship it untested. **If you have a colour mesh
  lamp and want to help validate, please shout** — it's a clean addition.
- **Provisioning still happens in the vendor app.** The integration joins an
  existing network from its `.connect` export; adding a brand-new lamp to the
  mesh from Home Assistant is not there yet.

### Install

HACS → custom repository (category *Integration*) → install → restart → add the
integration and paste your `.connect` export. Details and screenshots in the
README.

Feedback, testers, and issues welcome — especially anyone with ThingOS-based
lights that aren't Häfele, or colour hardware. 🙏
