<!--
Two drafts for community.home-assistant.io, for the v0.4.1 update.

⚠️ FIRST, edit post #1 of the announcement thread
   https://community.home-assistant.io/t/1018107
   It still says "Optimistic state: parallel changes from the vendor app aren't
   read back until HA's next command" — untrue since v0.2.0. Corrected text is
   in docs/forum-post-en.md.

A = follow-up reply in the announcement thread (1018107).
B = short cross-post in "Häfele BLE - Home Assistant Integration" (552982),
    5,700+ views and the thread that ranks for "Häfele Home Assistant". That is
    where the audience actually is — several people there solved this by
    replacing hardware. Keep B short and pointed; link to A for the detail.
-->

# A — follow-up in the announcement thread (1018107)

An update on this, because one change is worth explaining: **Home Assistant now
knows what the lamps are actually doing.**

Until now the state you saw was a guess — HA showed what it had last asked for,
not what the lamp was doing. Turn a light off from the Häfele app and HA would
happily keep showing it as on. I had assumed the lamps simply didn't answer.

They did. But in Bluetooth Mesh, the lamp you connect to also acts as a
switchboard for the rest of the network — and by default that switchboard
forwards **nothing** back to you. It waits to be told which addresses you care
about. The integration never told it, so every reply from every lamp was
discarded before it could reach us. A conversation where you're talking to
yourself without realising it.

It configures that filter now, and replies come back in **150–300 ms**:

- on/off and brightness are **read from the lamp** when the mesh becomes
  reachable, and again after every reconnection — **including whatever you
  changed from the vendor app while HA was away**;
- a lamp HA hasn't queried yet shows as `unknown` rather than a made-up `off`.

That second point sounds cosmetic. It isn't, and I found out the hard way: a
lamp of mine kept switching itself off after every HA restart. The culprit was
my own code — the entity came up announcing "off", another integration
(Magic Areas, which turns a room's lights off when it's empty) took that
invented value at face value and switched the lamp off for real. The invention
made itself true. And because the entity already read "off", the command left no
trace in the logbook. Invisible.

⚠️ One thing to know: if you have an automation testing `state == 'off'` on one
of these lights, it won't match during the brief window where the state is
`unknown` after a restart.

**Two bugs that could wedge the integration** are also fixed. The first left a
ghost Bluetooth connection attached to a lamp; since a mesh node accepts only
**one** connection at a time, nothing could connect afterwards — not HA, not the
vendor app — while the integration complained it couldn't find a proxy. It was
the one holding the slot. The second was quieter: when sending broke, the entity
still showed as available, the UI responded normally, and absolutely nothing
happened on the lamp, with no error anywhere.

**Also new:** a **Reconfigure** button (added a lamp and re-exported your
`.connect`? Paste it into the existing entry instead of deleting and recreating
everything), and **diagnostics** you can download and paste straight into an
issue — they carry no network keys, which is enforced by a test. Under the hood
the integration now tracks a network counter it used to assume was fixed; when
that changes on a live mesh, the old version would have gone deaf permanently
and silently.

All of it validated on real hardware at each step, which is what actually
mattered: most of the above never showed up in the test suite, only in
deployment.

**Update:** HACS, then restart. Nothing to reconfigure, nothing to recreate.

**Still no colour.** My hardware is tunable-white only and I'd rather ship
nothing than ship untested colour. **If you have a colour mesh lamp, or a
ThingOS luminaire from a brand other than Häfele, I'd love a hand validating
it** — it's a clean addition, I just can't verify it.

https://github.com/dasimon135/ha-bluetooth-mesh


# B — cross-post in "Häfele BLE - Home Assistant Integration" (552982)

Reading back through this thread, most of the answers ended up being hardware:
swapping the Connect Mesh modules for Zigbee controllers, cutting the driver
cable, or putting a smart plug in front of the whole thing for on/off. Those are
legitimate fixes and they clearly work — but they're a lot of rewiring for
something that turns out to be solvable in software.

I posted here in July about a custom integration that drives Häfele Connect Mesh
lights from HA **without the gateway and without the cloud**, over the ESPHome
Bluetooth proxies most of us already run (a local BT adapter works too). It's
had a fair bit of work since, and the part that matters for this thread:

**HA now reads the real state off the lamps** — on/off and brightness, including
changes you made from the Connect Mesh app while HA wasn't looking. So the app
and HA stay in agreement instead of drifting apart.

How it avoids the gateway: the Connect Mesh app exports your network as a
`.connect` file (network keys and node addresses). The integration imports that,
connects a proxy to the *same* mesh, and speaks standard SIG mesh messages. One
GATT connection to any powered lamp reaches the whole network — the mesh relays
the rest. Your existing modules stay exactly where they are.

Validated end to end on tunable-white Häfele hardware: on/off, brightness and
colour temperature. **No RGB yet** — I don't own colour hardware to test
against, and I'd rather not ship it blind. If you kept your mesh modules and
have colour, testing help is very welcome.

https://github.com/dasimon135/ha-bluetooth-mesh
(details of the recent changes in the announcement thread:
https://community.home-assistant.io/t/1018107)
