# Reading the lamp's real colour temperature, and its real range

*2026-08-29 — closes the last "not read back yet" gap in the README, and the
hard-coded Kelvin range underneath it.*

## Two gaps that turn out to be one

Colour temperature is the only attribute this integration never reads back. The
README says so: *"colour temperature is not read back yet (there is no CTL
Temperature getter in the library), so it still reflects the last command."*
On/off and brightness have been read from the lamp since 0.2/0.3.

Underneath it sits a second approximation. The exposed Kelvin range is a pair of
constants:

```python
DEFAULT_MIN_KELVIN = 2700
DEFAULT_MAX_KELVIN = 6500
```

chosen as "a safe default rather than the raw model limits". That costs a lamp
its real extremes — they simply cannot be asked for — and it does something
less obvious as well. The inversion workaround mirrors around the **exposed**
range:

```python
self._attr_min_color_temp_kelvin + self._attr_max_color_temp_kelvin - kelvin
```

So on a lamp whose real range is not 2700–6500, the mirror shipped in 0.5.1 is
off-centre by twice the difference of the midpoints. The two gaps are one piece
of work: ask the lamp what it is, and both close.

## What the library already has

Less is missing than it looks. `LightCtlTemperatureStatus` and
`parse_light_ctl_temperature_status` exist and handle both the 4-byte and 9-byte
forms; `LightCtlStatus` and `parse_light_ctl_status` exist too. What is missing
is the asking:

```python
OP_LIGHT_CTL_GET                      = 0x825D
OP_LIGHT_CTL_TEMPERATURE_GET          = 0x8261
OP_LIGHT_CTL_TEMPERATURE_RANGE_GET    = 0x8262
OP_LIGHT_CTL_TEMPERATURE_RANGE_STATUS = 0x8263
```

Those are exactly the holes in the opcodes already present (`825E`, `825F`,
`8260`, `8264`–`8266`) — a free cross-check against a mistranscribed spec.

One new parser, `LightCtlTemperatureRangeStatus`: a status code byte then two
uint16 Kelvin. The status code carries weight. A node may answer something other
than Success, meaning *I have no valid range*, and treating that as an answer
would overwrite a sensible default with nothing. A non-zero code behaves as no
answer at all.

Three encoders and three controller getters, modelled on `get_lightness`:
`TimeoutError` → `logger.debug` → `None`. Then the coordinator pass-throughs.
Re-sync the vendored copy, or HACS ships the old library while every test passes.

## The entity

**The range is read once**, on the first successful `async_refresh_state`, and
fills `_attr_min_color_temp_kelvin` / `_attr_max_color_temp_kelvin`. The mirror
then pivots on it with no extra code, because it already pivots on those
attributes. A flag stops it being re-asked: this is a device property, not a
state.

Until that first read the constants stand, so a command issued in the seconds
before the mesh is reachable is mirrored on the default. On a 2700–6500 lamp
that is identical; on a wider one it is briefly off-centre. The alternative —
persisting the range in the config entry — buys a correct slider one restart
earlier at the price of duplicating a device property into our own storage,
where it can outlive a firmware change. Not worth it.

**The mirror becomes bidirectional, and has to be named for it.** The lamp
reports the value it was *sent*, which is the mirrored one; displaying it raw
would show a wrong Kelvin on precisely the lamps the option exists for. Since
`min + max - K` applied twice returns `K`, one involution serves both ways — but
`_ctl_kelvin` used in reverse reads as a bug. A private `_mirror()` with two
callers named for their direction, `_ctl_kelvin()` outbound and `_ha_kelvin()`
inbound.

**Mid-fade is detectable here**, unlike brightness. The existing parser already
returns `target_temperature` and `remaining_time`; a non-zero remaining time
means `present_temperature` is a value in transit, so the target is taken
instead. Brightness never had that information, which is why it carries a
blanket "never read back mid-fade" rule. There is no reason to imitate a
limitation we do not have.

**Addressing mirrors the send path.** Temperature Get goes to the 0x1306 element
when the node has one. When it does not — the case where sending already falls
back to `Light CTL Set` — reading falls back to `Light CTL Get`, whose Status is
already decoded. Without it a node with no 0x1306 element would be written but
never read, an asymmetry with no defensible explanation.

**No on/off gate** for temperature. Brightness is skipped on an off lamp because
an off lamp reports lightness 0, which is not a brightness worth showing. A
temperature is held across off/on and has no such problem.

## Diagnostics

The range joins the per-node probe rather than the `state` block: one more
request for CTL nodes, beside the relay and composition. What the lamp *claims*
is worth more than what an entity cached, and without it an off-centre mirror
would be undiagnosable — the blind spot that cost a round trip on #7. The probe
is already capped at six nodes, so the cost stays bounded.

## Tests

Library: the three encoders byte for byte; the range parser against Success, a
non-Success code, and a wrong length; `None` on timeout.

Entity:

* the range is read **once**, not on every refresh;
* the mirror pivots on the range that was read, not on the constants;
* reading **un-mirrors** — a ticked lamp displays the Kelvin that was asked for,
  not the one that was sent;
* a non-zero `remaining_time` takes `target_temperature`;
* with no 0x1306 element, reading goes through `Light CTL Get`.

And the one carrying the promise: **a lamp whose real range is 2700–6500 sends
exactly what it sent before.** That is the "nothing changes on upgrade"
guarantee, and it is asserted rather than hoped for.

## Release

Version 0.6.0. The README loses its "not read back yet" limitation, which is the
point of the whole exercise.

Per `docs/release-flow.md`: a release candidate, hardware-validated, then the
tag. The rc's checks:

1. does the slider's range change on the lamp, and to what?
2. does the temperature survive a restart, and follow a change made from the
   vendor app?
3. are the colours unchanged?

Check 1 is the interesting one. If the lamp reports anything other than
2700–6500, the mirror was already off-centre there, and check 3 becomes the one
that matters.
