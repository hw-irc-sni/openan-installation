# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure logic of the agentgateway route sync: one A2A route per a2a ExposedAPI of a namespace.

agentgateway (standalone) runs from one config file in a ConfigMap and reloads it when it changes; its own HTTP
ports only read (8081: /config_dump, /api/runtime). So the routes are written to the file's top-level `routes` list,
next to what Helm and the Canvas Portal put there. Ownership is by name: this operator only ever adds, changes or
removes routes named `openan.<namespace>.<ExposedAPI>` of the namespace it syncs (namespaces have no dots, so the
prefix of one namespace never matches another's). Every other route is kept as it is, in its order.

Route: `<prefix>/<namespace>/<ExposedAPI>` -> http://<implementation>.<namespace>.svc.cluster.local:<port>, the path
prefix rewritten to `/` and the `a2a` policy, which makes the gateway rewrite the agent card's interface URLs to
itself. Both kinds of agents serve their card at /.well-known/agent-card.json with an interface URL on their own
Service (a mirror serves a card pointing at its /<name>/a2a), so one route shape covers Helm-installed agents,
deployed registry agents and mirrors of external ones.
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

from .agent_sync import parent_component
from .cards import is_a2a

ROUTE_NAME_PREFIX = "openan."
REQUEST_TIMEOUT = "120s"

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_PREFIX = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")


@dataclass(frozen=True)
class GatewayTarget:
    """Where and how the routes go (operator configuration, never from a CR or an annotation)."""
    namespace: str             # namespace of the agentgateway ConfigMap
    config_map: str            # agentgateway-config
    key_secret: str            # Secret with the read key of port 8081 (data.key)
    config_url: str            # http://agentgateway.<namespace>.svc.cluster.local:8081
    gateway: str               # `gateways:` value of the routes (the chart's listener is "default")
    path_prefix: str           # /agents


def valid_prefix(value: str) -> bool:
    return bool(_PREFIX.match(value)) and ".." not in value


def owner_prefix(namespace: str) -> str:
    return f"{ROUTE_NAME_PREFIX}{namespace}."


def route_name(namespace: str, api: str) -> str:
    return owner_prefix(namespace) + api


def owned(route: Any, namespace: str) -> bool:
    return isinstance(route, dict) and str(route.get("name") or "").startswith(owner_prefix(namespace))


def route_path(target: GatewayTarget, namespace: str, api: str) -> str:
    return f"{target.path_prefix}/{namespace}/{api}"


def _path_prefixes(route: Dict[str, Any]) -> List[str]:
    out = []
    for m in route.get("matches") or []:
        path = (m or {}).get("path") if isinstance(m, dict) else None
        if isinstance(path, dict) and isinstance(path.get("pathPrefix"), str):
            out.append(path["pathPrefix"].rstrip("/") or "/")
    return out


def unroutable(name: str, spec: Dict[str, Any]) -> Optional[str]:
    """Why an a2a ExposedAPI gets no route (None: it gets one). An ExposedAPI with a gatewayConfiguration (API key
    verification, rate limits, ...) gets none: the route would not carry those Canvas gateway policies, so it fails
    closed instead of offering an unprotected way in."""
    if spec.get("gatewayConfiguration"):
        return "has a gatewayConfiguration the agentgateway route would not enforce"
    impl, port = spec.get("implementation"), spec.get("port")
    if not _DNS_LABEL.match(name or "") or not isinstance(impl, str) or not _DNS_LABEL.match(impl):
        return "name or implementation is not a DNS label"
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return f"port {port!r} is not a port number"
    return None


def valid_publish_url(value: str) -> bool:
    """AGENTGATEWAY_PUBLISH_URL: http(s)://host[:port][/path], nothing else (no query, fragment, credentials, $)."""
    parts = urlsplit(value)
    try:
        parts.port
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username
            and not parts.query and not parts.fragment and "$" not in value
            and (not parts.path or valid_prefix(parts.path.rstrip("/")) or parts.path == "/"))


def via_gateway(card: Dict[str, Any], service_hosts: Iterable[str], publish_url: str, target: GatewayTarget,
                namespace: str, api: str) -> Dict[str, Any]:
    """The card as published when AGENTGATEWAY_PUBLISH_URL is set: every interface served by the agent's own
    Service points at its gateway route instead, path kept (the route rewrites its prefix to `/`). Interfaces on
    other hosts (the Canvas gateway URL) are left as they are."""
    hosts = set(service_hosts)
    base = publish_url.rstrip("/") + route_path(target, namespace, api)
    out = []
    for i in card.get("supportedInterfaces") or []:
        parts = urlsplit(i["url"])
        if parts.hostname in hosts:
            i = {**i, "url": base + (parts.path if parts.path != "/" else "") + (f"?{parts.query}" if parts.query else "")}
        out.append(i)
    return {**card, "supportedInterfaces": out}


def desired_routes(target: GatewayTarget, namespace: str, exposedapis: Iterable[Dict[str, Any]],
                   components: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(routes, warnings). One route per a2a ExposedAPI of a Component that exists and is not being deleted. The
    backend is the ExposedAPI's own implementation Service in the same namespace, the address the Canvas itself
    routes to; a value that is not a DNS label or a port is reported and skipped."""
    routes, warnings = [], []
    for api in exposedapis:
        meta, spec = api.get("metadata") or {}, api.get("spec") or {}
        name = meta.get("name", "")
        if not is_a2a(spec) or meta.get("deletionTimestamp"):
            continue
        parent = parent_component(meta)
        comp = components.get(parent or "")
        if comp is None or (comp.get("metadata") or {}).get("deletionTimestamp"):
            continue
        problem = unroutable(name, spec)
        if problem:
            warnings.append(f"ExposedAPI {name!r}: {problem}, no gateway route")
            continue
        impl, port = spec["implementation"], spec["port"]
        routes.append({
            "name": route_name(namespace, name),
            "gateways": target.gateway,
            "matches": [{"path": {"pathPrefix": route_path(target, namespace, name)}}],
            "policies": {"a2a": {}, "urlRewrite": {"path": {"prefix": "/"}},
                         "timeout": {"requestTimeout": REQUEST_TIMEOUT}},
            "backends": [{"host": f"{impl}.{namespace}.svc.cluster.local:{port}"}],
        })
    return sorted(routes, key=lambda r: r["name"]), warnings


def merge(current: Optional[List[Any]], wanted: List[Dict[str, Any]], namespace: str
          ) -> Tuple[List[Any], List[str]]:
    """(new `routes` list, warnings): every route this namespace does not own, unchanged and in order, then the
    wanted ones. A wanted route is left out when another route serves its path or a path below it (the gateway
    picks the longest prefix, so such a route would take the agent's traffic; it was there first and is not ours
    to change). Shorter foreign prefixes (/agents) lose to ours and are no clash."""
    current = current if isinstance(current, list) else []
    foreign = [r for r in current if not owned(r, namespace)]
    taken = {p for r in foreign if isinstance(r, dict) for p in _path_prefixes(r)}
    out, warnings = list(foreign), []
    for route in wanted:
        clash = [t for p in _path_prefixes(route) for t in sorted(taken) if t == p or t.startswith(p + "/")]
        if clash:
            warnings.append(f"gateway route {route['name']}: path {clash[0]} is already served by another route")
            continue
        out.append(route)
    return out, warnings


def unsafe(routes: List[Dict[str, Any]]) -> bool:
    """The gateway expands environment variables ($NAME) across the whole file: none in what we write."""
    return "$" in json.dumps(routes)


def owned_names(routes: Iterable[Dict[str, Any]]) -> List[str]:
    return sorted(r["name"] for r in routes)


def names_in_dump(dump: Any) -> List[str]:
    """Route names (and keys) found in the gateway's /config_dump: binds[].listeners{}.routes{}."""
    found: List[str] = []
    binds = dump.get("binds") if isinstance(dump, dict) else None
    for bind in binds if isinstance(binds, list) else []:
        listeners = bind.get("listeners") if isinstance(bind, dict) else None
        for listener in (listeners.values() if isinstance(listeners, dict) else listeners or []):
            routes = listener.get("routes") if isinstance(listener, dict) else None
            for key, route in (routes.items() if isinstance(routes, dict) else enumerate(routes or [])):
                if isinstance(route, dict):
                    found += [str(v) for v in (route.get("name"), route.get("key")) if v]
                found.append(str(key))
    return found


def applied(dump: Any, names: Iterable[str], namespace: str) -> bool:
    """The gateway runs exactly the owned route names we wrote (a name may carry a namespace prefix in the dump)."""
    seen = names_in_dump(dump)
    running = {n for n in seen if owner_prefix(namespace) in n}
    wanted = set(names)
    return all(any(s.endswith(n) for s in running) for n in wanted) and \
        all(any(s.endswith(n) for n in wanted) for s in running)
