"""Constants for the Bluetooth Mesh integration."""

from __future__ import annotations

DOMAIN = "bluetooth_mesh"

# Config entry keys (populated by the config flow in a later task).
CONF_NETWORK = "network"
CONF_NETWORK_NAME = "network_name"

# Raw ``.connect`` network export JSON, stored verbatim in the config entry.
# The runtime (B3) re-parses it via ``Network.from_connect`` at setup time.
CONF_CONNECT_JSON = "connect_json"

# Options-flow key: how long (seconds) to keep the proxy connection open after
# the last command before dropping it to free the lamp's single proxy slot.
# 0 = keep it always open (most responsive; the Häfele app then cannot connect
# to the lamp while the integration is loaded). A positive value trades a
# multi-second cold-start after that idle period for letting the vendor app
# reclaim the lamp when Home Assistant is not actively driving it.
CONF_KEEPALIVE = "keepalive_seconds"
DEFAULT_KEEPALIVE = 0

# Options-flow key: the unicast address the integration transmits FROM.
# 0 means "derive it" — the top of the unicast range, stepping down past any
# address the imported network already uses.
#
# It is overridable because the one thing an export cannot tell us is which
# address the vendor app gave ITSELF: exports carry no provisioner node. If that
# address happens to be ours, every message we send is discarded as a replay by
# nodes that hold a sequence number for it, before any model sees it — with
# nothing in any log to say so. Moving off it is the only way to find out.
CONF_SRC_ADDR = "src_addr"
DEFAULT_SRC_ADDR = 0

# Options-flow key: the unicast addresses whose Light CTL server maps colour
# temperature INVERSELY — asking for warm produces cool. The requested Kelvin is
# mirrored around the exposed range before being sent to those, and to those
# only: mirroring a spec-conformant lamp inverts warm and cool end to end.
#
# Per lamp, not per vendor. It was gated on the company identifier until 0.5.0,
# when issue #7 produced a Häfele lamp the mirror was itself inverting — so the
# quirk varies WITHIN a vendor, by model or firmware, and a CID cannot predict
# it. Stored as a list of addresses rather than a dict of booleans because
# options are serialised to JSON, which has no integer keys: a dict comes back
# keyed by strings after a restart and every lookup silently misses.
#
# Absent and empty mean different things. Absent is an entry that predates the
# option, and the first setup seeds it from the old vendor rule so no working
# install flips on upgrade; empty is a user who unchecked every lamp, and must
# be left alone.
CONF_INVERTED_CTL = "inverted_ctl"

# SIG model identifiers the integration drives (spec Mesh Model §6/§7). They
# live here rather than in ``light.py`` because the coordinator needs them too:
# the AppKey to encrypt with is the one THESE models are bound to.
MODEL_GENERIC_ONOFF = 0x1000
MODEL_LIGHT_LIGHTNESS = 0x1300
MODEL_LIGHT_CTL = 0x1303
# The Light CTL Temperature server lives on its OWN (secondary) element and sets
# temperature WITHOUT touching lightness — unlike Light CTL Set on element 0.
MODEL_LIGHT_CTL_TEMP = 0x1306

# Every model a command may be addressed to. Used to resolve which application
# key the network binds to the things we actually drive.
CONTROLLED_MODEL_IDS = (
    MODEL_GENERIC_ONOFF,
    MODEL_LIGHT_LIGHTNESS,
    MODEL_LIGHT_CTL,
    MODEL_LIGHT_CTL_TEMP,
)
