# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Side effects of the agent sync: observe a registry, apply the pure plan (agent_sync) to the cluster, release
deletes. The Kubernetes API objects come in as callables so the handlers' _core/_custom/_apps seams stay the only
place tests have to patch."""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx
from kubernetes import client

from . import agent_manifests as am
from . import agent_sync as sync
from .cards import ANNOTATION, decode_secret, is_a2a
from .registry_client import RegistryClient, RegistryError
from .registry_source import NativeSource, SourceUnavailable, Tmf639Source

ODA = "oda.tmforum.org"
LOG = logging.getLogger("openan.agent-sync")
CREDENTIALS_LABEL = "openan.dev/registry-credentials"
SYNC_REQUIRED = "SYNC_REQUIRED"


@dataclass
class State:
    cursor: Optional[int] = None
    last_pass: float = 0.0
    down: bool = False
    pending_deletes: Optional[Set[str]] = None


STATE: Dict[str, State] = {}  # registry id -> state; in memory: a restart starts at the feed head with a full pass


@dataclass(frozen=True)
class Settings:
    image_prefixes: Tuple[str, ...]
    proxy_image: str
    upstream_suffixes: Tuple[str, ...] = ()
    deploy_registries: Tuple[str, ...] = ()
    resync_seconds: int = 300
    tmf639_seconds: int = 60


@dataclass
class Apis:
    core: Callable[[], Any]
    custom: Callable[[], Any]
    apps: Callable[[], Any]


@dataclass
class PassResult:
    applied: Dict[str, str] = field(default_factory=dict)   # name -> created|updated|unchanged
    deleted: List[str] = field(default_factory=list)
    k8s_deleted: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    ran: bool = False


class DeleteRefused(RuntimeError):
    """The registry will not delete the card (e.g. owned by another identity): keep the Component's finalizer."""


def _get(fn: Callable, *args) -> Optional[Any]:
    try:
        return fn(*args)
    except client.ApiException as e:
        if e.status == 404:
            return None
        raise


# ---- discovery and credentials ---------------------------------------------------------------------------------

def managed_context(cr: Optional[Dict[str, Any]]) -> Tuple[Optional[str], bool, bool]:
    """(name of the CR's Registry Center Component, its cards are signed, Kubernetes cards are published to it)."""
    if cr is None:
        return None, False, False
    from .stack import component_object_name
    spec = cr.get("spec") or {}
    card = spec.get("cardSync") or {}
    return (component_object_name(cr["metadata"]["name"], "registryCenter", "registrycenter"),
            bool((card.get("signing") or {}).get("enabled", True)), bool(card.get("enabled", True)))


def discover_registries(apis: Apis, namespace: str, cr: Optional[Dict[str, Any]],
                        deploy_registries: Tuple[str, ...] = ()) -> List[sync.RegistryRef]:
    managed, signing, publish = managed_context(cr)
    items = apis.custom().list_namespaced_custom_object(ODA, "v1", namespace, "components")["items"]
    return [r for r in (sync.discover(c, managed, signing, publish, deploy_registries) for c in items) if r is not None]


def credentials(apis: Apis, reg: sync.RegistryRef, cr: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """(reader token, writer token). The CR's own registry uses the operator's generated tokens; any other registry
    a Secret named on its Component, which must be labelled for this use, must name that very registry Component
    and must not be one of the operator's own: otherwise whoever can create or annotate a Component could send the
    operator's, or another registry's, tokens to a Service of their choice."""
    from .stack import secret_name
    core = apis.core()
    if reg.managed:
        if cr is None:
            raise SourceUnavailable("no OpenAN CR for the managed registry")
        n = cr["metadata"]["name"]
        sync_secret = _get(core.read_namespaced_secret, secret_name(n, "sync-token"), reg.namespace)
        if sync_secret is None:
            raise SourceUnavailable("sync credential not created yet")
        writer = core.read_namespaced_secret(secret_name(n, "operator-token"), reg.namespace)
        return decode_secret(sync_secret.data, "token"), decode_secret(writer.data, "token")
    if not reg.credentials_secret:
        raise SourceUnavailable(f"registry {reg.id} has no {sync.A_CREDENTIALS} annotation")
    sec = _get(core.read_namespaced_secret, reg.credentials_secret, reg.namespace)
    labels = (sec.metadata.labels or {}) if sec is not None else {}
    bound = (sec.metadata.annotations or {}) if sec is not None else {}
    if sec is None or labels.get(CREDENTIALS_LABEL) != "true" or labels.get(sync.MANAGED_BY[0]) == sync.MANAGED_BY[1]:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} is missing or not labelled {CREDENTIALS_LABEL}=true")
    if bound.get(sync.A_FOR_REGISTRY) != reg.component or bound.get(sync.A_FOR_ENDPOINT) != reg.base_url:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} is not for registry {reg.component} at {reg.base_url} "
                                f"(annotations {sync.A_FOR_REGISTRY}, {sync.A_FOR_ENDPOINT})")
    data = sec.data or {}
    writer = decode_secret(data, "token") if "token" in data else None
    reader = decode_secret(data, "readToken") if "readToken" in data else writer
    if not reader:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} has neither token nor readToken")
    return reader, writer


def source_for(reg: sync.RegistryRef, reader: str):
    if reg.flavour == "native":
        return NativeSource(RegistryClient(reg.base_url, reader))
    return Tmf639Source(reg.base_url, reader)


# ---- cluster writes --------------------------------------------------------------------------------------------

def _owner(kind: str, api_version: str, name: str, uid: str) -> Dict[str, Any]:
    # blockOwnerDeletion false: no finalizer permissions needed and a parent's deletion is never held up by us.
    return {"apiVersion": api_version, "kind": kind, "name": name, "uid": uid, "blockOwnerDeletion": False}


def _child_ops(apis: Apis, kind: str):
    core, apps = apis.core(), apis.apps()
    return {"ConfigMap": (core.read_namespaced_config_map, core.create_namespaced_config_map,
                          core.patch_namespaced_config_map),
            "Service": (core.read_namespaced_service, core.create_namespaced_service, core.patch_namespaced_service),
            "Deployment": (apps.read_namespaced_deployment, apps.create_namespaced_deployment,
                           apps.patch_namespaced_deployment)}[kind]


def apply_agent(apis: Apis, reg: sync.RegistryRef, name: str, objs: List[Dict[str, Any]], digest: str) -> str:
    """Create or update the Component, then its children (owned by it). Only our labels, annotations and spec are
    merge-patched: the Canvas keeps its own annotations, finalizers and status."""
    ns, custom = reg.namespace, apis.custom()
    comp = json.loads(json.dumps(objs[0]))
    comp["metadata"]["namespace"] = ns
    comp["metadata"]["annotations"][sync.A_SPEC_HASH] = digest
    comp["metadata"]["ownerReferences"] = [_owner("Component", f"{ODA}/v1", reg.component, reg.uid)]
    current = _get(custom.get_namespaced_custom_object, ODA, "v1", ns, "components", name)
    if current is None:
        result, live = "created", custom.create_namespaced_custom_object(ODA, "v1", ns, "components", comp)
    elif ((current.get("metadata") or {}).get("annotations") or {}).get(sync.A_SPEC_HASH) == digest:
        result, live = "unchanged", current
    else:
        result = "updated"
        live = custom.patch_namespaced_custom_object(ODA, "v1", ns, "components", name, {
            "metadata": {k: comp["metadata"][k] for k in ("labels", "annotations", "ownerReferences")},
            "spec": comp["spec"]})
    uid = live["metadata"]["uid"]
    for obj in objs[1:]:
        body = json.loads(json.dumps(obj))
        body["metadata"]["namespace"] = ns
        body["metadata"]["ownerReferences"] = [_owner("Component", f"{ODA}/v1", name, uid)]
        read, create, patch = _child_ops(apis, body["kind"])
        if _get(read, name, ns) is None:
            create(ns, body)
        elif result != "unchanged":
            patch(name, ns, body)
    return result


def delete_agent(apis: Apis, namespace: str, name: str) -> None:
    """Operator-initiated: mark first, so the delete handler does not delete the card in the registry."""
    custom = apis.custom()
    try:
        custom.patch_namespaced_custom_object(ODA, "v1", namespace, "components", name,
                                              {"metadata": {"annotations": {sync.A_OP_DELETE: "true"}}})
        custom.delete_namespaced_custom_object(ODA, "v1", namespace, "components", name)
    except client.ApiException as e:
        if e.status != 404:
            raise


def parent_component(meta: Dict[str, Any]) -> Optional[str]:
    """The Component an ExposedAPI belongs to: its ownerReference when the Canvas sets one, else the
    oda.tmforum.org/componentName label (the Canvas used on this cluster does not set owner references)."""
    for ref in meta.get("ownerReferences") or []:
        if ref.get("kind") == "Component":
            return ref.get("name")
    return (meta.get("labels") or {}).get(am.COMPONENT_LABEL)


def published_ref(meta: Dict[str, Any], reg: sync.RegistryRef) -> Optional[str]:
    """"<org>/<name>" this ExposedAPI published to `reg`, from the annotations sync_card writes."""
    ann = meta.get("annotations") or {}
    if reg.managed:
        return ann.get(ANNOTATION)
    try:
        return (json.loads(ann.get(sync.REGISTERED_CARDS) or "{}") or {}).get(reg.id)
    except ValueError:
        return None


def delete_k8s_made(apis: Apis, reg: sync.RegistryRef, keys: Set[Tuple[str, str]], logger) -> List[str]:
    """'Delete on either side' for agents made in Kubernetes: their card was deregistered and is gone from the
    complete listing. Only Components whose ExposedAPI had published exactly that card, that are not registry-made,
    not already being deleted and not annotated keep-on-card-delete."""
    if not keys:
        return []
    custom, ns, done = apis.custom(), reg.namespace, []
    wanted = {f"{o}/{n}" for o, n in keys}
    for api in custom.list_namespaced_custom_object(ODA, "v1", ns, "exposedapis")["items"]:
        meta = api.get("metadata") or {}
        if not is_a2a(api.get("spec") or {}) or meta.get("deletionTimestamp") or published_ref(meta, reg) not in wanted:
            continue
        parent = parent_component(meta)
        comp = _get(custom.get_namespaced_custom_object, ODA, "v1", ns, "components", parent) if parent else None
        cmeta = (comp or {}).get("metadata") or {}
        if (comp is None or cmeta.get("deletionTimestamp") or sync.is_registry_originated(comp)
                or str((cmeta.get("annotations") or {}).get(sync.A_KEEP, "")).lower() == "true"):
            continue
        try:
            custom.delete_namespaced_custom_object(ODA, "v1", ns, "components", parent)
        except client.ApiException as e:
            if e.status != 404:
                raise
        logger.warning(f"Component {parent} deleted: its card {published_ref(meta, reg)} was deleted in {reg.id}")
        done.append(parent)
    return done


# ---- one pass --------------------------------------------------------------------------------------------------

def run_pass(apis: Apis, reg: sync.RegistryRef, cr: Optional[Dict[str, Any]], settings: Settings,
             now: Optional[float] = None, logger=None) -> PassResult:
    """Observe -> plan -> apply for one registry. Raises SourceUnavailable without touching the cluster when the
    registry cannot be read completely; the cursor only advances after a successful pass."""
    now = time.monotonic() if now is None else now
    logger = logger or LOG
    st = STATE.setdefault(reg.id, State())
    out = PassResult()
    reader, _writer = credentials(apis, reg, cr)
    source = source_for(reg, reader)
    events: List[Dict[str, Any]] = []
    cursor = st.cursor
    due = now - st.last_pass >= (settings.resync_seconds if source.supports_feed else settings.tmf639_seconds)
    if source.supports_feed:
        if cursor is None:
            cursor, due = source.head(), True
        feed = source.changes(cursor)
        events, cursor = feed.events, feed.next_since
    if not events and not due and st.last_pass:
        st.cursor = cursor
        return out
    agents = source.list_agents()
    custom = apis.custom()
    live = [sync.Live.of(o) for o in custom.list_namespaced_custom_object(
        ODA, "v1", reg.namespace, "components",
        label_selector=f"{sync.ORIGIN_LABEL}={sync.ORIGIN_REGISTRY},{sync.REGISTRY_LABEL}={reg.tag}")["items"]]
    plan = sync.plan(reg, agents, live)
    out.warnings += plan.warnings
    policy = am.Policy(reg.namespace, settings.image_prefixes, settings.proxy_image, settings.upstream_suffixes,
                       reg.deploy_allowed)
    for name, (org, agent), card in plan.apply:
        try:
            _mode, objs, warnings = am.build(name, card, policy, sync.labels_for(reg, name),
                                             sync.annotations_for(reg, org, agent))
            out.applied[name] = apply_agent(apis, reg, name, objs, am.spec_hash(objs))
            out.warnings += [f"{org}/{agent}: {w}" for w in warnings]
        except am.DeploymentError as e:
            out.warnings.append(f"{org}/{agent}: not mirrored: {e}")
    deletes, st.pending_deletes = sync.guard_deletes(plan.delete, sum(1 for o in live if not o.deleting),
                                                     st.pending_deletes)
    if len(deletes) < len(plan.delete):
        out.warnings.append(f"holding {len(plan.delete)} deletions until the next pass confirms them")
    for name in deletes:
        delete_agent(apis, reg.namespace, name)
        out.deleted.append(name)
    if events and reg.publish and reg.flavour == "native":
        present = {sync.card_key(c) for c, _ in agents}
        out.k8s_deleted = delete_k8s_made(apis, reg, sync.deregistered(events) - present, logger)
    st.cursor, st.last_pass, out.ran = cursor, now, True
    return out


# ---- mirror delete (user-initiated) ----------------------------------------------------------------------------

def release(apis: Apis, meta: Dict[str, Any], namespace: str, cr: Optional[Dict[str, Any]],
            settings: Optional[Settings] = None) -> str:
    """A registry-made Component is being deleted. Operator-initiated deletes and orphans pass; otherwise the card
    is deleted in its registry first ('delete on either side'); a refusal raises DeleteRefused (finalizer kept)."""
    ann = meta.get("annotations") or {}
    if str(ann.get(sync.A_OP_DELETE, "")).lower() == "true":
        return "operator-initiated"
    source_ns, _, source_name = (ann.get(sync.A_SOURCE) or "").partition("/")
    org, _, agent = (ann.get(sync.A_CARD_KEY) or "").partition("/")
    if not source_name or not org or not agent or cr is None or source_ns != namespace:
        return "no source"
    comp = _get(apis.custom().get_namespaced_custom_object, ODA, "v1", namespace, "components", source_name)
    managed, signing, publish = managed_context(cr)
    reg = sync.discover(comp, managed, signing, publish, settings.deploy_registries if settings else ()) if comp else None
    if reg is None:
        return "registry gone"
    # Labels and annotations are editable: only act for an object this operator made from this very card,
    # i.e. owned by that registry Component and named after the card key.
    owned = any(r.get("uid") == reg.uid for r in meta.get("ownerReferences") or [])
    if not owned or sync.object_name(reg, org, agent) != meta.get("name"):
        return "not made from this registry"
    reader, writer = credentials(apis, reg, cr)
    found = next(((c, s) for c, s in source_for(reg, reader).list_agents() if sync.card_key(c) == (org, agent)), None)
    if found is None:
        return "card absent"
    card, status = found
    if sync.is_operator_card(card):  # published from a cluster, never mirrored: not this Component's card
        return "card published from Kubernetes"
    if status != sync.PUBLISHED:     # only published cards are ever mirrored
        return "card not published"
    if settings is not None:
        # The mirror must be exactly what this operator renders for that card (same hash it stamped), not an
        # object somebody named and labelled after it.
        policy = am.Policy(namespace, settings.image_prefixes, settings.proxy_image, settings.upstream_suffixes,
                           reg.deploy_allowed)
        try:
            _m, objs, _w = am.build(meta.get("name", ""), card, policy, sync.labels_for(reg, meta.get("name", "")),
                                    sync.annotations_for(reg, org, agent))
        except am.DeploymentError:
            return "not made from this registry"
        if am.spec_hash(objs) != ann.get(sync.A_SPEC_HASH):
            return "not made from this registry"
    if reg.flavour != "native" or not writer:
        raise DeleteRefused(f"registry {reg.id} is read-only for the operator: delete {org}/{agent} there first")
    try:
        RegistryClient(reg.base_url, writer).delete(org, agent)
    except RegistryError as e:
        if 400 <= e.status < 500:
            raise DeleteRefused(f"registry {reg.id} refused to delete {org}/{agent} ({e.status}): delete it there "
                                "or annotate the Component openan.dev/operator-delete=true")
        raise SourceUnavailable(str(e), e.status)
    except httpx.HTTPError as e:
        raise SourceUnavailable(f"{type(e).__name__}: {e}")
    return "card deleted"
