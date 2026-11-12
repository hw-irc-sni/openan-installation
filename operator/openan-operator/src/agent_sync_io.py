# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Side effects of the agent sync: observe a registry, apply the pure plan (agent_sync) to the cluster, release
deletes. The Kubernetes API clients come in an Apis bundle built by the handlers from their _core/_custom/_apps
seams, the only place tests patch."""
import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from kubernetes import client

from . import agent_manifests as am
from . import agent_sync as sync
from .cards import decode_secret
from .registry_client import RegistryClient
from .registry_source import NativeSource, SourceUnavailable, Tmf639Source, find_agent, unavailable_on_error
from .stack import secret_name

ODA = "oda.tmforum.org"
CREDENTIALS_LABEL = "openan.dev/registry-credentials"
NEVER = float("-inf")


@dataclass
class State:
    cursor: Optional[int] = None
    last_pass: float = NEVER
    down: bool = False
    synced: bool = False                               # a pass completed since the operator started
    pending_deletes: Optional[Set[str]] = None         # mass-delete guard
    retry_gone: Set[sync.Key] = field(default_factory=set)  # deregistered cards whose Components are still to delete


STATE: Dict[str, State] = {}  # registry id -> state (the cursor is also persisted in the OpenAN CR status)


def state_for(reg_id: str) -> State:
    return STATE.setdefault(reg_id, State())


def forget_except(reg_ids: Set[str]) -> None:
    for gone in set(STATE) - reg_ids:
        del STATE[gone]


@dataclass(frozen=True)
class Settings:
    image_prefixes: Tuple[str, ...]
    proxy_image: str
    upstream_suffixes: Tuple[str, ...] = ()
    deploy_registries: Tuple[str, ...] = ()
    resync_seconds: int = 300
    tmf639_seconds: int = 60

    def policy(self, reg: sync.RegistryRef) -> am.Policy:
        return am.Policy(reg.namespace, self.image_prefixes, self.proxy_image, self.upstream_suffixes, reg.deploy_allowed)


@dataclass
class Apis:
    core: Any
    custom: Any
    apps: Any


@dataclass
class PassResult:
    applied: Dict[str, str] = field(default_factory=dict)  # mirror name -> created|updated|unchanged
    mirrors_removed: List[str] = field(default_factory=list)
    components_removed: List[str] = field(default_factory=list)  # Kubernetes-made agents whose cards were deleted
    warnings: List[str] = field(default_factory=list)
    ran: bool = False


class DeleteRefused(RuntimeError):
    """The registry will not delete the card (e.g. owned by another identity): keep the Component's finalizer."""


def ignore_404(fn: Callable, *args) -> Optional[Any]:
    try:
        return fn(*args)
    except client.ApiException as e:
        if e.status == 404:
            return None
        raise


# ---- discovery and credentials ---------------------------------------------------------------------------------

def discover_registries(apis: Apis, namespace: str, cr: Dict[str, Any],
                        deploy_registries: Tuple[str, ...] = ()) -> List[sync.RegistryRef]:
    managed = sync.managed_context(cr)
    items = apis.custom.list_namespaced_custom_object(ODA, "v1", namespace, "components")["items"]
    return [r for r in (sync.discover(c, managed, deploy_registries) for c in items) if r is not None]


def _secret(core, name: str, namespace: str):
    return ignore_404(core.read_namespaced_secret, name, namespace)


def _managed_credentials(core, reg: sync.RegistryRef, cr: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    n = cr["metadata"]["name"]
    reader = _secret(core, secret_name(n, "sync-token"), reg.namespace)
    if reader is None:
        raise SourceUnavailable("sync credential not created yet")
    writer = core.read_namespaced_secret(secret_name(n, "operator-token"), reg.namespace)
    return decode_secret(reader.data, "token"), decode_secret(writer.data, "token")


def _connected_credentials(core, reg: sync.RegistryRef) -> Tuple[str, Optional[str]]:
    """A Secret named on the registry Component, labelled for this use and bound to that Component and endpoint,
    never one of the operator's own: otherwise whoever can create or annotate a Component could send the
    operator's, or another registry's, tokens to a Service of their choice."""
    if not reg.credentials_secret:
        raise SourceUnavailable(f"registry {reg.id} has no {sync.A_CREDENTIALS} annotation")
    sec = _secret(core, reg.credentials_secret, reg.namespace)
    if sec is None:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} not found")
    labels, bound = sec.metadata.labels or {}, sec.metadata.annotations or {}
    if labels.get(CREDENTIALS_LABEL) != "true" or labels.get(sync.MANAGED_BY_KEY) == sync.MANAGED_BY_VALUE:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} is not labelled {CREDENTIALS_LABEL}=true")
    if bound.get(sync.A_FOR_REGISTRY) != reg.component or bound.get(sync.A_FOR_ENDPOINT) != reg.base_url:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} is not for registry {reg.component} at {reg.base_url} "
                                f"(annotations {sync.A_FOR_REGISTRY}, {sync.A_FOR_ENDPOINT})")
    data = sec.data or {}
    writer = decode_secret(data, "token") if "token" in data else None
    reader = decode_secret(data, "readToken") if "readToken" in data else writer
    if not reader:
        raise SourceUnavailable(f"Secret {reg.credentials_secret} has neither token nor readToken")
    return reader, writer


def credentials(apis: Apis, reg: sync.RegistryRef, cr: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """(reader token, writer token): the operator's generated tokens for the CR's own registry, else the registry's
    bound Secret."""
    return _managed_credentials(apis.core, reg, cr) if reg.managed else _connected_credentials(apis.core, reg)


def source_for(reg: sync.RegistryRef, reader: str):
    if reg.flavour == "native":
        return NativeSource(RegistryClient(reg.base_url, reader))
    return Tmf639Source(reg.base_url, reader)


@dataclass
class Connection:
    reg: sync.RegistryRef
    reader: str
    writer: Optional[str]

    def source(self):
        return source_for(self.reg, self.reader)

    def write_client(self) -> Optional[RegistryClient]:
        return RegistryClient(self.reg.base_url, self.writer) if self.writer else None


def connections(apis: Apis, namespace: str, cr: Dict[str, Any],
                deploy_registries: Tuple[str, ...] = ()) -> Tuple[List[Connection], List[str]]:
    """Every registry in the namespace with its credentials, resolved once; (connections, problems)."""
    out, problems = [], []
    for reg in discover_registries(apis, namespace, cr, deploy_registries):
        try:
            out.append(Connection(reg, *credentials(apis, reg, cr)))
        except SourceUnavailable as e:
            problems.append(f"registry {reg.id} skipped: {e}")
    return out, problems


# ---- cluster writes --------------------------------------------------------------------------------------------

def _owner(name: str, uid: str) -> Dict[str, Any]:
    # blockOwnerDeletion false: no finalizer permissions needed and a parent's deletion is never held up by us.
    return {"apiVersion": f"{ODA}/v1", "kind": "Component", "name": name, "uid": uid, "blockOwnerDeletion": False}


_CHILD_API = {"ConfigMap": ("core", "config_map"), "Service": ("core", "service"), "Deployment": ("apps", "deployment")}


def _child_op(apis: Apis, kind: str, verb: str) -> Callable:
    api, suffix = _CHILD_API[kind]
    return getattr(getattr(apis, api), f"{verb}_namespaced_{suffix}")


def _plain(obj: Any) -> Dict[str, Any]:
    return client.ApiClient().sanitize_for_serialization(obj) or {}


def _apply_child(apis: Apis, namespace: str, body: Dict[str, Any], owner: Dict[str, Any]) -> None:
    """Create, or replace as a whole: a strategic merge would append to lists (a second Service port with the same
    name is rejected; switching mirror <-> deploy would leave both containers in the pod)."""
    kind, name = body["kind"], body["metadata"]["name"]
    current = _plain(ignore_404(_child_op(apis, kind, "read"), name, namespace))
    others = [r for r in (current.get("metadata") or {}).get("ownerReferences") or [] if r.get("uid") != owner["uid"]]
    body["metadata"].update(namespace=namespace, ownerReferences=[owner] + others)
    if not current:
        _child_op(apis, kind, "create")(namespace, body)
        return
    body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
    if kind == "Service":  # immutable once allocated
        for key in ("clusterIP", "clusterIPs"):
            if key in (current.get("spec") or {}):
                body["spec"][key] = current["spec"][key]
    _child_op(apis, kind, "replace")(name, namespace, body)


def apply_agent(apis: Apis, reg: sync.RegistryRef, name: str, objs: List[Dict[str, Any]]) -> str:
    """Create or update the Component and its children (owned by it). The spec hash is stamped on the Component
    last, so a pass that failed half-way is redone; only our labels, annotations and spec are merge-patched: the
    Canvas keeps its own annotations, finalizers and status."""
    digest, ns, custom = am.spec_hash(objs), reg.namespace, apis.custom
    comp = copy.deepcopy(objs[0])
    comp["metadata"].update(namespace=ns, ownerReferences=[_owner(reg.component, reg.uid)])
    children = [copy.deepcopy(o) for o in objs[1:]]
    current = ignore_404(custom.get_namespaced_custom_object, ODA, "v1", ns, "components", name)
    if current is not None and ((current["metadata"].get("annotations") or {}).get(sync.A_SPEC_HASH) == digest):
        owner = _owner(name, current["metadata"]["uid"])
        for child in children:  # repair deleted children only
            if ignore_404(_child_op(apis, child["kind"], "read"), name, ns) is None:
                child["metadata"].update(namespace=ns, ownerReferences=[owner])
                _child_op(apis, child["kind"], "create")(ns, child)
        return "unchanged"
    result = "created" if current is None else "updated"
    if current is None:
        current = custom.create_namespaced_custom_object(ODA, "v1", ns, "components", comp)
    owner = _owner(name, current["metadata"]["uid"])
    for child in children:
        _apply_child(apis, ns, child, owner)
    if not any(c["kind"] == "ConfigMap" for c in children):  # deploy mode: no proxy configuration
        ignore_404(apis.core.delete_namespaced_config_map, name, ns)
    custom.patch_namespaced_custom_object(ODA, "v1", ns, "components", name, {
        "metadata": {"labels": comp["metadata"]["labels"], "ownerReferences": comp["metadata"]["ownerReferences"],
                     "annotations": {**comp["metadata"]["annotations"], sync.A_SPEC_HASH: digest,
                                     sync.A_OP_DELETE: None}},
        "spec": comp["spec"]})
    return result


def delete_agent(apis: Apis, namespace: str, name: str) -> None:
    """Operator-initiated: mark first, so the delete handler does not delete the card in the registry; unmark if
    the delete fails, so a later user delete of a mirror that stays is not mistaken for ours."""
    custom = apis.custom
    if ignore_404(custom.patch_namespaced_custom_object, ODA, "v1", namespace, "components", name,
                  {"metadata": {"annotations": {sync.A_OP_DELETE: "true"}}}) is None:
        return
    try:
        custom.delete_namespaced_custom_object(ODA, "v1", namespace, "components", name)
    except client.ApiException as e:
        if e.status == 404:
            return
        ignore_404(custom.patch_namespaced_custom_object, ODA, "v1", namespace, "components", name,
                   {"metadata": {"annotations": {sync.A_OP_DELETE: None}}})
        raise


def render(reg: sync.RegistryRef, name: str, key: sync.Key, card: Dict[str, Any],
           settings: Settings) -> Tuple[List[Dict[str, Any]], List[str]]:
    return am.build(name, card, settings.policy(reg), sync.labels_for(reg, name), sync.annotations_for(reg, *key))


def delete_k8s_made(apis: Apis, reg: sync.RegistryRef, gone: Set[sync.Key]) -> List[str]:
    """'Delete on either side' for agents made in Kubernetes whose cards were deleted (see
    agent_sync.components_to_delete for which ones)."""
    if not gone:
        return []
    custom, ns = apis.custom, reg.namespace
    exposed = custom.list_namespaced_custom_object(ODA, "v1", ns, "exposedapis")["items"]
    comps = {c["metadata"]["name"]: c for c in custom.list_namespaced_custom_object(ODA, "v1", ns, "components")["items"]}
    names = sync.components_to_delete(reg, exposed, comps, gone)
    for name in names:
        ignore_404(custom.delete_namespaced_custom_object, ODA, "v1", ns, "components", name)
    return names


# ---- one pass --------------------------------------------------------------------------------------------------

def _observe(source, st: State, settings: Settings, now: float,
             stored_cursor: Optional[int]) -> Tuple[List[Dict[str, Any]], Optional[int], bool]:
    """(events, new cursor, whether a full pass is needed). The cursor resumes from the CR status after a restart,
    so changes made while the operator was down are replayed; without one it starts at the feed head."""
    due = now - st.last_pass >= (settings.resync_seconds if source.supports_feed else settings.tmf639_seconds)
    if not source.supports_feed:
        return [], None, due
    cursor = st.cursor if st.cursor is not None else stored_cursor
    if cursor is None:
        cursor, due = source.head(), True
    feed = source.changes(cursor)
    return feed.events, feed.next_since, due or bool(feed.events)


def _apply_plan(apis: Apis, reg: sync.RegistryRef, settings: Settings, st: State,
                agents, out: PassResult) -> bool:
    """Apply one plan; every agent and every delete on its own, so one failure never starves the others.
    Returns whether something failed (the next tick then runs a full pass again)."""
    live = [sync.Live.of(o) for o in apis.custom.list_namespaced_custom_object(
        ODA, "v1", reg.namespace, "components",
        label_selector=f"{sync.ORIGIN_LABEL}={sync.ORIGIN_REGISTRY},{sync.REGISTRY_LABEL}={reg.tag}")["items"]]
    plan = sync.plan(reg, agents, live)
    out.warnings += plan.warnings
    failed = False
    for name, key, card in plan.apply:
        try:
            objs, warnings = render(reg, name, key, card, settings)
            out.applied[name] = apply_agent(apis, reg, name, objs)
            out.warnings += [f"{sync.format_key(*key)}: {w}" for w in warnings]
        except am.DeploymentError as e:  # the card itself: retrying will not help until it changes
            out.warnings.append(f"{sync.format_key(*key)}: not mirrored: {e}")
        except client.ApiException as e:
            out.warnings.append(f"{name}: Kubernetes API {e.status} {e.reason}")
            failed = True
    deletes, st.pending_deletes = sync.guard_deletes(plan.delete, sum(1 for o in live if not o.deleting),
                                                     st.pending_deletes)
    if len(deletes) < len(plan.delete):
        out.warnings.append(f"holding {len(plan.delete)} deletions until the next pass confirms them")
    for name in deletes:
        try:
            delete_agent(apis, reg.namespace, name)
            out.mirrors_removed.append(name)
        except client.ApiException as e:
            out.warnings.append(f"{name}: delete failed: Kubernetes API {e.status} {e.reason}")
            failed = True
    return failed


def run_pass(apis: Apis, reg: sync.RegistryRef, cr: Dict[str, Any], settings: Settings,
             now: Optional[float] = None, stored_cursor: Optional[int] = None) -> PassResult:
    """Observe -> plan -> apply for one registry. Raises SourceUnavailable without touching the cluster when the
    registry cannot be read completely."""
    now = time.monotonic() if now is None else now
    st, out = state_for(reg.id), PassResult()
    reader, _writer = credentials(apis, reg, cr)
    source = source_for(reg, reader)
    events, cursor, needed = _observe(source, st, settings, now, stored_cursor)
    if not needed:
        st.cursor = cursor
        return out
    agents = source.list_agents()
    failed = _apply_plan(apis, reg, settings, st, agents, out)
    if reg.publish:  # only registries the cluster publishes to hold cards of Kubernetes-made agents
        gone = (sync.deregistered(events) | st.retry_gone) - {sync.card_key(c) for c, _ in agents}
        try:
            out.components_removed, st.retry_gone = delete_k8s_made(apis, reg, gone), set()
        except client.ApiException as e:
            out.warnings.append(f"deleting agents whose cards were deleted failed: {e.status} {e.reason}")
            st.retry_gone, failed = gone, True
    st.cursor, st.last_pass, st.synced, out.ran = cursor, NEVER if failed else now, True, True
    return out


def first_pass_done(reg_id: str) -> bool:
    return reg_id in STATE and STATE[reg_id].synced


# ---- mirror delete (user-initiated) ----------------------------------------------------------------------------

def release(apis: Apis, meta: Dict[str, Any], namespace: str, cr: Dict[str, Any], settings: Settings) -> str:
    """A registry-made Component is being deleted. Operator-initiated deletes and orphans pass; otherwise the card
    is deleted in its registry first ('delete on either side'); a refusal raises DeleteRefused (finalizer kept).
    Labels and annotations are editable, so only an object this operator rendered for that very card counts: owned
    by the registry Component, named after the card and carrying the hash rendered for it now. A mirror deleted
    right after its card changed (before the next pass re-stamped it) therefore goes without its card and comes
    back on the next pass; deleting it again then deletes the card."""
    ann = meta.get("annotations") or {}
    if sync.is_true(ann.get(sync.A_OP_DELETE)):
        return "operator-initiated"
    source_ns, _, source_name = (ann.get(sync.A_SOURCE) or "").partition("/")
    key = sync.parse_key(ann.get(sync.A_CARD_KEY))
    if not source_name or key is None or source_ns != namespace:
        return "no source"
    comp = ignore_404(apis.custom.get_namespaced_custom_object, ODA, "v1", namespace, "components", source_name)
    reg = sync.discover(comp, sync.managed_context(cr), settings.deploy_registries) if comp else None
    if reg is None:
        return "registry gone"
    owned = any(r.get("uid") == reg.uid for r in meta.get("ownerReferences") or [])
    if not owned or sync.object_name(reg, *key) != meta.get("name"):
        return "not made from this registry"
    reader, writer = credentials(apis, reg, cr)
    found = find_agent(source_for(reg, reader), key)
    if found is None:
        return "card absent"
    card, status = found
    if sync.is_operator_card(card):  # published from a cluster, never mirrored: not this Component's card
        return "card published from Kubernetes"
    if status != sync.PUBLISHED:     # only published cards are ever mirrored
        return "card not published"
    try:
        objs, _warnings = render(reg, meta.get("name", ""), key, card, settings)
    except am.DeploymentError:
        return "not made from this registry"
    if am.spec_hash(objs) != ann.get(sync.A_SPEC_HASH):
        return "not made from this registry"
    if reg.flavour != "native" or not writer:
        raise DeleteRefused(f"registry {reg.id} is read-only for the operator: delete {sync.format_key(*key)} there first")
    try:
        with unavailable_on_error():
            RegistryClient(reg.base_url, writer).delete(*key)
    except SourceUnavailable as e:
        if e.status is not None and 400 <= e.status < 500:
            raise DeleteRefused(f"registry {reg.id} refused to delete {sync.format_key(*key)} ({e.status}): delete it "
                                f"there or annotate the Component {sync.A_OP_DELETE}=true")
        raise
    return "card deleted"
