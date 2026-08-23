"""Static mesh-network model imported from a ThingOS ``.connect`` export.

The btmesh stack rides on a mesh network provisioned by another app (the
Häfele Connect Mesh / ThingOS app). That app exports the network — NetKey,
AppKey, and every node's composition (unicast, elements, models) — as a
``.connect`` JSON file. This module parses that file into an immutable model
so the runtime knows which key to use and which element/unicast to address.

The model here is deliberately DISTINCT from :mod:`btmesh.node`:

* :class:`btmesh.node.MeshNode` is the *runtime* — it sends/receives access
  messages over a subnet.
* The classes here (:class:`Network`, :class:`Node`, :class:`Element`,
  :class:`Model`) are *static descriptors* parsed from the ``.connect`` file.
  They are named without the ``Mesh`` prefix precisely to avoid colliding with
  the runtime ``MeshNode`` when both are imported together.

Composition maps onto the SIG Mesh Profile §4.2 model hierarchy: a node owns
one or more elements (each with a unicast address = node base + element index,
§3.4.2), and each element hosts SIG or vendor models (§4.2.1) with a set of
bound application-key indexes.
"""

import json
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from .crypto import k3
from .errors import BtMeshError

__all__ = [
    "NetworkModelError",
    "AppKey",
    "Model",
    "Element",
    "Node",
    "Network",
]

# A unicast address identifies one element (spec §3.4.2); 0x0000 is unassigned
# and everything from 0x8000 up is a group or virtual address.
UNICAST_MIN = 0x0001
UNICAST_MAX = 0x7FFF


logger = logging.getLogger(__name__)


class NetworkModelError(BtMeshError):
    """A ``.connect`` document was missing required fields or malformed."""


@dataclass(frozen=True)
class AppKey:
    """One application key from the export (spec §3.8.6.3).

    ``index`` is the key's AppKey Index — the number a model's ``bind`` list
    refers to. It is what makes a key usable: a message encrypted with a key a
    model was never bound to is discarded by the node, silently.
    """

    index: int
    key: bytes
    name: str = ""


@dataclass(frozen=True)
class Model:
    """One SIG or vendor model on an element (spec §4.2.1).

    ``model_id`` is the numeric model identifier: a SIG model is ``<= 0xFFFF``
    (4-hex-char ``modelId`` in the file); a vendor model packs the 16-bit
    company identifier in the high half and the 16-bit vendor-assigned model id
    in the low half (``company << 16 | model``), i.e. the 8-hex-char ``modelId``
    ``"07E91000"`` parses directly to ``0x07E91000``.
    """

    model_id: int
    bound_appkey_indexes: tuple[int, ...]

    @property
    def is_vendor(self) -> bool:
        """True for a vendor (company-specific) model, False for a SIG model."""
        return self.model_id > 0xFFFF


@dataclass(frozen=True)
class Element:
    """One element of a node (spec §4.2), addressed by its own unicast.

    ``unicast`` is the node's base unicast plus ``index`` (§3.4.2): element 0 is
    the node's primary address, element 1 is base+1, and so on.
    """

    index: int
    unicast: int
    models: tuple[Model, ...]

    def has_model(self, model_id: int) -> bool:
        """Whether this element hosts the model ``model_id``."""
        return any(m.model_id == model_id for m in self.models)


@dataclass(frozen=True)
class Node:
    """A provisioned node described by the ``.connect`` composition.

    ``unicast`` is the primary (element-0) address. Distinct from the runtime
    :class:`btmesh.node.MeshNode`; this is a static descriptor only.
    """

    uuid: str
    unicast: int
    device_key: bytes
    cid: int
    name: str
    elements: tuple[Element, ...]

    def has_model(self, model_id: int) -> bool:
        """Whether any element of this node hosts ``model_id``."""
        return any(e.has_model(model_id) for e in self.elements)

    def element_for_model(self, model_id: int) -> "Element | None":
        """The first element hosting ``model_id`` (lowest index), or ``None``."""
        for element in self.elements:
            if element.has_model(model_id):
                return element
        return None


@dataclass(frozen=True)
class Network:
    """A mesh network parsed from a ThingOS ``.connect`` export.

    Phase 1 rides on the first NetKey/AppKey pair (the app only provisions one
    subnet). IV Index is absent from the export and defaults to 0, matching the
    Phase-0 hardware ground truth.
    """

    name: str
    uuid: str
    net_key: bytes
    net_key_index: int
    app_key: bytes
    app_key_index: int
    iv_index: int
    nodes: tuple[Node, ...]
    # Every application key the export carries. ``app_key`` / ``app_key_index``
    # above name the FIRST one, which is not necessarily the one the models we
    # want to drive are bound to — see :meth:`app_key_for_models`. Defaulted so
    # a Network built by hand (tests, fixtures) stays valid.
    app_keys: tuple[AppKey, ...] = ()

    @property
    def identifier(self) -> str:
        """A stable identity for this network, even without a ``meshUUID``.

        Exports do not all carry one, and an empty string made every such
        network collide. ``k3(NetKey)`` — the Network ID nodes advertise — is
        the subnet's real on-air identity, so it is the honest fallback.
        """
        return self.uuid or k3(self.net_key).hex()

    # ------------------------------------------------- application keys

    def bound_app_key_indexes(self, model_ids: Iterable[int]) -> tuple[int, ...]:
        """AppKey indexes the export binds to ``model_ids``, most-bound first.

        Reported rather than merely used so a caller can explain itself: an
        index listed here that the export has no key for is exactly the case
        where nothing we send can ever be understood, and saying so beats
        failing in silence.
        """
        wanted = frozenset(model_ids)
        counts: Counter[int] = Counter()
        for node in self.nodes:
            for element in node.elements:
                for model in element.models:
                    if model.model_id in wanted:
                        counts.update(model.bound_appkey_indexes)
        return tuple(
            index for index, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )

    def app_key_for_models(self, model_ids: Iterable[int]) -> AppKey:
        """The AppKey to encrypt messages for ``model_ids`` with.

        A node checks the AID of an incoming access message against the keys
        each of its models was bound to; a message encrypted with any other key
        is discarded at the upper transport layer without a word — no error, no
        Status, no physical effect. So the key is chosen from what the models
        actually bind rather than by taking the first key in the export and
        hoping.

        Falls back to the first key when the export binds nothing (or binds an
        index it carries no key for), which is the only usable guess left.
        """
        held = {key.index: key for key in self.app_keys}
        for index in self.bound_app_key_indexes(model_ids):
            if index in held:
                return held[index]
        return self.default_app_key

    @property
    def default_app_key(self) -> AppKey:
        """The export's first application key."""
        if self.app_keys:
            return self.app_keys[0]
        return AppKey(index=self.app_key_index, key=self.app_key)

    # ---------------------------------------------------- source addressing

    def unicast_addresses(self) -> frozenset[int]:
        """Every element address the export accounts for (spec §3.4.2)."""
        return frozenset(
            element.unicast for node in self.nodes for element in node.elements
        )

    def free_unicast(self, preferred: int = UNICAST_MAX) -> int:
        """``preferred`` if no imported element owns it, else the next one down.

        Transmitting from an address that already belongs to a node of the mesh
        is fatal in silence: that node's peers hold a replay-protection entry
        for the address, our sequence cursor starts far below whatever it
        reached, and every message we send is discarded as a replay. Nothing is
        logged anywhere, the lamp simply never reacts.

        Counting DOWN from the preferred address keeps us clear of the range a
        provisioner hands out next, which it allocates upwards.
        """
        if not UNICAST_MIN <= preferred <= UNICAST_MAX:
            raise NetworkModelError(
                f"{preferred:#06x} is not a unicast address "
                f"({UNICAST_MIN:#06x}..{UNICAST_MAX:#06x})"
            )
        taken = self.unicast_addresses()
        for candidate in range(preferred, UNICAST_MIN - 1, -1):
            if candidate not in taken:
                return candidate
        raise NetworkModelError("the export leaves no free unicast address")

    @classmethod
    def from_connect(cls, data: dict) -> "Network":
        """Parse a decoded ``.connect`` JSON document into a :class:`Network`.

        Raises :class:`NetworkModelError` on any missing required field or bad
        hex value.
        """
        if not isinstance(data, dict):
            raise NetworkModelError("connect document must be a JSON object")

        net_key_entry = _first(data, "netKeys")
        app_key_entry = _first(data, "appKeys")

        net_key = _hex_bytes(net_key_entry, "key", "netKeys[0].key")
        app_key = _hex_bytes(app_key_entry, "key", "appKeys[0].key")
        app_keys = _parse_app_keys(data["appKeys"])

        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list):
            raise NetworkModelError("connect document has no 'nodes' list")

        # One malformed entry must not sink the whole import. Exports come from
        # another vendor's app, and a single node missing a field it never
        # promised (a provisioner record, a firmware quirk) used to make the
        # entire network unusable with no way to tell which one was at fault.
        nodes = []
        skipped: list[str] = []
        for raw_node in raw_nodes:
            try:
                nodes.append(_parse_node(raw_node))
            except NetworkModelError as exc:
                name = (
                    raw_node.get("UUID", "?")
                    if isinstance(raw_node, dict)
                    else "?"
                )
                skipped.append(f"{name}: {exc}")
        if skipped:
            logger.warning(
                "skipped %d unparseable node(s) in the connect export: %s",
                len(skipped), "; ".join(skipped),
            )
            if not nodes:
                # Every node was dropped: importing an empty network would
                # look like success and expose nothing.
                raise NetworkModelError(
                    "no usable node in the connect export — "
                    + "; ".join(skipped)
                )

        return cls(
            name=str(data.get("meshName", "")),
            uuid=str(data.get("meshUUID", "")),
            net_key=net_key,
            net_key_index=_as_int(net_key_entry.get("index", 0), "netKeys[0].index"),
            app_key=app_key,
            app_key_index=_as_int(app_key_entry.get("index", 0), "appKeys[0].index"),
            iv_index=_as_int(data.get("ivIndex", 0), "ivIndex"),
            nodes=tuple(nodes),
            app_keys=app_keys,
        )

    @classmethod
    def from_connect_file(cls, path: str) -> "Network":
        """Load a ``.connect`` file from ``path`` and parse it (see
        :meth:`from_connect`)."""
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise NetworkModelError(f"cannot read connect file {path!r}: {exc}") from exc
        return cls.from_connect(data)


# --------------------------------------------------------------------- helpers


def _first(data: dict, field: str) -> dict:
    """Return the first entry of a required non-empty list field."""
    entries = data.get(field)
    if not isinstance(entries, list) or not entries:
        raise NetworkModelError(f"connect document has no '{field}' entry")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise NetworkModelError(f"'{field}[0]' is not an object")
    return entry


def _parse_app_keys(entries: list) -> tuple[AppKey, ...]:
    """Parse every ``appKeys[]`` entry; the first one is already validated."""
    keys = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise NetworkModelError(f"'appKeys[{position}]' is not an object")
        keys.append(
            AppKey(
                index=_as_int(entry.get("index", 0), f"appKeys[{position}].index"),
                key=_hex_bytes(entry, "key", f"appKeys[{position}].key"),
                name=str(entry.get("name", "")),
            )
        )
    return tuple(keys)


def _hex_bytes(entry: dict, key: str, label: str) -> bytes:
    """Decode a required hex-string field into bytes."""
    value = entry.get(key)
    if not isinstance(value, str):
        raise NetworkModelError(f"missing or non-string '{label}'")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise NetworkModelError(f"'{label}' is not valid hex: {value!r}") from exc


def _as_int(value: object, label: str) -> int:
    """Coerce an int-ish JSON value into an int."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise NetworkModelError(f"'{label}' must be an integer, got {value!r}")
    return value


def _hex_int(value: object, label: str) -> int:
    """Parse a hex-string field (e.g. ``unicastAddress`` "000C") into an int."""
    if not isinstance(value, str):
        raise NetworkModelError(f"missing or non-string '{label}'")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise NetworkModelError(f"'{label}' is not valid hex: {value!r}") from exc


def _node_name(node: dict) -> str:
    """Friendly name: ``tos_devices[0].name`` else ``tos_node.type`` else ""."""
    tos_devices = node.get("tos_devices")
    if isinstance(tos_devices, list) and tos_devices:
        name = tos_devices[0].get("name") if isinstance(tos_devices[0], dict) else None
        if isinstance(name, str) and name:
            return name
    tos_node = node.get("tos_node")
    if isinstance(tos_node, dict):
        type_ = tos_node.get("type")
        if isinstance(type_, str) and type_:
            return type_
    return ""


def _parse_model(raw: dict) -> Model:
    """Parse one ``models[]`` entry.

    ``modelId`` is a hex string: 4 hex chars → SIG model, 8 hex chars → vendor
    model (already ``company << 16 | model`` when parsed as one integer).
    """
    if not isinstance(raw, dict):
        raise NetworkModelError("element model entry is not an object")
    model_id = _hex_int(raw.get("modelId"), "models[].modelId")
    bind = raw.get("bind", [])
    if not isinstance(bind, list):
        raise NetworkModelError(f"'bind' must be a list, got {bind!r}")
    indexes = tuple(_as_int(b, "models[].bind[]") for b in bind)
    return Model(model_id=model_id, bound_appkey_indexes=indexes)


def _parse_element(base_unicast: int, raw: dict) -> Element:
    """Parse one ``elements[]`` entry; unicast = base + element index (§3.4.2)."""
    if not isinstance(raw, dict):
        raise NetworkModelError("element entry is not an object")
    index = _as_int(raw.get("index", 0), "elements[].index")
    models = raw.get("models", [])
    if not isinstance(models, list):
        raise NetworkModelError(f"element 'models' must be a list, got {models!r}")
    return Element(
        index=index,
        unicast=base_unicast + index,
        models=tuple(_parse_model(m) for m in models),
    )


def _parse_node(raw: dict) -> Node:
    """Parse one ``nodes[]`` entry into a :class:`Node`."""
    if not isinstance(raw, dict):
        raise NetworkModelError("node entry is not an object")
    base_unicast = _hex_int(raw.get("unicastAddress"), "nodes[].unicastAddress")
    device_key = _hex_bytes(raw, "deviceKey", "nodes[].deviceKey")
    cid = _hex_int(raw.get("cid"), "nodes[].cid")
    raw_elements = raw.get("elements", [])
    if not isinstance(raw_elements, list):
        raise NetworkModelError(
            f"node 'elements' must be a list, got {raw_elements!r}"
        )
    elements = tuple(_parse_element(base_unicast, e) for e in raw_elements)
    return Node(
        uuid=str(raw.get("UUID", "")),
        unicast=base_unicast,
        device_key=device_key,
        cid=cid,
        name=_node_name(raw),
        elements=elements,
    )
