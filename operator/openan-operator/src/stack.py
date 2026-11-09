# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure logic: values for the three component charts and OpenAN CR status conditions."""
import re
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
# Keys the chart ConfigMap generates itself (kept in sync with the templates by a contract test). The template
# merges them last, so an extraEnv entry can never change them; they are also rejected here, to fail early.
GENERATED_ENV = {
    "registryCenter": frozenset({
        "REGISTRY_IP", "REGISTRY_PORT", "REGISTRY_ENABLE_HTTPS", "REGISTRY_VERIFY_CLIENT", "REGISTRY_FORWARDED_ALLOW_IPS",
        "REGISTRY_STARTUP_STRICT_IDENTITY", "REGISTRY_OWNER_IDENTITY_MODE", "REGISTRY_INTEGRATION_AUTH_MODE",
        "REGISTRY_REGISTRY_PUBLIC_BASE_URL", "REGISTRY_REGISTRY_SIGN_ENABLED", "REGISTRY_SIGNATURE_VALIDATION_ENABLED",
        "REGISTRY_TMF639_ENABLED", "REGISTRY_PATH_PREFIX", "REGISTRY_BROADCAST_ENABLED", "REGISTRY_BROADCAST_CALLBACK_ALLOWLIST",
        "REGISTRY_BROADCAST_ALLOW_HTTP_CALLBACKS", "REGISTRY_BROADCAST_CALLBACK_CA_FILE", "PERSISTENCE_MODE", "LLM_CONFIG_FILE"}),
    "orchestrationCenter": frozenset({
        "ORCH_IP", "ORCH_PORT", "ORCH_ENABLE_HTTPS", "ORCH_VERIFY_CLIENT", "ORCH_FORWARDED_ALLOW_IPS",
        "ORCH_EXTERNAL_AUTH_MODE", "ORCH_PUBLIC_SCHEME", "AGENT_REGISTRY_URL", "PERSISTENCE_MODE", "LLM_CONFIG_FILE"}),
}
# Process-level variables that change how the interpreter loads code, where traffic goes or what TLS trusts.
FORBIDDEN_PREFIXES = ("PYTHON", "LD_", "DYLD_", "SSL_", "REQUESTS_", "CURL_", "HTTP_", "HTTPS_", "ALL_PROXY", "NO_PROXY",
                      "KUBERNETES_", "HELM_", "KUBECONFIG")
# Any name that talks about authentication, secrets, trust, transport or storage is operator territory.
SECURITY_WORDS = ("AUTH", "TOKEN", "PASSWORD", "PASSWD", "SECRET", "CRED", "IDENTITY", "SIGN", "KEY", "CERT", "TLS", "SSL",
                  "HTTPS", "VERIFY", "OWNER", "TRUST", "URL", "URI", "HOST", "ENDPOINT", "PROXY", "ALLOWLIST", "PERSISTENCE",
                  "DB_", "DATABASE", "CIPHER", "ADMIN", "ISOLATION")
DEFAULT_IMAGE_PREFIXES = ("ghcr.io/project-openan/",)
# The TMF639 specification URL is what the Canvas matches dependencies on: a CR may only declare a profile
# published under these prefixes (ALLOWED_SPEC_URL_PREFIXES overrides them).
DEFAULT_SPEC_URL_PREFIXES = ("https://raw.githubusercontent.com/project-openan/registry-center/",)
TMF639_PROFILE_FILE = "/tmf639-a2a-agent-v5.yaml"


class ValuesError(ValueError):
    """The CR carries values the operator refuses to render."""


ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
IMAGE_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
PULL_POLICIES = ("Always", "IfNotPresent", "Never")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _integer(comp: str, key: str, value: Any, lo: int, hi: int) -> None:
    # bool is an int subclass: reject it explicitly. Strings never pass, whatever they contain.
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValuesError(f"{comp}: {key} must be an integer between {lo} and {hi}")


def _validate_tmfc012(values: Dict[str, Any], spec_prefixes, callback_hosts, callback_ca_configmaps=()) -> None:
    """tmf639.*, catalog.* and broadcast.* decide which APIs the Canvas binds and where the Registry posts events."""
    tmf639 = values.get("tmf639") or {}
    url = tmf639.get("specificationUrl") if isinstance(tmf639, dict) else None
    if url not in (None, "") and not (isinstance(url, str) and url.startswith(tuple(spec_prefixes))
                                and url.endswith(TMF639_PROFILE_FILE)):
        raise ValuesError(f"registryCenter: tmf639.specificationUrl must be a {TMF639_PROFILE_FILE.lstrip('/')} profile "
                          f"under {list(spec_prefixes)}")
    if "specificationUrl" in (values.get("catalog") or {}):
        raise ValuesError("registryCenter: catalog.specificationUrl is controlled by the chart (canonical TMF634 v5)")
    broadcast = values.get("broadcast") or {}
    if not isinstance(broadcast, dict):
        raise ValuesError("registryCenter: broadcast must be a map")
    if broadcast.get("allowHttpCallbacks") not in (None, False):
        raise ValuesError("registryCenter: broadcast.allowHttpCallbacks is controlled by the operator")
    hosts = broadcast.get("callbackAllowlist")
    if hosts is not None:
        requested = {h.strip().lower() for h in str(hosts).split(",") if h.strip()}
        outside = sorted(requested - {h.lower() for h in callback_hosts})
        if outside:
            raise ValuesError(f"registryCenter: broadcast.callbackAllowlist hosts {outside} are not in the operator's "
                              "ALLOWED_CALLBACK_HOSTS")
    # An extra CA widens whom callback TLS trusts, like allowHttpCallbacks: only operator-approved ConfigMaps.
    ca = broadcast.get("callbackCaConfigMap")
    if ca not in (None, "") and ca not in callback_ca_configmaps:
        raise ValuesError(f"registryCenter: broadcast.callbackCaConfigMap {ca!r} is not in the operator's "
                          "ALLOWED_CALLBACK_CA_CONFIGMAPS")


def validate_values(comp: str, values: Dict[str, Any], allowed_prefixes=DEFAULT_IMAGE_PREFIXES, *,
                    allowed_spec_prefixes=DEFAULT_SPEC_URL_PREFIXES, allowed_callback_hosts=(),
                    allowed_callback_ca_configmaps=()) -> None:
    repo = (values.get("image") or {}).get("repository")
    if repo and not any(repo.startswith(p) for p in allowed_prefixes):
        raise ValuesError(f"{comp}: image.repository {repo!r} is outside the allowed registries {list(allowed_prefixes)}")
    image = values.get("image") or {}
    if not isinstance(image, dict):
        raise ValuesError(f"{comp}: image must be a map")
    tag = image.get("tag")
    if tag is not None and not (isinstance(tag, str) and IMAGE_TAG.match(tag)):
        raise ValuesError(f"{comp}: image.tag {tag!r} is not a valid image tag ({IMAGE_TAG.pattern})")
    if image.get("pullPolicy") is not None and image["pullPolicy"] not in PULL_POLICIES:
        raise ValuesError(f"{comp}: image.pullPolicy must be one of {list(PULL_POLICIES)}")
    if "replicas" in values:
        _integer(comp, "replicas", values["replicas"], 0, 100)
    for key in ("port", "specPort"):
        if key in values:
            _integer(comp, key, values[key], 1, 65535)
    component = values.get("component") or {}
    if isinstance(component, dict):
        for key in ("id", "name", "functionalBlock", "version"):
            if key in component and not (isinstance(component[key], str) and SAFE_NAME.match(component[key])):
                raise ValuesError(f"{comp}: component.{key} {component[key]!r} contains characters that are not allowed")
    if comp == "registryCenter":
        set_by_cr = sorted(k for k in ("authMode", "identityMode", "signatureValidation") if k in (values.get("registry") or {}))
        if set_by_cr:
            raise ValuesError(f"registryCenter: registry.{set_by_cr} are controlled by the operator (use spec.cardSync.signing)")
        _validate_tmfc012(values, allowed_spec_prefixes, allowed_callback_hosts, allowed_callback_ca_configmaps)
    if comp in EXTRA_ENV:
        section, key = EXTRA_ENV[comp]
        env = (values.get(section) or {}).get(key) or {}
        if not isinstance(env, dict):
            raise ValuesError(f"{comp}: {section}.{key} must be a map")
        # Exact, upper-case names only: no other spelling can alias a protected variable or reach the template.
        malformed = sorted(str(k) for k in env if not isinstance(k, str) or not ENV_KEY.match(k))
        if malformed:
            raise ValuesError(f"{comp}: {section}.{key} has invalid variable names {malformed} (use [A-Z_][A-Z0-9_]*)")
        bad = sorted(k for k in env if k in FORBIDDEN_ENV or k in GENERATED_ENV.get(comp, ())
                     or k.startswith(FORBIDDEN_PREFIXES) or any(w in k for w in SECURITY_WORDS))
        if bad:
            raise ValuesError(f"{comp}: {section}.{key} may not set {bad} (generated, security-relevant or process-level)")
        nonscalar = sorted(k for k, v in env.items() if not isinstance(v, (str, int, float, bool)))
        if nonscalar:
            raise ValuesError(f"{comp}: {section}.{key} values must be strings, numbers or booleans: {nonscalar}")
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
    if comp == "registryCenter":
        # Authentication and signature policy come from the operator, never from the CR.
        reg = dict(out.get("registry") or {})
        for key in ("authMode", "identityMode"):
            reg.pop(key, None)  # chart defaults: static_bearer / token
        reg["signatureValidation"] = gen["registry"]["signatureValidation"]
        out["registry"] = reg
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
