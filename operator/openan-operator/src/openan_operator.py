# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""OpenAN operator: OpenAN CR lifecycle (Helm-managed ODA Components) + a2a ExposedAPI -> Registry cards
+ agent registries -> Agent ODA Components (agent sync)."""
import base64
import hashlib
import json
import logging
import os
import secrets
from typing import Any, Dict, List, Optional

import httpx
import kopf
from kubernetes import client, config

from . import agent_sync, agent_sync_io, helm_driver, metrics, stack
from .card_signer import public_jwk
from .cards import ANNOTATION, OwnershipError, allowed_hosts, build_signed_card, card_owner, owner_ref, candidate_urls, decode_secret, fetch_card, is_a2a, trust_document
from .card_mapper import CardError
from .registry_client import RegistryClient, RegistryError
from .registry_source import SourceUnavailable
from .secrets_bootstrap import new_stack_secrets, with_sync_credential

ODA, OPENAN = "oda.tmforum.org", "openan.dev"
CHARTS_DIR = os.environ.get("CHARTS_DIR", "/charts")
FINALIZER = f"{OPENAN}/finalizer"


def _load() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    _load()
    settings.persistence.finalizer = FINALIZER
    # Canvas operators (kopf) also watch ExposedAPIs: keep our state in our own annotation namespace.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=OPENAN)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(prefix=OPENAN)
    settings.peering.name = "openan"
    settings.peering.mandatory = False
    settings.posting.level = logging.INFO
    if os.environ.get("METRICS_PORT", "8000") != "0":
        metrics.start(int(os.environ.get("METRICS_PORT", "8000")))


def _core() -> client.CoreV1Api:
    return client.CoreV1Api()


def _custom() -> client.CustomObjectsApi:
    return client.CustomObjectsApi()


def _apps() -> client.AppsV1Api:
    return client.AppsV1Api()


def _apis() -> agent_sync_io.Apis:
    # resolved at call time, so tests that patch _core/_custom/_apps also patch the agent sync
    return agent_sync_io.Apis(lambda: _core(), lambda: _custom(), lambda: _apps())


def ensure_secrets(name: str, namespace: str, owner: Dict[str, Any]) -> None:
    """Create-once: existing Secrets are never rewritten (tokens/keys must stay stable)."""
    generated = None
    for suffix in ("registry-runtime", "registry-credentials", "operator-token", "orchestration-runtime", "signing-key"):
        sname = stack.secret_name(name, suffix)
        try:
            existing = _core().read_namespaced_secret(sname, namespace)
            if not existing.metadata.owner_references:  # detached by an earlier retainData delete: re-adopt
                _core().patch_namespaced_secret(sname, namespace, {"metadata": {"ownerReferences": [{
                    "apiVersion": f"{OPENAN}/v1alpha1", "kind": "OpenAN", "name": owner["metadata"]["name"],
                    "uid": owner["metadata"]["uid"], "blockOwnerDeletion": True}]}})
            continue
        except client.ApiException as e:
            if e.status != 404:
                raise
        generated = generated or new_stack_secrets(f"{name}")
        body = client.V1Secret(metadata=client.V1ObjectMeta(name=sname, namespace=namespace,
                               labels={"app.kubernetes.io/managed-by": "openan-operator"}),
                               string_data=generated[suffix])
        body.metadata.owner_references = [client.V1OwnerReference(
            api_version=f"{OPENAN}/v1alpha1", kind="OpenAN", name=owner["metadata"]["name"],
            uid=owner["metadata"]["uid"], block_owner_deletion=True)]
        _core().create_namespaced_secret(namespace, body)


def ensure_sync_credential(name: str, namespace: str, owner: Dict[str, Any]) -> str:
    """Agent sync reads the registry with its own nms_oss identity. Added to existing installs too: the token
    Secret first (so a crash in between re-adds the same token), then the entry in the credentials file.
    Returns the hash of the credentials file, which restarts the Registry Center when it changes."""
    core = _core()
    cred_name, tok_name = stack.secret_name(name, "registry-credentials"), stack.secret_name(name, "sync-token")
    conf = decode_secret(core.read_namespaced_secret(cred_name, namespace).data, "integration_credentials.conf")
    try:
        token: Optional[str] = decode_secret(core.read_namespaced_secret(tok_name, namespace).data, "token")
    except client.ApiException as e:
        if e.status != 404:
            raise
        token = None
    if token is None:
        token = secrets.token_urlsafe(48)
        body = client.V1Secret(metadata=client.V1ObjectMeta(
            name=tok_name, namespace=namespace, labels={"app.kubernetes.io/managed-by": "openan-operator"},
            owner_references=[client.V1OwnerReference(api_version=f"{OPENAN}/v1alpha1", kind="OpenAN",
                              name=owner["metadata"]["name"], uid=owner["metadata"]["uid"], block_owner_deletion=True)]),
            string_data={"token": token})
        core.create_namespaced_secret(namespace, body)
    hmac_key = decode_secret(core.read_namespaced_secret(stack.secret_name(name, "registry-runtime"), namespace).data,
                             "INTEGRATION_TOKEN_HMAC_KEY")
    wanted = with_sync_credential(conf, token, hmac_key)  # identical to conf when the entry is already current
    if wanted != conf:
        core.patch_namespaced_secret(cred_name, namespace, {"data": {
            "integration_credentials.conf": base64.b64encode(wanted.encode()).decode()}})
    return hashlib.sha256(wanted.encode()).hexdigest()[:32]


def component_ready(name: str, namespace: str) -> bool:
    try:
        obj = _custom().get_namespaced_custom_object(ODA, "v1", namespace, "components", name)
    except client.ApiException as e:
        if e.status == 404:
            return False
        raise
    return stack.is_ready(obj)


@kopf.on.create(OPENAN, "v1alpha1", "openans")
@kopf.on.update(OPENAN, "v1alpha1", "openans")
@kopf.on.resume(OPENAN, "v1alpha1", "openans")
def reconcile(spec, name, namespace, body, patch, logger, **_):
    conds, comps = [], {}
    prior_ready = True
    prefixes = tuple(p for p in os.environ.get("ALLOWED_IMAGE_PREFIXES", "").split(",") if p) or stack.DEFAULT_IMAGE_PREFIXES
    spec_prefixes = (tuple(p for p in os.environ.get("ALLOWED_SPEC_URL_PREFIXES", "").split(",") if p)
                     or stack.DEFAULT_SPEC_URL_PREFIXES)
    callback_hosts = tuple(h.strip() for h in os.environ.get("ALLOWED_CALLBACK_HOSTS", "").split(",") if h.strip())
    callback_cas = tuple(c.strip() for c in os.environ.get("ALLOWED_CALLBACK_CA_CONFIGMAPS", "").split(",") if c.strip())
    try:  # reject the whole CR up front, before anything is installed
        for comp in stack.enabled_components(spec):
            stack.validate_values(comp, spec.get(comp, {}).get("values", {}), prefixes,
                                  allowed_spec_prefixes=spec_prefixes, allowed_callback_hosts=callback_hosts,
                                  allowed_callback_ca_configmaps=callback_cas)
    except stack.ValuesError as e:
        metrics.RECONCILES.labels("rejected").inc()
        raise kopf.PermanentError(str(e))
    ensure_secrets(name, namespace, body)  # only for CRs that passed validation
    checksum = ensure_sync_credential(name, namespace, body) if stack.agent_sync_enabled(spec) else ""
    for comp in stack.enabled_components(spec):  # ordered RC -> OC -> frontend
        if not prior_ready:
            break
        if not spec.get(comp, {}).get("values", {}).get("image", {}).get("tag"):
            raise kopf.PermanentError(f"{comp}: values.image.tag is required (no default)")
        rel = stack.release_name(name, comp)
        try:
            helm_driver.upgrade_install(rel, os.path.join(CHARTS_DIR, stack.CHART_DIR[comp]), namespace,
                                        stack.chart_values(name, spec, comp, credentials_checksum=checksum))
        except helm_driver.HelmError as e:
            metrics.RECONCILES.labels("error").inc()
            raise kopf.TemporaryError(f"helm {rel}: {e}", delay=30)
        cname = stack.component_object_name(name, comp, {"registryCenter": "registrycenter",
                "orchestrationCenter": "orchestrationcenter", "frontend": "frontend"}[comp])
        ready = component_ready(cname, namespace)
        comps[comp] = {"release": rel, "component": cname, "phase": "Ready" if ready else "Progressing"}
        conds.append(stack.condition(f"{comp[0].upper()}{comp[1:]}Ready", ready, "Complete" if ready else "Progressing"))
        prior_ready = ready
    all_ready = prior_ready and len(comps) == len(stack.enabled_components(spec))
    conds.append(stack.condition("Ready", all_ready, "AllComponentsReady" if all_ready else "WaitingForComponents"))
    patch.status["observedGeneration"] = body["metadata"].get("generation")
    patch.status["conditions"] = conds
    patch.status["components"] = comps
    metrics.RECONCILES.labels("ok" if all_ready else "progressing").inc()
    if not all_ready:
        raise kopf.TemporaryError("waiting for components to become Complete", delay=15)


def _delete_pvcs(release: str, namespace: str) -> None:
    """Helm keeps the data PVCs (resource-policy: keep); remove them unless spec.retainData.
    Deleted one by one: the Role grants `delete`, deliberately not `deletecollection`."""
    for pvc in _core().list_namespaced_persistent_volume_claim(
            namespace, label_selector=f"app.kubernetes.io/instance={release}").items:
        try:
            _core().delete_namespaced_persistent_volume_claim(pvc.metadata.name, namespace)
        except client.ApiException as e:
            if e.status != 404:
                raise


def _orphan_pvcs(release: str, namespace: str) -> None:
    """The Canvas component operator adopts the PVCs (ownerReference -> Component), so deleting the Component
    would garbage-collect them despite Helm's keep policy: detach them before uninstalling."""
    for pvc in _core().list_namespaced_persistent_volume_claim(
            namespace, label_selector=f"app.kubernetes.io/instance={release}").items:
        if pvc.metadata.owner_references:
            _core().patch_namespaced_persistent_volume_claim(
                pvc.metadata.name, namespace, {"metadata": {"ownerReferences": None}})


def _orphan_secrets(name: str, namespace: str) -> None:
    """retainData: detach generated Secrets from the CR so garbage collection keeps tokens and keys."""
    for obj in _core().list_namespaced_secret(namespace, label_selector="app.kubernetes.io/managed-by=openan-operator").items:
        if obj.metadata.name.startswith(f"{name}-"):
            _core().patch_namespaced_secret(obj.metadata.name, namespace, {"metadata": {"ownerReferences": None}})


@kopf.on.delete(OPENAN, "v1alpha1", "openans")
def teardown(spec, name, namespace, logger, **_):
    retain = bool(spec.get("retainData", False))
    for comp in reversed(stack.enabled_components(spec)):
        rel = stack.release_name(name, comp)
        try:
            if retain:
                _orphan_pvcs(rel, namespace)
            helm_driver.uninstall(rel, namespace)
            if not retain:
                _delete_pvcs(rel, namespace)
        except (helm_driver.HelmError, client.ApiException) as e:
            raise kopf.TemporaryError(f"teardown {rel}: {e}", delay=15)
    if retain:
        _orphan_secrets(name, namespace)


def _registry_for(namespace: str):
    """Locate the (single) OpenAN CR in the namespace; return (RegistryClient, org, signing) or None."""
    items = _custom().list_namespaced_custom_object(OPENAN, "v1alpha1", namespace, "openans")["items"]
    cr = stack.pick_cr(items)
    if cr is None:
        return None
    n, spec = cr["metadata"]["name"], cr["spec"]
    rc_url = f"http://{stack.release_name(n, 'registryCenter')}-registry-center.{namespace}.svc.cluster.local:5000"
    if os.environ.get("REGISTRY_URL_OVERRIDE"):
        rc_url = os.environ["REGISTRY_URL_OVERRIDE"]
    token = decode_secret(_core().read_namespaced_secret(stack.secret_name(n, "operator-token"), namespace).data, "token")
    org = spec.get("registry", {}).get("organization", "openan")
    signing = None
    if spec.get("cardSync", {}).get("signing", {}).get("enabled", True):
        sec = _core().read_namespaced_secret(stack.secret_name(n, "signing-key"), namespace).data
        kid = f"{n}-operator"
        jku = spec.get("cardSync", {}).get("signing", {}).get("jku", "")
        signing = {"private": decode_secret(sec, "private.pem"), "kid": kid, "jku": jku,
                   "jwk": public_jwk(decode_secret(sec, "public.pem"), kid),
                   "trust_store": not jku, "trust_cm": stack.secret_name(n, "registry-trust"), "cr": cr}
    return RegistryClient(rc_url, token), org, signing


def publish_trust_key(signing: Dict[str, Any], namespace: str, card_name: str) -> None:
    """Make the operator's public key trusted by the Registry for this card (local key store)."""
    cm, key = signing["trust_cm"], f"{card_name}.json"
    doc = trust_document(signing["jwk"])
    try:
        cur = _core().read_namespaced_config_map(cm, namespace)
    except client.ApiException as e:
        if e.status != 404:
            raise
        cr = signing["cr"]
        _core().create_namespaced_config_map(namespace, client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name=cm, namespace=namespace, labels={"app.kubernetes.io/managed-by": "openan-operator"},
                owner_references=[client.V1OwnerReference(api_version=f"{OPENAN}/v1alpha1", kind="OpenAN",
                    name=cr["metadata"]["name"], uid=cr["metadata"]["uid"])]), data={key: doc}))
        return
    if (cur.data or {}).get(key) != doc:
        _core().patch_namespaced_config_map(cm, namespace, {"data": {key: doc}})


def remove_trust_key(signing: Dict[str, Any], namespace: str, card_name: str) -> None:
    try:
        _core().patch_namespaced_config_map(signing["trust_cm"], namespace, {"data": {f"{card_name}.json": None}})
    except client.ApiException as e:
        if e.status != 404:
            raise


def _agent_sync_cr(namespace: str) -> Optional[Dict[str, Any]]:
    return stack.pick_cr(_custom().list_namespaced_custom_object(OPENAN, "v1alpha1", namespace, "openans")["items"],
                         "agentSync")


def _parent_origin(meta: Dict[str, Any], namespace: str) -> Optional[bool]:
    """True: the ExposedAPI belongs to a registry-made Component; False: to any other (or none); None: its
    Component is gone (being deleted: nothing to publish)."""
    parent = agent_sync_io.parent_component(meta)
    if not parent:
        return False
    try:
        comp = _custom().get_namespaced_custom_object(ODA, "v1", namespace, "components", parent)
    except client.ApiException as e:
        if e.status == 404:
            return None
        raise
    return agent_sync.is_registry_originated(comp)


def _external_registries(namespace: str, logger) -> List[Any]:
    """[(RegistryRef, writer RegistryClient, reader source)] of the connected registries in the namespace."""
    cr = _agent_sync_cr(namespace)
    if cr is None:
        return []
    out = []
    for reg in agent_sync_io.discover_registries(_apis(), namespace, cr):
        if reg.managed:
            continue
        try:
            reader, writer = agent_sync_io.credentials(_apis(), reg, cr)
        except SourceUnavailable as e:
            logger.warning(f"registry {reg.id} skipped: {e}")
            continue
        out.append((reg, RegistryClient(reg.base_url, writer) if writer else None, agent_sync_io.source_for(reg, reader)))
    return out


def _find_card(source, org: str, card_name: str) -> Optional[Dict[str, Any]]:
    """Look a card up in the complete listing: a plain GET only sees published cards."""
    return next((c for c, _ in source.list_agents() if agent_sync.card_key(c) == (org, card_name)), None)


def _publish_external(card: Dict[str, Any], me: str, meta: Dict[str, Any], namespace: str,
                      signing: Optional[Dict[str, Any]], patch, logger) -> None:
    targets = [t for t in _external_registries(namespace, logger) if t[0].publish and t[1] is not None]
    if not targets:
        return
    if not (signing and signing.get("jku")):
        logger.warning("not publishing to connected registries: they verify cards through cardSync.signing.jku, "
                       "an HTTPS key URL they trust, which is not set")
        return
    try:
        published = json.loads((meta.get("annotations") or {}).get(agent_sync.REGISTERED_CARDS) or "{}")
    except ValueError:
        published = {}
    failed = []
    for reg, writer, _source in targets:
        try:
            action = writer.upsert(card, owner=me)
            published[reg.id] = f"{card['provider']['organization']}/{card['name']}"
            metrics.CARD_SYNC.labels(action, "ok").inc()
        except OwnershipError as e:  # never blocks the other registries
            metrics.CARD_SYNC.labels("upsert", "rejected").inc()
            logger.warning(f"{reg.id}: {e}")
        except (RegistryError, httpx.HTTPError) as e:
            metrics.CARD_SYNC.labels("upsert", "error").inc()
            failed.append(f"{reg.id}: {e}")
    patch.metadata.annotations[agent_sync.REGISTERED_CARDS] = json.dumps(published, sort_keys=True)
    if failed:
        raise kopf.TemporaryError(f"publishing to connected registries failed: {'; '.join(failed)}", delay=60)


@kopf.on.create(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.on.update(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.on.resume(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.timer(ODA, "v1", "exposedapis", interval=300, initial_delay=60, when=lambda spec, **_: is_a2a(spec))
def sync_card(spec, name, namespace, meta, status, patch, logger, **_):
    # Loop guard: agents made from a registry card are that registry's, never published back from here.
    if _parent_origin(meta, namespace) is not False:
        return
    target = _registry_for(namespace)
    if target is None:
        return
    rc, org, signing = target
    try:
        raw = fetch_card(candidate_urls(spec, namespace))
        me = owner_ref(namespace, name, meta["uid"])
        gateway = [(status or {}).get("apiStatus", {}).get("url", "")]
        card = build_signed_card(raw, org, signing, owner=me, hosts=allowed_hosts(spec, namespace, gateway))
        existing = rc.get(org, card["name"])
        if existing is not None and card_owner(existing) != me:
            raise OwnershipError(f"card {org}/{card['name']} is owned by another source ({card_owner(existing) or 'unknown'})")
        if signing and signing.get("trust_store"):
            publish_trust_key(signing, namespace, card["name"])
        action = rc.upsert(card, owner=me)
    except OwnershipError as e:
        metrics.CARD_SYNC.labels("upsert", "rejected").inc()
        raise kopf.PermanentError(str(e))
    except CardError as e:
        metrics.CARD_SYNC.labels("upsert", "invalid").inc()
        raise kopf.TemporaryError(str(e), delay=60)
    except RegistryError as e:
        metrics.CARD_SYNC.labels("upsert", "error").inc()
        raise kopf.TemporaryError(str(e), delay=60)
    metrics.CARD_SYNC.labels(action, "ok").inc()
    patch.metadata.annotations[ANNOTATION] = f"{org}/{card['name']}"
    logger.info(f"agent card {org}/{card['name']} {action}")
    _publish_external(card, me, meta, namespace, signing, patch, logger)


def _managed_reader(namespace: str):
    """Reader of the CR's own registry when agent sync is on (sees unpublished cards too), else None."""
    cr = _agent_sync_cr(namespace)
    if cr is None:
        return None
    for reg in agent_sync_io.discover_registries(_apis(), namespace, cr):
        if reg.managed:
            try:
                return agent_sync_io.source_for(reg, agent_sync_io.credentials(_apis(), reg, cr)[0])
            except SourceUnavailable:
                return None
    return None


@kopf.on.delete(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
def remove_card(name, meta, namespace, logger, **_):
    """Delete the card only when the Registry says this very ExposedAPI owns it: the annotations are editable
    by anyone who can patch the ExposedAPI and must not be trusted on their own."""
    me = owner_ref(namespace, name, meta["uid"])
    try:
        _remove_managed_card(meta, namespace, me, logger)
        _remove_external_cards(meta, namespace, me, logger)
    except (RegistryError, SourceUnavailable, client.ApiException, httpx.HTTPError) as e:
        raise kopf.TemporaryError(str(e), delay=30)


def _remove_managed_card(meta: Dict[str, Any], namespace: str, me: str, logger) -> None:
    ref = (meta.get("annotations") or {}).get(ANNOTATION)
    target = _registry_for(namespace)
    if not ref or target is None:
        return
    rc, configured_org, signing = target
    org, _, card_name = ref.partition("/")
    if org != configured_org:
        logger.warning(f"not deleting {ref}: organization differs from the configured one")
        return
    existing = rc.get(org, card_name)
    if existing is None:
        reader = _managed_reader(namespace)
        existing = _find_card(reader, org, card_name) if reader is not None else None
    if existing is None:
        return
    if card_owner(existing) != me:
        logger.warning(f"not deleting {ref}: the card is owned by {card_owner(existing) or 'another source'}")
        return
    rc.delete(org, card_name)
    if signing and signing.get("trust_store"):
        remove_trust_key(signing, namespace, card_name)


def _remove_external_cards(meta: Dict[str, Any], namespace: str, me: str, logger) -> None:
    try:
        published = json.loads((meta.get("annotations") or {}).get(agent_sync.REGISTERED_CARDS) or "{}")
    except ValueError:
        return
    if not published:
        return
    for reg, writer, source in _external_registries(namespace, logger):
        ref = published.get(reg.id)
        if not ref or writer is None:
            continue
        org, _, card_name = ref.partition("/")
        existing = _find_card(source, org, card_name)
        if existing is None:
            continue
        if card_owner(existing) != me:
            logger.warning(f"not deleting {ref} in {reg.id}: owned by {card_owner(existing) or 'another source'}")
            continue
        writer.delete(org, card_name)


# ---- agent sync: registries -> Agent ODA Components -----------------------------------------------------------

DEFAULT_PROXY_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"


def _agent_settings() -> agent_sync_io.Settings:
    prefixes = tuple(p for p in os.environ.get("ALLOWED_IMAGE_PREFIXES", "").split(",") if p) or stack.DEFAULT_IMAGE_PREFIXES
    suffixes = tuple(s.strip() for s in os.environ.get("AGENT_UPSTREAM_ALLOWED_HOSTS", "").split(",") if s.strip())
    # The proxy image is the operator's own configuration, not something a card can choose: it is not subject to
    # the card image allow-list, only to being pinned.
    proxy = os.environ.get("AGENT_PROXY_IMAGE", DEFAULT_PROXY_IMAGE)
    if ":" not in proxy.rsplit("/", 1)[-1] or proxy.endswith(":latest"):
        raise kopf.PermanentError(f"AGENT_PROXY_IMAGE {proxy!r} must be pinned to a tag or digest")
    return agent_sync_io.Settings(prefixes, proxy, suffixes,
                                  int(os.environ.get("AGENT_SYNC_RESYNC_SECONDS", "300")),
                                  int(os.environ.get("AGENT_SYNC_TMF639_SECONDS", "60")))


@kopf.timer(OPENAN, "v1alpha1", "openans", interval=15, initial_delay=20,
            when=lambda spec, **_: stack.agent_sync_enabled(spec))
def agent_sync_timer(name, namespace, body, logger, **_):
    """One driver on the OpenAN CR (which already carries our finalizer): kopf timers add a finalizer to what they
    watch, so no handler may run on registry Components, which belong to Helm or to whoever installed them."""
    cr = _agent_sync_cr(namespace)
    if cr is None or cr["metadata"]["name"] != name:
        return
    settings = _agent_settings()
    regs = agent_sync_io.discover_registries(_apis(), namespace, cr)
    for stale in set(agent_sync_io.STATE) - {r.id for r in regs}:
        agent_sync_io.STATE.pop(stale, None)
    for reg in regs:
        st = agent_sync_io.STATE.setdefault(reg.id, agent_sync_io.State())
        try:
            res = agent_sync_io.run_pass(_apis(), reg, cr, settings, logger=logger)
        except SourceUnavailable as e:
            metrics.AGENT_SYNC.labels("registry", "unavailable").inc()
            if not st.down:  # one event per outage, not one per tick
                logger.warning(f"registry {reg.id} unavailable, nothing changed: {e}")
            st.down = True
            continue
        except client.ApiException as e:
            metrics.AGENT_SYNC.labels("registry", "error").inc()
            logger.warning(f"registry {reg.id}: Kubernetes API error {e.status}: {e.reason}")
            continue
        if st.down:
            logger.info(f"registry {reg.id} reachable again")
            st.down = False
        for obj, action in res.applied.items():
            metrics.AGENT_SYNC.labels(action, "ok").inc()
            if action != "unchanged":
                logger.info(f"agent {obj} {action} from {reg.id}")
        for obj in res.deleted:
            metrics.AGENT_SYNC.labels("deleted", "ok").inc()
            logger.info(f"agent {obj} deleted: its card left {reg.id}")
        for obj in res.k8s_deleted:
            metrics.AGENT_SYNC.labels("k8s-deleted", "ok").inc()
        for w in res.warnings:
            logger.warning(f"{reg.id}: {w}")


@kopf.on.delete(ODA, "v1", "components", labels={agent_sync.ORIGIN_LABEL: agent_sync.ORIGIN_REGISTRY})
def release_agent(name, namespace, meta, logger, **_):
    """A registry-made Component is being deleted: delete its card too, or hold the deletion (finalizer) while the
    registry refuses. Static label filter: the finalizer only ever lands on Components this operator made."""
    try:
        outcome = agent_sync_io.release(_apis(), meta, namespace, _agent_sync_cr(namespace))
    except agent_sync_io.DeleteRefused as e:
        metrics.AGENT_SYNC.labels("release", "refused").inc()
        logger.warning(str(e))
        raise kopf.TemporaryError(str(e), delay=60)
    except (SourceUnavailable, client.ApiException) as e:
        raise kopf.TemporaryError(f"cannot check the card of {name}: {e}", delay=30)
    metrics.AGENT_SYNC.labels("release", "ok").inc()
    logger.info(f"agent {name} released ({outcome})")
