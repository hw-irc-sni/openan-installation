# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure logic: values for the three component charts and OpenAN CR status conditions."""
from datetime import datetime, timezone
from typing import Any, Dict, List

COMPONENTS = ("registryCenter", "orchestrationCenter", "frontend")
CHART_DIR = {"registryCenter": "openan-registry-center", "orchestrationCenter": "openan-orchestration-center",
             "frontend": "openan-frontend"}
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
    return _merge(gen, spec.get(comp, {}).get("values", {}))


def enabled_components(spec: Dict[str, Any]) -> List[str]:
    return [c for c in COMPONENTS if spec.get(c, {}).get("enabled", True)]


def condition(ctype: str, status: bool, reason: str, message: str = "") -> Dict[str, str]:
    return {"type": ctype, "status": "True" if status else "False", "reason": reason, "message": message,
            "lastTransitionTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def is_ready(component_cr: Dict[str, Any] | None) -> bool:
    """ODA Component is ready when the Canvas component operator reports deployment_status == Complete."""
    st = (component_cr or {}).get("status", {})
    return (st.get("summary/status", {}) or {}).get("deployment_status") == "Complete"
