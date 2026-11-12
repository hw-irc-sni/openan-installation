# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure logic of the agent registry <-> Agent ODA Component sync: registry discovery, naming, plans.

Loop freedom comes from two disjoint origins:
  P(card)      the card carries the signed source-exposedapi extension: published from this cluster (cards.py).
  Q(component) the Component carries label openan.dev/origin=registry: made by this module from a card.
Registry -> Kubernetes reads only cards with not P and writes only Components with Q; the ExposedAPI card sync
reads only ExposedAPIs whose parent has not Q and writes only cards with P. Neither side's output is the other's
input; content hashes keep steady state free of writes, deletes are idempotent and operator-initiated deletes are
marked so the delete handler does not echo them to the registry. Q is a routing hint, not proof: before anything
is deleted in a registry the object must also be owned by that registry Component and carry the hash the operator
rendered for the card (agent_sync_io.release).
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

from . import stack
from .agent_manifests import COMPONENT_LABEL
from .cards import ANNOTATION, card_owner, is_a2a

ORIGIN_LABEL, ORIGIN_REGISTRY = "openan.dev/origin", "registry"
REGISTRY_LABEL = "openan.dev/registry"            # short hash of the source registry id
MANAGED_BY_KEY, MANAGED_BY_VALUE = "app.kubernetes.io/managed-by", "openan-operator"
A_SOURCE = "openan.dev/source-registry"           # "<namespace>/<registry Component>"
A_CARD_KEY = "openan.dev/card-key"                # "<organization>/<agent name>"
A_SPEC_HASH = "openan.dev/spec-hash"
A_OP_DELETE = "openan.dev/operator-delete"        # set before every operator-initiated delete
# Annotations on registry Components (set by whoever installs a connected registry's proxy Component).
A_CREDENTIALS = "openan.dev/credentials-secret"
A_PUBLISH = "openan.dev/agent-publish"
# On the credentials Secret: the registry Component it belongs to and that Component's endpoint
# (http://<implementation>.<namespace>.svc.cluster.local:<port>); a Secret is only ever sent to exactly that.
A_FOR_REGISTRY = "openan.dev/registry-component"
A_FOR_ENDPOINT = "openan.dev/registry-endpoint"
# Annotations on Kubernetes-made agents.
A_KEEP = "openan.dev/keep-on-card-delete"         # on the Component: do not delete it when its card is deleted
# On the ExposedAPI, next to cards.ANNOTATION ("openan.dev/registered-card": "<org>/<name>" in the CR's own
# registry): {"<registry id>": "<org>/<name>"} for connected registries it was published to.
REGISTERED_CARDS = "openan.dev/registered-cards"

REGISTRY_IDS = ("TMFC012", "OPENAN-RC")           # Resource Inventory components; never matched on the id alone
NATIVE_SPEC, TMF639_SPEC = "/registry-center.yaml", "/tmf639-a2a-agent-v5.yaml"
NATIVE_API_NAME = "agentregistry"
PUBLISHED = "published"
DEREGISTERED = "AGENT_DEREGISTERED"
MASS_DELETE_MIN, MASS_DELETE_FRACTION = 5, 0.5

Key = Tuple[str, str]  # (organization, agent name)


class Managed(NamedTuple):
    """The OpenAN CR's own Registry Center: Component name, cards are signature-checked, cards are published."""
    name: str
    signing: bool
    publish: bool


@dataclass(frozen=True)
class RegistryRef:
    namespace: str
    component: str
    uid: str
    flavour: str               # "native" | "tmf639"
    base_url: str              # in-cluster Service, no path
    managed: bool              # the Registry Center of the OpenAN CR: operator-generated credentials
    publish: bool              # Kubernetes-made cards are published here (native only)
    deploy_allowed: bool       # cards may make the cluster run an (allow-listed) image
    credentials_secret: Optional[str] = None

    @property
    def id(self) -> str:
        return f"{self.namespace}/{self.component}"

    @property
    def tag(self) -> str:
        return hashlib.sha256(self.id.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class Live:
    name: str
    labels: Dict[str, str]
    deleting: bool

    @staticmethod
    def of(obj: Dict[str, Any]) -> "Live":
        md = obj.get("metadata") or {}
        return Live(md.get("name", ""), dict(md.get("labels") or {}), bool(md.get("deletionTimestamp")))

    def mine(self, reg: RegistryRef) -> bool:
        return made_by_registry(self.labels) and self.labels.get(REGISTRY_LABEL) == reg.tag


@dataclass
class Plan:
    apply: List[Tuple[str, Key, Dict[str, Any]]] = field(default_factory=list)  # (object name, key, card)
    delete: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---- small shared helpers ----------------------------------------------------------------------------------------

def is_true(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def made_by_registry(labels: Optional[Dict[str, str]]) -> bool:
    return (labels or {}).get(ORIGIN_LABEL) == ORIGIN_REGISTRY


def format_key(org: str, name: str) -> str:
    return f"{org}/{name}"


def parse_key(value: Optional[str]) -> Optional[Key]:
    org, _, name = (value or "").partition("/")
    return (org, name) if org and name else None


def card_key(card: Dict[str, Any]) -> Key:
    return ((card.get("provider") or {}).get("organization", ""), card.get("name", ""))


def registered_cards(meta: Dict[str, Any]) -> Dict[str, str]:
    """REGISTERED_CARDS of an ExposedAPI; {} for anything that is not a JSON object of strings."""
    try:
        value = json.loads((meta.get("annotations") or {}).get(REGISTERED_CARDS) or "{}")
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def published_ref(meta: Dict[str, Any], reg: RegistryRef) -> Optional[str]:
    """"<org>/<name>" the ExposedAPI published to `reg`, from the annotations the card sync writes."""
    if reg.managed:
        return (meta.get("annotations") or {}).get(ANNOTATION)
    return registered_cards(meta).get(reg.id)


def parent_component(meta: Dict[str, Any]) -> Optional[str]:
    """The Component an ExposedAPI belongs to: its ownerReference when the Canvas sets one, else the
    oda.tmforum.org/componentName label (the Canvas this was tested with sets no owner references)."""
    for ref in meta.get("ownerReferences") or []:
        if ref.get("kind") == "Component":
            return ref.get("name")
    return (meta.get("labels") or {}).get(COMPONENT_LABEL)


def managed_context(cr: Dict[str, Any]) -> Managed:
    spec = cr.get("spec") or {}
    card = spec.get("cardSync") or {}
    return Managed(stack.component_object_name(cr["metadata"]["name"], "registryCenter", "registrycenter"),
                   bool((card.get("signing") or {}).get("enabled", True)), bool(card.get("enabled", True)))


# ---- discovery and naming ----------------------------------------------------------------------------------------

def _spec_urls(api: Dict[str, Any]) -> List[str]:
    spec = api.get("specification")
    items = spec if isinstance(spec, list) else [spec] if isinstance(spec, dict) else []
    return [i.get("url", "") for i in items if isinstance(i, dict) and isinstance(i.get("url"), str)]


def registry_flavour(component: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """(flavour, exposedAPI) when the Component is an agent registry: a Resource Inventory component that exposes
    the native Registry Center API (preferred) or the TMF639 A2A-agent profile. Matching is on the specification URL,
    the key the Canvas itself binds APIs on: TMFC012/TMF639 alone also match generic inventories."""
    spec = component.get("spec") or {}
    if (spec.get("componentMetadata") or {}).get("id") not in REGISTRY_IDS:
        return None
    apis = (spec.get("coreFunction") or {}).get("exposedAPIs") or []
    for api in apis:
        if api.get("name") == NATIVE_API_NAME or any(u.endswith(NATIVE_SPEC) for u in _spec_urls(api)):
            return "native", api
    for api in apis:
        if any(u.endswith(TMF639_SPEC) for u in _spec_urls(api)):
            return "tmf639", api
    return None


def discover(component: Dict[str, Any], managed: Managed, deploy_registries: Tuple[str, ...] = ()) -> Optional[RegistryRef]:
    """`deploy_registries`: "<registry Component>=<endpoint>" entries of connected registries whose cards may run
    images. Operator configuration (AGENT_DEPLOY_REGISTRIES), never an annotation, and bound to the endpoint as well
    as the name: a Component re-created or re-pointed under the same name does not inherit the trust. The CR's own
    registry may deploy when it checks card signatures: its cards are then signed by keys the operator trusts."""
    md = component.get("metadata") or {}
    if md.get("deletionTimestamp") or made_by_registry(md.get("labels")):
        return None
    found = registry_flavour(component)
    if found is None:
        return None
    flavour, api = found
    impl, port, ns = api.get("implementation"), api.get("port"), md.get("namespace", "")
    if not impl or not isinstance(port, int):
        return None
    ann = md.get("annotations") or {}
    is_managed = md.get("name") == managed.name
    base_url = f"http://{impl}.{ns}.svc.cluster.local:{port}"
    return RegistryRef(
        namespace=ns, component=md.get("name", ""), uid=md.get("uid", ""), flavour=flavour,
        base_url=base_url, managed=is_managed,
        publish=flavour == "native" and (managed.publish if is_managed else is_true(ann.get(A_PUBLISH))),
        deploy_allowed=managed.signing if is_managed else f"{md.get('name')}={base_url}" in deploy_registries,
        credentials_secret=None if is_managed else ann.get(A_CREDENTIALS))


def _slug(value: str, size: int) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")[:size].strip("-")


def object_name(reg: RegistryRef, org: str, name: str) -> str:
    """<registry<=10>-<agent<=18>-<hash8>: DNS-1035 (starts with a letter), <= 38 characters so the Canvas'
    <component>-<api> names stay short, and collision-free: the hash covers the exact registry id and card key."""
    digest = hashlib.sha256(f"{reg.id}|{org}|{name}".encode()).hexdigest()[:8]
    parts = [p for p in (_slug(reg.component, 10), _slug(name, 18)) if p]
    out = "-".join(parts + [digest])
    return out if out[0].isalpha() else "a" + out[:37]


def is_operator_card(card: Dict[str, Any]) -> bool:
    """P: published by an OpenAN operator from an ExposedAPI (never mirrored back as a Component)."""
    return card_owner(card) is not None


def is_registry_originated(component: Dict[str, Any]) -> bool:
    """Q: made by the registry -> Kubernetes sync."""
    return made_by_registry((component.get("metadata") or {}).get("labels"))


def labels_for(reg: RegistryRef, name: str) -> Dict[str, str]:
    return {COMPONENT_LABEL: name, ORIGIN_LABEL: ORIGIN_REGISTRY, REGISTRY_LABEL: reg.tag, MANAGED_BY_KEY: MANAGED_BY_VALUE}


def annotations_for(reg: RegistryRef, org: str, name: str) -> Dict[str, str]:
    return {A_SOURCE: reg.id, A_CARD_KEY: format_key(org, name)}


# ---- plans -------------------------------------------------------------------------------------------------------

def plan(reg: RegistryRef, agents: Iterable[Tuple[Dict[str, Any], str]], live: Iterable[Live]) -> Plan:
    """Desired: one Component per published card that was not published from a cluster. Live objects of another
    origin or registry are never touched (name collision); objects being deleted (e.g. a refused card delete
    holding the finalizer) are neither re-created nor patched."""
    out = Plan()
    by_name = {o.name: o for o in live}
    wanted: Set[str] = set()
    for card, status in agents:
        if status != PUBLISHED or is_operator_card(card):
            continue
        org, name = card_key(card)
        if not org or not name:
            out.warnings.append(f"card without organization or name ignored: {json.dumps(card)[:80]}")
            continue
        obj = object_name(reg, org, name)
        wanted.add(obj)
        current = by_name.get(obj)
        if current is not None and not current.mine(reg):
            out.warnings.append(f"{obj} exists and was not made from {reg.id}: left alone")
            continue
        if current is not None and current.deleting:
            continue
        out.apply.append((obj, (org, name), card))
    out.delete = [o.name for o in by_name.values() if o.mine(reg) and not o.deleting and o.name not in wanted]
    return out


def guard_deletes(deletes: List[str], mine: int, pending: Optional[Set[str]]) -> Tuple[List[str], Optional[Set[str]]]:
    """Hold a pass that would remove most mirrors at once (e.g. a registry that came back empty after a storage
    reset) until a second consecutive pass asks for the same removals. Returns (deletes to apply now, new pending)."""
    if len(deletes) < MASS_DELETE_MIN or len(deletes) <= MASS_DELETE_FRACTION * mine:
        return deletes, None
    asked = set(deletes)
    if pending is not None and asked <= pending:
        return deletes, None
    return [], asked


def deregistered(events: Iterable[Dict[str, Any]]) -> Set[Key]:
    """Keys whose latest event in the batch is AGENT_DEREGISTERED (a later re-register cancels it). Deregistered
    also covers published -> pending: callers confirm the card is really gone from the complete listing."""
    latest: Dict[Key, str] = {}
    for e in sorted(events, key=lambda e: e.get("registry_version", 0)):
        data = e.get("data") or {}
        if data.get("organization") and data.get("name"):
            latest[(data["organization"], data["name"])] = e.get("event_type", "")
    return {k for k, t in latest.items() if t == DEREGISTERED}


def components_to_delete(reg: RegistryRef, exposedapis: Iterable[Dict[str, Any]],
                         components: Dict[str, Dict[str, Any]], gone: Set[Key]) -> List[str]:
    """Kubernetes-made Components whose cards in `reg` were all deleted: every a2a ExposedAPI of the Component must
    have published a card to `reg` that is in `gone` (a Component exposing two agents keeps living while one of them
    is still registered). Registry-made, terminating and keep-on-card-delete Components are never returned."""
    gone_refs = {format_key(*k) for k in gone}
    by_parent: Dict[str, List[Optional[str]]] = {}
    for api in exposedapis:
        meta = api.get("metadata") or {}
        if not is_a2a(api.get("spec") or {}) or meta.get("deletionTimestamp"):
            continue
        parent = parent_component(meta)
        if parent:
            by_parent.setdefault(parent, []).append(published_ref(meta, reg))
    out = []
    for parent, refs in sorted(by_parent.items()):
        comp = components.get(parent)
        cmeta = (comp or {}).get("metadata") or {}
        if (comp is None or cmeta.get("deletionTimestamp") or is_registry_originated(comp)
                or is_true((cmeta.get("annotations") or {}).get(A_KEEP))):
            continue
        if refs and all(r in gone_refs for r in refs):
            out.append(parent)
    return out
