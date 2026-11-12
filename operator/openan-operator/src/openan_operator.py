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
from typing import Any, Dict, List, Optional, Tuple

import httpx
import kopf
from kubernetes import client, config

from . import agent_sync, agent_sync_io, helm_driver, metrics, stack
from .card_signer import public_jwk
from .cards import ANNOTATION, OwnershipError, allowed_hosts, build_signed_card, card_owner, owner_ref, candidate_urls, decode_secret, fetch_card, is_a2a, trust_document
from .card_mapper import CardError
from .registry_client import RegistryClient, RegistryError
from .registry_source import SourceUnavailable, find_agent
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
    return agent_sync_io.Apis(_core(), _custom(), _apps())


def _env_list(name: str, default: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    """Comma-separated operator setting; blanks dropped; `default` when empty."""
    return tuple(v.strip() for v in os.environ.get(name, "").split(",") if v.strip()) or default


def _openan_owner(owner: Dict[str, Any]) -> client.V1OwnerReference:
    return client.V1OwnerReference(api_version=f"{OPENAN}/v1alpha1", kind="OpenAN", name=owner["metadata"]["name"],
                                   uid=owner["metadata"]["uid"], block_owner_deletion=True)


def _new_secret(name: str, namespace: str, owner: Dict[str, Any], string_data: Dict[str, str]) -> client.V1Secret:
    return client.V1Secret(metadata=client.V1ObjectMeta(
        name=name, namespace=namespace, labels={agent_sync.MANAGED_BY_KEY: agent_sync.MANAGED_BY_VALUE},
        owner_references=[_openan_owner(owner)]), string_data=string_data)


def ensure_secrets(name: str, namespace: str, owner: Dict[str, Any]) -> None:
    """Create-once: existing Secrets are never rewritten (tokens/keys must stay stable)."""
    generated = None
    for suffix in ("registry-runtime", "registry-credentials", "operator-token", "orchestration-runtime", "signing-key"):
        sname = stack.secret_name(name, suffix)
        existing = agent_sync_io.ignore_404(_core().read_namespaced_secret, sname, namespace)
        if existing is not None:
            if not existing.metadata.owner_references:  # detached by an earlier retainData delete: re-adopt
                _core().patch_namespaced_secret(sname, namespace, {"metadata": {"ownerReferences": [
                    client.ApiClient().sanitize_for_serialization(_openan_owner(owner))]}})
            continue
        generated = generated or new_stack_secrets(f"{name}")
        _core().create_namespaced_secret(namespace, _new_secret(sname, namespace, owner, generated[suffix]))


def ensure_sync_credential(name: str, namespace: str, owner: Dict[str, Any]) -> str:
    """Agent sync reads the registry with its own nms_oss identity. Added to existing installs too: the token
    Secret first (so a crash in between re-adds the same token), then the entry in the credentials file.
    Returns the hash of the credentials file, which restarts the Registry Center when it changes."""
    core = _core()
    cred_name, tok_name = stack.secret_name(name, "registry-credentials"), stack.secret_name(name, "sync-token")
    conf = decode_secret(core.read_namespaced_secret(cred_name, namespace).data, "integration_credentials.conf")
    existing = agent_sync_io.ignore_404(core.read_namespaced_secret, tok_name, namespace)
    if existing is not None:
        token = decode_secret(existing.data, "token")
    else:
        token = secrets.token_urlsafe(48)
        core.create_namespaced_secret(namespace, _new_secret(tok_name, namespace, owner, {"token": token}))
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
    prefixes = _env_list("ALLOWED_IMAGE_PREFIXES", stack.DEFAULT_IMAGE_PREFIXES)
    spec_prefixes = _env_list("ALLOWED_SPEC_URL_PREFIXES", stack.DEFAULT_SPEC_URL_PREFIXES)
    callback_hosts = _env_list("ALLOWED_CALLBACK_HOSTS")
    callback_cas = _env_list("ALLOWED_CALLBACK_CA_CONFIGMAPS")
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


def _connections(namespace: str, cr: Optional[Dict[str, Any]], logger) -> List[agent_sync_io.Connection]:
    """Every registry of the namespace with its credentials, resolved once per handler call ([] while agent sync
    is off)."""
    if cr is None:
        return []
    conns, problems = agent_sync_io.connections(_apis(), namespace, cr, _agent_settings().deploy_registries)
    for problem in problems:
        logger.warning(problem)
    return conns


def _registry_made_parent(meta: Dict[str, Any], namespace: str) -> bool:
    """The ExposedAPI belongs to a registry-made Component, or to one that is gone: nothing to publish."""
    parent = agent_sync.parent_component(meta)
    if not parent:
        return False
    comp = agent_sync_io.ignore_404(_custom().get_namespaced_custom_object, ODA, "v1", namespace, "components", parent)
    return comp is None or agent_sync.is_registry_originated(comp)


def _publish_external(card: Dict[str, Any], me: str, meta: Dict[str, Any], conns: List[agent_sync_io.Connection],
                      signing: Optional[Dict[str, Any]], patch, logger) -> None:
    """Publish to the connected registries that opted in (native API, write token). Only after their first agent
    sync pass, like the CR's own registry (see sync_card)."""
    targets = [c for c in conns if not c.reg.managed and c.reg.publish and c.writer
               and agent_sync_io.first_pass_done(c.reg.id)]
    if not targets:
        return
    if not (signing and signing.get("jku")):
        logger.warning("not publishing to connected registries: they verify cards through cardSync.signing.jku, "
                       "an HTTPS key URL they trust, which is not set")
        return
    published, failed = agent_sync.registered_cards(meta), []
    for conn in targets:
        try:
            action = conn.write_client().upsert(card, owner=me)
            published[conn.reg.id] = agent_sync.format_key(*agent_sync.card_key(card))
            metrics.CARD_SYNC.labels(action, "ok").inc()
        except OwnershipError as e:  # never blocks the other registries
            metrics.CARD_SYNC.labels("upsert", "rejected").inc()
            logger.warning(f"{conn.reg.id}: {e}")
        except (RegistryError, httpx.HTTPError) as e:
            metrics.CARD_SYNC.labels("upsert", "error").inc()
            failed.append(f"{conn.reg.id}: {e}")
    patch.metadata.annotations[agent_sync.REGISTERED_CARDS] = json.dumps(published, sort_keys=True)
    if failed:
        raise kopf.TemporaryError(f"publishing to connected registries failed: {'; '.join(failed)}", delay=60)


@kopf.on.create(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.on.update(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.on.resume(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.timer(ODA, "v1", "exposedapis", interval=300, initial_delay=60, when=lambda spec, **_: is_a2a(spec))
def sync_card(spec, name, namespace, meta, status, patch, logger, **_):
    # Loop guard: agents made from a registry card are that registry's, never published back from here.
    if _registry_made_parent(meta, namespace):
        return
    target = _registry_for(namespace)
    if target is None:
        return
    rc, org, signing = target
    conns = _connections(namespace, _agent_sync_cr(namespace), logger)
    # After a restart the agent sync first replays the changes made while the operator was down: a card deleted
    # then must delete its Component, not be republished by this handler before the replay ran.
    if any(c.reg.managed and not agent_sync_io.first_pass_done(c.reg.id) for c in conns):
        raise kopf.TemporaryError("waiting for the first agent sync pass of the Registry Center", delay=15)
    try:
        raw = fetch_card(candidate_urls(spec, namespace))
        me = owner_ref(namespace, name, meta["uid"])
        gateway = [(status or {}).get("apiStatus", {}).get("url", "")]
        card = build_signed_card(raw, org, signing, owner=me, hosts=allowed_hosts(spec, namespace, gateway))
        # upsert repeats this check; it is done first so a foreign card's name never gets our key in the trust store
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
    patch.metadata.annotations[ANNOTATION] = agent_sync.format_key(org, card["name"])
    logger.info(f"agent card {org}/{card['name']} {action}")
    _publish_external(card, me, meta, conns, signing, patch, logger)


@kopf.on.delete(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
def remove_card(name, meta, namespace, logger, **_):
    """Delete the card only when the Registry says this very ExposedAPI owns it: the annotations are editable
    by anyone who can patch the ExposedAPI and must not be trusted on their own."""
    me = owner_ref(namespace, name, meta["uid"])
    try:
        conns = _connections(namespace, _agent_sync_cr(namespace), logger)
        _remove_managed_card(meta, namespace, me, conns, logger)
        _remove_external_cards(meta, me, conns, logger)
    except (RegistryError, SourceUnavailable, client.ApiException, httpx.HTTPError) as e:
        raise kopf.TemporaryError(str(e), delay=30)


def _remove_managed_card(meta: Dict[str, Any], namespace: str, me: str, conns: List[agent_sync_io.Connection],
                         logger) -> None:
    ref = (meta.get("annotations") or {}).get(ANNOTATION)
    target = _registry_for(namespace)
    key = agent_sync.parse_key(ref)
    if key is None or target is None:
        return
    rc, configured_org, signing = target
    if key[0] != configured_org:
        logger.warning(f"not deleting {ref}: organization differs from the configured one")
        return
    existing = rc.get(*key)
    managed = next((c for c in conns if c.reg.managed), None)
    if existing is None and managed is not None:  # a plain GET does not see unpublished cards
        found = find_agent(managed.source(), key)
        existing = found[0] if found else None
    if existing is None:
        return
    if card_owner(existing) != me:
        logger.warning(f"not deleting {ref}: the card is owned by {card_owner(existing) or 'another source'}")
        return
    rc.delete(*key)
    if signing and signing.get("trust_store"):
        remove_trust_key(signing, namespace, key[1])


def _remove_external_cards(meta: Dict[str, Any], me: str, conns: List[agent_sync_io.Connection], logger) -> None:
    published = agent_sync.registered_cards(meta)
    for conn in conns:
        key = agent_sync.parse_key(published.get(conn.reg.id)) if not conn.reg.managed else None
        if key is None or not conn.writer:
            continue
        found = find_agent(conn.source(), key)
        if found is None:
            continue
        if card_owner(found[0]) != me:
            logger.warning(f"not deleting {published[conn.reg.id]} in {conn.reg.id}: owned by "
                           f"{card_owner(found[0]) or 'another source'}")
            continue
        conn.write_client().delete(*key)


# ---- agent sync: registries -> Agent ODA Components -----------------------------------------------------------

DEFAULT_PROXY_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"


def _agent_settings() -> agent_sync_io.Settings:
    # The proxy image is the operator's own configuration, not something a card can choose: it is not subject to
    # the card image allow-list, only to being pinned.
    proxy = os.environ.get("AGENT_PROXY_IMAGE", DEFAULT_PROXY_IMAGE)
    if ":" not in proxy.rsplit("/", 1)[-1] or proxy.endswith(":latest"):
        raise kopf.PermanentError(f"AGENT_PROXY_IMAGE {proxy!r} must be pinned to a tag or digest")
    return agent_sync_io.Settings(
        image_prefixes=_env_list("ALLOWED_IMAGE_PREFIXES", stack.DEFAULT_IMAGE_PREFIXES), proxy_image=proxy,
        upstream_suffixes=_env_list("AGENT_UPSTREAM_ALLOWED_HOSTS"),
        # "<registry Component>=<endpoint>" pairs, e.g. "partner=http://partner.components.svc.cluster.local:8080"
        deploy_registries=_env_list("AGENT_DEPLOY_REGISTRIES"),
        resync_seconds=int(os.environ.get("AGENT_SYNC_RESYNC_SECONDS", "300")),
        tmf639_seconds=int(os.environ.get("AGENT_SYNC_TMF639_SECONDS", "60")))


def _report(reg: agent_sync.RegistryRef, res: agent_sync_io.PassResult, logger) -> None:
    for obj, action in res.applied.items():
        metrics.AGENT_SYNC.labels(action, "ok").inc()
        if action != "unchanged":
            logger.info(f"agent {obj} {action} from {reg.id}")
    for obj in res.mirrors_removed:
        metrics.AGENT_SYNC.labels("mirror-deleted", "ok").inc()
        logger.info(f"agent {obj} deleted: its card left {reg.id}")
    for obj in res.components_removed:
        metrics.AGENT_SYNC.labels("component-deleted", "ok").inc()
        logger.warning(f"Component {obj} deleted: its card was deleted in {reg.id}")
    for warning in res.warnings:
        logger.warning(f"{reg.id}: {warning}")


@kopf.timer(OPENAN, "v1alpha1", "openans", interval=15, initial_delay=20,
            when=lambda spec, **_: stack.agent_sync_enabled(spec))
def agent_sync_timer(name, namespace, status, patch, logger, **_):
    """One driver on the OpenAN CR (which already carries our finalizer): kopf timers add a finalizer to what they
    watch, so no handler may run on registry Components, which belong to Helm or to whoever installed them. The
    change-feed cursors are kept in the CR status so a restart replays what happened while the operator was down."""
    cr = _agent_sync_cr(namespace)
    if cr is None or cr["metadata"]["name"] != name:
        return
    settings, apis = _agent_settings(), _apis()
    regs = agent_sync_io.discover_registries(apis, namespace, cr, settings.deploy_registries)
    agent_sync_io.forget_except({r.id for r in regs})
    stored = dict(((status or {}).get("agentSync") or {}).get("cursors") or {})
    for reg in regs:
        st = agent_sync_io.state_for(reg.id)
        try:
            res = agent_sync_io.run_pass(apis, reg, cr, settings, stored_cursor=stored.get(reg.id))
        except SourceUnavailable as e:
            metrics.AGENT_SYNC.labels("read", "unavailable").inc()
            if not st.down:  # one event per outage, not one per tick
                logger.warning(f"registry {reg.id} unavailable, nothing changed: {e}")
            st.down = True
            continue
        except client.ApiException as e:
            metrics.AGENT_SYNC.labels("read", "kubernetes-error").inc()
            logger.warning(f"registry {reg.id}: Kubernetes API error {e.status}: {e.reason}")
            continue
        if st.down:
            logger.info(f"registry {reg.id} reachable again")
            st.down = False
        _report(reg, res, logger)
    cursors = {r.id: agent_sync_io.STATE[r.id].cursor for r in regs if agent_sync_io.STATE[r.id].cursor is not None}
    if cursors != stored:  # merge patch: registries that are gone are removed with null
        patch.status["agentSync"] = {"cursors": {**{k: None for k in stored if k not in cursors}, **cursors}}


@kopf.on.delete(ODA, "v1", "components", labels={agent_sync.ORIGIN_LABEL: agent_sync.ORIGIN_REGISTRY})
def release_agent(name, namespace, meta, logger, **_):
    """A registry-made Component is being deleted: delete its card too, or hold the deletion (finalizer) while the
    registry refuses. Static label filter: the finalizer only ever lands on Components this operator made."""
    cr = _agent_sync_cr(namespace)
    if cr is None:
        logger.info(f"agent {name} released (agent sync is off)")
        return
    try:
        outcome = agent_sync_io.release(_apis(), meta, namespace, cr, _agent_settings())
    except agent_sync_io.DeleteRefused as e:
        metrics.AGENT_SYNC.labels("release", "refused").inc()
        logger.warning(str(e))
        raise kopf.TemporaryError(str(e), delay=60)
    except (SourceUnavailable, client.ApiException) as e:
        raise kopf.TemporaryError(f"cannot check the card of {name}: {e}", delay=30)
    metrics.AGENT_SYNC.labels("release", "ok").inc()
    logger.info(f"agent {name} released ({outcome})")
