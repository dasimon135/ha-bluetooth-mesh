# Per-lamp Light CTL inversion

*2026-08-28 — closes the second half of issue #7.*

## The problem

The integration mirrors the requested Light CTL temperature around the exposed
Kelvin range for every node whose company identifier is Häfele's:

```python
_INVERTED_CTL_CIDS = frozenset({0x07E9})  # Häfele / ThingOS
```

The mirror exists because the Häfele/ThingOS lamps seen so far map temperature
inversely: asking for warm produces cool. Issue #7 produced the first lamp that
contradicts the rule. Its reporter has a Häfele Mesh Box that shows warm white
when Home Assistant says cool — and from here that symptom has two mutually
exclusive causes, which no diagnostic dump can tell apart:

1. the lamp reports CID `0x07E9`, we mirror, and the lamp is in fact
   spec-conformant — so *we* invert it; or
2. the lamp reports some other CID, we do not mirror, and the lamp is natively
   inverted — so the vendor list is missing an entry.

Case 2 would be a one-line addition. Case 1 says the quirk varies *within* a
vendor, by model or by firmware, and that a company identifier therefore cannot
predict it. A per-lamp option is correct under both, and needs no answer from
the reporter to ship.

It also settles a case the vendor list cannot express at all: one network
holding both a lamp that needs the mirror and a lamp that must not have it.

## The decision moves from the vendor to the lamp

`const.py` gains the option key:

```python
CONF_INVERTED_CTL = "inverted_ctl"
```

Its value is a **list of unicast addresses** — a per-lamp boolean encoded as
presence or absence. A list rather than a dict because Home Assistant
serialises options to JSON, which has no integer keys: a `dict[int, bool]`
comes back with string keys after a restart, and the lookup silently misses.

`_INVERTED_CTL_CIDS` disappears from `light.py`, and with it the last place
where a company identifier decided a behaviour. `MeshLight` takes the answer as
a constructor argument:

```python
def __init__(self, coordinator, node, *, invert_ctl: bool) -> None:
```

and `_ctl_kelvin` reduces to `if not self._invert_ctl: return kelvin`.

Passing it in rather than having the entity read `entry.options` at call time:
the options flow is an `OptionsFlowWithReload`, so changing the option reloads
the entry and rebuilds every entity. A value frozen at construction is
therefore always current, and the entity keeps its single dependency on the
coordinator.

`_KNOWN_CIDS` and the *Manufacturer* field stay as they are. Displaying
"Häfele" remains true; deriving behaviour from it was the error.

## Seeding, once

An empty default would invert the colour temperature of every working Häfele
install on upgrade, unasked. So the first setup after the upgrade writes
today's behaviour into the option — in `__init__.py:async_setup_entry`, after
the coordinator has parsed the network and before the platforms are forwarded,
so the entities are built from the seeded value:

```python
if CONF_INVERTED_CTL not in entry.options:
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_INVERTED_CTL: [
                node.unicast
                for node in coordinator.network.nodes
                if node.has_model(MODEL_LIGHT_CTL)
                and node.cid == HAEFELE_COMPANY_ID
            ],
        },
    )
```

`HAEFELE_COMPANY_ID` already exists in `btmesh/access.py`; it is reused, just
no longer consulted at run time.

**The `not in` is load-bearing.** It separates *absent* (never seeded, so seed)
from *present but empty* (seeded, then unchecked by the user, so leave alone).
Testing the value for truth instead would re-check the reporter's lamp on every
restart and silently revert his choice. That defect is what ruled out the
alternative of seeding each newly-seen node, which needs a second persistent
set — the nodes already seeded — to avoid exactly the same bug.

A lamp imported *after* the seed arrives un-inverted whatever its CID. Two
Häfele lamps on one network can therefore end up configured differently
depending on when they were imported, and that is the honest position: #7
established that the CID does not predict the quirk, so neutral is the right
default for everything that follows.

## The form

`async_step_init` gains a third field, a `SelectSelector` with
`multiple=True`, listing every CTL-capable node by name:

```python
SelectOptionDict(value=f"{n.unicast:04x}", label=n.name or f"Mesh {n.unicast:04x}")
```

Values travel as hexadecimal — the form this repository writes unicasts in
everywhere — and are converted back to `int` on save. The flow reads the
network through `self.config_entry`, as the reconfigure step already does. A
network with no CTL lamp omits the field entirely rather than showing an empty
list that means nothing.

## Diagnostics

`diagnostics.py` lists options one by one, so the new one needs its own line
beside `src_addr` and `keepalive_seconds`. Without it, the next report of an
inverted lamp cannot say whether the inversion is ours — the ambiguity that
cost a round trip on #7.

## Tests

The existing colour-temperature tests assert the mirror from the fixture's CID
(`tests/ha/test_light.py:455`: *"The fixture node is Häfele (CID 0x07E9)"*).
That premise becomes false by construction; they are rewritten to drive
`invert_ctl` explicitly, which also makes them read better — the mirror no
longer depends on a fixture detail three levels away.

New tests, one per real risk:

* `_ctl_kelvin` mirrors when `invert_ctl=True` and passes the value through
  otherwise, **independently of the CID**: an unchecked Häfele lamp is not
  mirrored, and a checked non-Häfele lamp is. The rule is being inverted, so it
  is worth asserting in both directions.
* The seed writes only Häfele CTL lamps, and skips a Häfele node that hosts
  Generic OnOff alone.
* **The seed does not re-run against an empty list** — the regression test for
  the trap above: options `[]`, set up again, still `[]`.
* The options flow stores `int`s from the selector's hex strings (round trip).

## Not in scope

Per-lamp minimum and maximum Kelvin. The exposed range is a fixed 2700–6500 K
default (`DEFAULT_MIN_KELVIN` / `DEFAULT_MAX_KELVIN`) rather than the lamp's
real limits, which is a separate and so far unreported inaccuracy. Adding it
here would turn a single checkbox into a per-lamp settings sub-flow for no
present complaint.
