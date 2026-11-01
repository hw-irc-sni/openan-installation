# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure logic: values for the three component charts and OpenAN CR status conditions."""
from datetime import datetime, timezone
from typing import Any, Dict, List

COMPONENTS = ("registryCenter", "orchestrationCenter", "frontend")
CHART_DIR = {"registryCenter": "openan-registry-center", "orchestrationCenter": "openan-orchestration-center",
             "frontend": "openan-frontend"}
# Generated values a CR must not change: they decide where credentials are mounted and where they are sent.
PINNED = {"registryCenter": ("runtimeSecret", "credentialSecret", "trust"),
          "orchestrationCenter": ("runtimeSecret", "registryUrl"),
          "frontend": ("backendUrl", "registryUrl")}
# Non-secret env maps rendered into ConfigMaps (after the generated keys, so they would win): these keys are
# security relevant and rejected, not silently dropped.
EXTRA_ENV = {"registryCenter": ("registry", "extraEnv"), "orchestrationCenter": ("orchestration", "extraEnv")}
FORBIDDEN_ENV = {"AGENT_REGISTRY_URL", "REGISTRY_ACCESS_TOKEN", "ORCH_ACCESS_PASSWORD", "OC_ADMIN_INITIAL_PASSWORD",
                 "ORCH_EXTERNAL_AUTH_MODE", "REGISTRY_INTEGRATION_AUTH_MODE", "REGISTRY_OWNER_IDENTITY_MODE",
                 "REGISTRY_SIGNATURE_VALIDATION_ENABLED", "INTEGRATION_TOKEN_HMAC_KEY", "INTEGRATION_FINGERPRINT_KEY"}
DEFAULT_IMAGE_PREFIXES = ("ghcr.io/project-openan/",)


class ValuesError(ValueError):
    """The CR carries values the operator refuses to render."""


def validate_values(comp: str, values: Dict[str, Any], allowed_prefixes=DEFAULT_IMAGE_PREFIXES) -> None:
    repo = (values.get("image") or {}).get("repository")
    if repo and not any(repo.startswith(p) for p in allowed_prefixes):
        raise ValuesError(f"{comp}: image.repository {repo!r} is outside the allowed registries {list(allowed_prefixes)}")
    if comp in EXTRA_ENV:
        section, key = EXTRA_ENV[comp]
        bad = sorted(k for k in (values.get(section) or {}).get(key, {}) if k.upper() in FORBIDDEN_ENV)
        if bad:
            raise ValuesError(f"{comp}: {section}.{key} may not set {bad}")
SUFFIX = {"registryCenter": "registry-center", "orchestrationCenter": "orchestration-center", "frontend": "frontend"}


def release_name(cr_name: str, comp: str) -> str:
    return f"{cr_name}-{SUFFIX[comp]}"[:53].rstrip("-")


def component_object_name(cr_name: str, comp: str, component_name: str) -> str:
    return f"{release_name(cr_name, comp)}-{component_name}"


def secret_name(cr_name: str, suffix: str) -> str:
    return f"{cr_name}-{suffix}"


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def chart_values(cr_name: str, spec: Dict[str, Any], comp: str) -> Dict[str, Any]:
    """Helm values for one component. User 'values' override generated ones; image tag stays required."""
    rc_rel = release_name(cr_name, "registryCenter")
    oc_rel = release_name(cr_name, "orchestrationCenter")
    sig_off = not (spec.get("cardSync", {}).get("signing", {}).get("enabled", True))
    gen: Dict[str, Any]
    if comp == "registryCenter":
        gen = {"runtimeSecret": secret_name(cr_name, "registry-runtime"),
               "credentialSecret": secret_name(cr_name, "registry-credentials"),
               "registry": {"signatureValidation": not sig_off}}
        if not sig_off:  # keys of signed cards live in an operator-managed ConfigMap (see cards.trust_document)
            gen["trust"] = {"organization": spec.get("registry", {}).get("organization", "openan"),
                            "configMap": secret_name(cr_name, "registry-trust")}
    elif comp == "orchestrationCenter":
        gen = {"runtimeSecret": secret_name(cr_name, "orchestration-runtime"),
               "registryUrl": f"http://{rc_rel}-registry-center:5000"}
    else:
        gen = {"backendUrl": f"http://{oc_rel}-orchestration-center:5001",
               "registryUrl": f"http://{rc_rel}-registry-center:5000"}
    out = _merge(gen, spec.get(comp, {}).get("values", {}))
    # A CR author must not redirect the operator's credentials: Secret references and the trust store are
    # always the generated ones (the pod is created with the operator's, not the author's, permissions).
    for key in PINNED.get(comp, ()):
        if key in gen:
            out[key] = gen[key]
        else:
            out.pop(key, None)
    return out


def enabled_components(spec: Dict[str, Any]) -> List[str]:
    return [c for c in COMPONENTS if spec.get(c, {}).get("enabled", True)]


def condition(ctype: str, status: bool, reason: str, message: str = "") -> Dict[str, str]:
    return {"type": ctype, "status": "True" if status else "False", "reason": reason, "message": message,
            "lastTransitionTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def is_ready(component_cr: Dict[str, Any] | None) -> bool:
    """ODA Component is ready when the Canvas component operator reports deployment_status == Complete."""
    st = (component_cr or {}).get("status", {})
    return (st.get("summary/status", {}) or {}).get("deployment_status") == "Complete"


def pick_cr(items):
    """The OpenAN CR that card sync talks to: oldest first, and only one that reconciled to Ready, so a
    rejected or half-installed second CR in the namespace can never become the sync target."""
    def ready(i):
        return any(c.get("type") == "Ready" and c.get("status") == "True" for c in (i.get("status") or {}).get("conditions", []))
    usable = [i for i in items if ready(i) and i.get("spec", {}).get("cardSync", {}).get("enabled", True)]
    return min(usable, key=lambda i: i["metadata"].get("creationTimestamp", ""), default=None)
