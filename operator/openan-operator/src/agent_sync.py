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
marked so the delete handler does not echo them to the registry.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .agent_manifests import COMPONENT_LABEL
from .cards import card_owner

ORIGIN_LABEL, ORIGIN_REGISTRY = "openan.dev/origin", "registry"
REGISTRY_LABEL = "openan.dev/registry"            # short hash of the source registry id
MANAGED_BY = ("app.kubernetes.io/managed-by", "openan-operator")
A_SOURCE = "openan.dev/source-registry"           # "<namespace>/<registry Component>"
A_CARD_KEY = "openan.dev/card-key"                # "<organization>/<agent name>"
A_SPEC_HASH = "openan.dev/spec-hash"
A_OP_DELETE = "openan.dev/operator-delete"        # set before every operator-initiated delete
# Annotations on registry Components (set by whoever installs a connected registry's proxy Component).
A_CREDENTIALS = "openan.dev/credentials-secret"
A_PUBLISH = "openan.dev/agent-publish"
A_DEPLOY = "openan.dev/agent-deploy"
# Annotations on Kubernetes-made agents.
A_KEEP = "openan.dev/keep-on-card-delete"         # on the Component: do not delete it when its card is deleted
REGISTERED_CARDS = "openan.dev/registered-cards"  # on the ExposedAPI: {"<registry id>": "<org>/<name>"} (non-managed)

REGISTRY_IDS = ("TMFC012", "OPENAN-RC")           # Resource Inventory components; never matched on the id alone
NATIVE_SPEC, TMF639_SPEC = "/registry-center.yaml", "/tmf639-a2a-agent-v5.yaml"
NATIVE_API_NAME = "agentregistry"
PUBLISHED = "published"
DEREGISTERED = "AGENT_DEREGISTERED"
MASS_DELETE_MIN, MASS_DELETE_FRACTION = 5, 0.5


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
    annotations: Dict[str, str]
    deleting: bool

    @staticmethod
    def of(obj: Dict[str, Any]) -> "Live":
        md = obj.get("metadata") or {}
        return Live(md.get("name", ""), dict(md.get("labels") or {}), dict(md.get("annotations") or {}),
                    bool(md.get("deletionTimestamp")))


@dataclass
class Plan:
    apply: List[Tuple[str, Tuple[str, str], Dict[str, Any]]] = field(default_factory=list)  # (name, key, card)
    delete: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _true(value: Any) -> bool:
    return str(value).strip().lower() == "true"


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


def discover(component: Dict[str, Any], managed_name: Optional[str], managed_signing: bool,
             managed_publish: bool) -> Optional[RegistryRef]:
    md = component.get("metadata") or {}
    if md.get("deletionTimestamp") or (md.get("labels") or {}).get(ORIGIN_LABEL) == ORIGIN_REGISTRY:
        return None
    found = registry_flavour(component)
    if found is None:
        return None
    flavour, api = found
    impl, port, ns = api.get("implementation"), api.get("port"), md.get("namespace", "")
    if not impl or not isinstance(port, int):
        return None
    ann = md.get("annotations") or {}
    managed = md.get("name") == managed_name
    return RegistryRef(
        namespace=ns, component=md.get("name", ""), uid=md.get("uid", ""), flavour=flavour,
        base_url=f"http://{impl}.{ns}.svc.cluster.local:{port}", managed=managed,
        publish=flavour == "native" and (managed_publish if managed else _true(ann.get(A_PUBLISH))),
        deploy_allowed=managed_signing if managed else _true(ann.get(A_DEPLOY)),
        credentials_secret=None if managed else ann.get(A_CREDENTIALS))


def _slug(value: str, size: int) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")[:size].strip("-")


def object_name(reg: RegistryRef, org: str, name: str) -> str:
    """<registry<=10>-<agent<=18>-<hash8>: DNS-1035 (starts with a letter), <= 38 characters so the Canvas'
    <component>-<api> names stay short, and collision-free: the hash covers the exact registry id and card key."""
    digest = hashlib.sha256(f"{reg.id}|{org}|{name}".encode()).hexdigest()[:8]
    parts = [p for p in (_slug(reg.component, 10), _slug(name, 18)) if p]
    out = "-".join(parts + [digest])
    return out if out[0].isalpha() else "a" + out[:37]


def card_key(card: Dict[str, Any]) -> Tuple[str, str]:
    return ((card.get("provider") or {}).get("organization", ""), card.get("name", ""))


def is_operator_card(card: Dict[str, Any]) -> bool:
    """P: published by an OpenAN operator from an ExposedAPI (never mirrored back as a Component)."""
    return card_owner(card) is not None


def is_registry_originated(component: Optional[Dict[str, Any]]) -> bool:
    """Q: made by the registry -> Kubernetes sync."""
    return ((component or {}).get("metadata", {}).get("labels") or {}).get(ORIGIN_LABEL) == ORIGIN_REGISTRY


def labels_for(reg: RegistryRef, name: str) -> Dict[str, str]:
    return {COMPONENT_LABEL: name, ORIGIN_LABEL: ORIGIN_REGISTRY, REGISTRY_LABEL: reg.tag, MANAGED_BY[0]: MANAGED_BY[1]}


def annotations_for(reg: RegistryRef, org: str, name: str) -> Dict[str, str]:
    return {A_SOURCE: reg.id, A_CARD_KEY: f"{org}/{name}"}


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
        if current is not None and (current.labels.get(ORIGIN_LABEL) != ORIGIN_REGISTRY
                                    or current.labels.get(REGISTRY_LABEL) != reg.tag):
            out.warnings.append(f"{obj} exists and was not made from {reg.id}: left alone")
            continue
        if current is not None and current.deleting:
            continue
        out.apply.append((obj, (org, name), card))
    for o in by_name.values():
        if (o.labels.get(ORIGIN_LABEL) == ORIGIN_REGISTRY and o.labels.get(REGISTRY_LABEL) == reg.tag
                and not o.deleting and o.name not in wanted):
            out.delete.append(o.name)
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


def deregistered(events: Iterable[Dict[str, Any]]) -> Set[Tuple[str, str]]:
    """Keys whose latest event in the batch is AGENT_DEREGISTERED (a later re-register cancels it). Deregistered
    also covers published -> pending: callers confirm the card is really gone from the full listing."""
    latest: Dict[Tuple[str, str], str] = {}
    for e in sorted(events, key=lambda e: e.get("registry_version", 0)):
        data = e.get("data") or {}
        if data.get("organization") and data.get("name"):
            latest[(data["organization"], data["name"])] = e.get("event_type", "")
    return {k for k, t in latest.items() if t == DEREGISTERED}
