# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Side effects of the agentgateway route sync (pure logic: gateway_routes.py).

Write: the `config.yaml` key of the gateway's ConfigMap, patched with the resourceVersion that was read, so a
concurrent change (Canvas Portal, Helm, another namespace's sync) makes the write fail with 409 and the next pass
starts again from the new file. Read: port 8081 of the gateway (`/api/runtime` for the reload status,
`/config_dump` for the running routes) with the key of its read-only Secret.

The kubelet brings a ConfigMap change into the pod within about a minute; the gateway then reloads it. A file the
gateway rejects keeps the old config running but would stop the gateway from starting again, so a write that the
gateway reports as rejected is put back (only while the ConfigMap is still the version we wrote) and those routes
are not written again until they change. While a write waits for that confirmation no further write is made.
"""
import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import httpx
import yaml
from kubernetes import client

from . import gateway_routes as gr

ODA = "oda.tmforum.org"
CONFIG_KEY = "config.yaml"
CONFIRM_SECONDS = 180.0


class GatewayUnavailable(RuntimeError):
    pass


class _NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


@dataclass
class Pending:
    version: str               # resourceVersion of the ConfigMap we wrote
    previous: str              # the file before our write
    names: List[str]           # owned route names in what we wrote
    error_before: Optional[str]
    since: float
    digest: str


@dataclass
class State:
    pending: Optional[Pending] = None
    rejected: Optional[str] = None   # digest of owned routes the gateway rejected
    down: bool = False
    reported: Set[str] = field(default_factory=set)  # warnings of the last pass (each is logged once)

    def new_warnings(self, warnings: List[str]) -> List[str]:
        fresh = [w for w in warnings if w not in self.reported]
        self.reported = set(warnings)
        return fresh


STATE: Dict[str, State] = {}  # namespace -> state


def state_for(namespace: str) -> State:
    return STATE.setdefault(namespace, State())


@dataclass
class Result:
    changed: bool = False
    confirmed: bool = False
    reverted: bool = False
    routes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class GatewayStatus:
    last_error: Optional[str]
    dump: Any


def read_gateway(core, target: gr.GatewayTarget, get: Callable[..., Any] = httpx.get) -> Optional[GatewayStatus]:
    """Reload status and running config from port 8081; None when the gateway or its key cannot be read."""
    try:
        secret = core.read_namespaced_secret(target.key_secret, target.namespace)
        key = base64.b64decode((secret.data or {}).get("key", "")).decode().strip()
    except (client.ApiException, ValueError):
        return None
    if not key:
        return None
    headers = {"Authorization": f"Bearer {key}"}
    try:
        runtime = get(f"{target.config_url}/api/runtime", headers=headers, timeout=5.0)
        runtime.raise_for_status()
        dump = get(f"{target.config_url}/config_dump", headers=headers, timeout=5.0)
        dump.raise_for_status()
        reload = (runtime.json() or {}).get("configReload") or {}
        error = reload.get("lastError") if isinstance(reload, dict) else None
        return GatewayStatus(str(error) if error else None, dump.json())
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def _digest(routes: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(routes, sort_keys=True).encode()).hexdigest()[:16]


def _write(core, target: gr.GatewayTarget, text: str, version: str) -> str:
    body = {"metadata": {"resourceVersion": version}, "data": {CONFIG_KEY: text}}
    return core.patch_namespaced_config_map(target.config_map, target.namespace, body).metadata.resource_version


def _check_pending(core, target: gr.GatewayTarget, namespace: str, st: State, version: str, res: Result,
                   now: float, get: Callable[..., Any]) -> bool:
    """True while our last write still waits for the gateway (no new write in the meantime)."""
    p = st.pending
    if version != p.version:  # someone wrote after us (Helm, the portal): start again from their file
        st.pending = None
        return False
    gw = read_gateway(core, target, get)
    if gw is not None and gw.last_error and gw.last_error != p.error_before:
        try:
            _write(core, target, p.previous, version)
        except client.ApiException as e:
            res.warnings.append(f"agentgateway rejected the routes ({gw.last_error}); putting the previous config "
                                f"back failed: HTTP {e.status}")
            return True
        st.pending, st.rejected, res.reverted = None, p.digest, True
        res.warnings.append(f"agentgateway rejected the routes, previous config put back: {gw.last_error}")
        return True
    if gw is not None and not gw.last_error and gr.applied(gw.dump, p.names, namespace):
        st.pending, res.confirmed = None, True
        return False
    if now - p.since > CONFIRM_SECONDS:
        st.pending = None
        res.warnings.append("agentgateway has not confirmed the route change "
                            f"within {int(CONFIRM_SECONDS)} s (port 8081 unreachable or the pod not updated)")
        return False
    return True


def sync_routes(core, custom, namespace: str, target: gr.GatewayTarget, now: Optional[float] = None,
                get: Callable[..., Any] = httpx.get) -> Result:
    """One pass for `namespace`. Raises GatewayUnavailable when the ConfigMap is missing or not a config file."""
    now = time.monotonic() if now is None else now
    st, res = state_for(namespace), Result()
    exposed = custom.list_namespaced_custom_object(ODA, "v1", namespace, "exposedapis")["items"]
    components = {c["metadata"]["name"]: c
                  for c in custom.list_namespaced_custom_object(ODA, "v1", namespace, "components")["items"]}
    wanted, warnings = gr.desired_routes(target, namespace, exposed, components)
    res.warnings += warnings
    try:
        cm = core.read_namespaced_config_map(target.config_map, target.namespace)
    except client.ApiException as e:
        if e.status in (403, 404):
            raise GatewayUnavailable(f"ConfigMap {target.namespace}/{target.config_map}: HTTP {e.status}")
        raise
    text, version = (cm.data or {}).get(CONFIG_KEY, ""), cm.metadata.resource_version
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None
    if not isinstance(doc, dict):
        raise GatewayUnavailable(f"{target.namespace}/{target.config_map} has no readable {CONFIG_KEY}")
    if st.pending is not None and _check_pending(core, target, namespace, st, version, res, now, get):
        return res
    routes, clashes = gr.merge(doc.get("routes"), wanted, namespace)
    res.warnings += clashes
    mine = [r for r in routes if gr.owned(r, namespace)]
    res.routes = gr.owned_names(mine)
    digest = _digest(mine)
    if routes == (doc.get("routes") or []):
        st.rejected = None
        return res
    if digest == st.rejected:
        return res  # reported when it was rejected; written again once the wanted routes change
    if gr.unsafe(mine):
        res.warnings.append("gateway routes not written: '$' in a route (the gateway expands variables)")
        return res
    before = read_gateway(core, target, get)
    doc["routes"] = routes
    new_text = yaml.dump(doc, Dumper=_NoAliases, sort_keys=False, allow_unicode=True, default_flow_style=False)
    try:
        written = _write(core, target, new_text, version)
    except client.ApiException as e:
        if e.status == 409:
            res.warnings.append("agentgateway config changed at the same time; retried on the next pass")
            return res
        raise
    st.pending = Pending(written, text, res.routes, before.last_error if before else None, now, digest)
    st.rejected, res.changed = None, True
    return res
