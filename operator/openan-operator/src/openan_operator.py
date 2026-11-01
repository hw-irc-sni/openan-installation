# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""OpenAN operator: OpenAN CR lifecycle (Helm-managed ODA Components) + a2a ExposedAPI -> Registry cards."""
import logging
import os
from typing import Any, Dict

import kopf
from kubernetes import client, config

from . import helm_driver, metrics, stack
from .card_signer import public_jwk
from .cards import ANNOTATION, OwnershipError, allowed_hosts, build_signed_card, card_owner, owner_ref, candidate_urls, decode_secret, fetch_card, is_a2a, trust_document
from .card_mapper import CardError
from .registry_client import RegistryClient, RegistryError
from .secrets_bootstrap import new_stack_secrets

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
    ensure_secrets(name, namespace, body)
    conds, comps = [], {}
    prior_ready = True
    for comp in stack.enabled_components(spec):  # ordered RC -> OC -> frontend
        if not prior_ready:
            break
        if not spec.get(comp, {}).get("values", {}).get("image", {}).get("tag"):
            raise kopf.PermanentError(f"{comp}: values.image.tag is required (no default)")
        rel = stack.release_name(name, comp)
        try:
            helm_driver.upgrade_install(rel, os.path.join(CHARTS_DIR, stack.CHART_DIR[comp]), namespace,
                                        stack.chart_values(name, spec, comp))
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
    """Helm keeps the data PVCs (resource-policy: keep); remove them unless spec.retainData."""
    _core().delete_collection_namespaced_persistent_volume_claim(
        namespace, label_selector=f"app.kubernetes.io/instance={release}")


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
    items = [i for i in items if i.get("spec", {}).get("cardSync", {}).get("enabled", True)]
    if not items:
        return None
    cr = items[0]
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


@kopf.on.create(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.on.update(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.on.resume(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
@kopf.timer(ODA, "v1", "exposedapis", interval=300, initial_delay=60, when=lambda spec, **_: is_a2a(spec))
def sync_card(spec, name, namespace, meta, status, patch, logger, **_):
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
        action = rc.upsert(card)
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


@kopf.on.delete(ODA, "v1", "exposedapis", when=lambda spec, **_: is_a2a(spec))
def remove_card(meta, namespace, logger, **_):
    ref = (meta.get("annotations") or {}).get(ANNOTATION)
    target = _registry_for(namespace)
    if not ref or target is None:
        return
    org, _, card_name = ref.partition("/")
    try:
        target[0].delete(org, card_name)
        if target[2] and target[2].get("trust_store"):
            remove_trust_key(target[2], namespace, card_name)
    except (RegistryError, client.ApiException) as e:
        raise kopf.TemporaryError(str(e), delay=30)
