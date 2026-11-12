# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pure logic: the Kubernetes objects of an Agent ODA Component made from a registry agent card.

Two shapes, both a Component with one a2a ExposedAPI plus a Deployment and a Service labelled with
oda.tmforum.org/componentName (the Canvas adopts them and marks the API ready from the Service endpoints):

- deploy: the card carries a signed deployment extension naming an allowed image; the Deployment runs it.
- mirror: anything else; the Deployment is an nginx reverse proxy to the agent's own (remote) URL. An ExternalName
  Service would be simpler, but the Canvas only marks an API ready from EndpointSlices, which those never have.
"""
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

DEPLOY_EXT = "https://openan.dev/extensions/deployment"
COMPONENT_LABEL = "oda.tmforum.org/componentName"
MODE_ANNOTATION = "openan.dev/mode"  # "deploy" | "mirror"
API_NAME = "a2aagent"
PROXY_PORT = 8080
PUBLICATION_DATE = "2026-01-01T00:00:00.000Z"  # constant: the manifests must hash the same on every pass

_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,100}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_INTERNAL_SUFFIXES = (".svc", ".cluster.local", ".local", ".internal", ".localdomain")
_DEPLOY_KEYS = {"v", "image", "port", "path", "health", "replicas"}


class DeploymentError(ValueError):
    """The deployment extension or the agent URL is not acceptable."""


@dataclass(frozen=True)
class Policy:
    namespace: str
    image_prefixes: Tuple[str, ...]
    proxy_image: str
    upstream_suffixes: Tuple[str, ...] = ()  # empty: any public host
    # The CR's own registry when its cards are signature-checked, or a connected registry the operator's
    # configuration (AGENT_DEPLOY_REGISTRIES) trusts: only then may a card make the cluster run an image.
    deploy_allowed: bool = False


@dataclass(frozen=True)
class Upstream:
    origin: str     # scheme://host[:port], what nginx connects to (no path: see nginx_conf)
    host: str       # for SNI
    authority: str  # host[:port], the Host header
    path: str       # path of the agent's interface URL, prepended to the forwarded request


def _integer(params: Dict[str, Any], key: str, lo: int, hi: int, default: Optional[int] = None) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise DeploymentError(f"deployment.{key} must be an integer between {lo} and {hi}")
    return value


def _path(params: Dict[str, Any], key: str, default: str) -> str:
    value = params.get(key, default)
    if not isinstance(value, str) or not _PATH.match(value) or ".." in value:
        raise DeploymentError(f"deployment.{key} must be an absolute path matching {_PATH.pattern}")
    return value


def parse_deployment(card: Dict[str, Any], image_prefixes: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """The deployment extension of a card, validated; None when the card has none."""
    ext = next((e for e in ((card.get("capabilities") or {}).get("extensions") or []) if e.get("uri") == DEPLOY_EXT), None)
    if ext is None:
        return None
    params = ext.get("params")
    if not isinstance(params, dict):
        raise DeploymentError("deployment extension has no params")
    unknown = sorted(set(params) - _DEPLOY_KEYS)
    if unknown:
        raise DeploymentError(f"deployment extension has unsupported keys {unknown} (no env, command or volumes)")
    if params.get("v") != 1:
        raise DeploymentError("deployment extension must have v: 1")
    image = params.get("image")
    if not isinstance(image, str) or not any(image.startswith(p) for p in image_prefixes):
        raise DeploymentError(f"deployment.image {image!r} is outside the allowed registries {list(image_prefixes)}")
    last = image.rsplit("/", 1)[-1]
    if "@sha256:" not in image and (":" not in last or last.endswith(":latest")):
        raise DeploymentError("deployment.image needs a pinned tag or digest (not :latest)")
    return {"image": image, "port": _integer(params, "port", 1, 65535), "path": _path(params, "path", "/"),
            "health": _path(params, "health", "/healthz"), "replicas": _integer(params, "replicas", 0, 3, 1)}


def primary_url(card: Dict[str, Any]) -> str:
    interfaces = card.get("supportedInterfaces") or []
    url = interfaces[0].get("url") if interfaces and isinstance(interfaces[0], dict) else None
    if not isinstance(url, str) or not url:
        raise DeploymentError("card has no supportedInterfaces url")
    return url


def _suffix_match(host: str, suffix: str) -> bool:
    bare = suffix.lstrip(".")
    return host == bare or host.endswith("." + bare)


def validate_upstream(url: str, suffixes: Tuple[str, ...] = ()) -> Upstream:
    """A mirror proxies to a URL taken from the card: refuse anything that would reach into the cluster or the node.
    With `suffixes` (AGENT_UPSTREAM_ALLOWED_HOSTS) only those hosts are allowed, internal ones included. The check is
    on the name: a public name resolving to an internal address needs an egress NetworkPolicy to be stopped."""
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for out-of-range or non-numeric ports
    except ValueError as e:
        raise DeploymentError(f"agent url {url!r} is malformed: {e}")
    if parts.scheme not in ("http", "https"):
        raise DeploymentError(f"agent url {url!r} must be http(s)")
    if parts.username or parts.password:
        raise DeploymentError("agent url must not carry credentials")
    host = (parts.hostname or "").lower()
    # A trailing dot is the same name to a resolver but defeats suffix checks (x.svc.cluster.local.).
    if not host or not _HOST.match(host) or host.endswith(".") or ".." in host:
        raise DeploymentError(f"agent url {url!r} has an invalid host")
    if suffixes:
        if not any(_suffix_match(host, s) for s in suffixes):
            raise DeploymentError(f"agent url host {host} is not in the allowed upstream hosts {list(suffixes)}")
    else:
        try:
            internal = not ipaddress.ip_address(host).is_global
        except ValueError:
            internal = host == "localhost" or "." not in host or host.endswith(_INTERNAL_SUFFIXES)
        if internal:
            raise DeploymentError(f"agent url host {host} is cluster-internal or not public")
    path = parts.path.rstrip("/")
    if path and not _PATH.match(path):
        raise DeploymentError(f"agent url path {path!r} is not supported")
    authority = f"{host}:{port}" if port else host
    return Upstream(origin=f"{parts.scheme}://{authority}", host=host, authority=authority, path=path)


def nginx_conf(name: str, upstream: Upstream) -> str:
    """nginx template (rendered by the image entrypoint): resolve the upstream per request so the proxy starts and
    stays ready while the remote agent is down, pass only scheme://host:port to proxy_pass (a URI in the variable
    would replace every request path) and map the gateway prefix /<name>/a2a onto the agent's own path."""
    return f"""server {{
  listen {PROXY_PORT};
  location = /healthz {{ access_log off; default_type text/plain; return 200 "ok"; }}
  location = /.well-known/agent-card.json {{ default_type application/json; alias /etc/nginx/card/agent-card.json; }}
  location ~ ^/{name}/a2a(/.*)?$ {{
    resolver ${{NGINX_LOCAL_RESOLVERS}} valid=30s;
    set $agent_upstream {upstream.origin};
    rewrite ^/{name}/a2a$ {upstream.path or "/"} break;
    rewrite ^/{name}/a2a(/.*)$ {upstream.path}$1 break;
    proxy_pass $agent_upstream;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host {upstream.authority};
    proxy_ssl_server_name on;
    proxy_ssl_name {upstream.host};
    proxy_buffering off;
    proxy_read_timeout 3600s;
  }}
}}
"""


_SECURITY = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
             "seccompProfile": {"type": "RuntimeDefault"}}
_RESOURCES = {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}


def _component(name: str, card: Dict[str, Any], policy: Policy, labels: Dict[str, str], annotations: Dict[str, str],
               mode: str, port: int) -> Dict[str, Any]:
    version = card.get("version") if isinstance(card.get("version"), str) and _VERSION.match(card["version"]) else "1.0.0"
    return {
        "apiVersion": "oda.tmforum.org/v1", "kind": "Component",
        "metadata": {"name": name, "labels": labels, "annotations": {**annotations, MODE_ANNOTATION: mode}},
        "spec": {
            "componentMetadata": {
                "id": "AGENT-" + name.rsplit("-", 1)[-1].upper(), "name": name, "functionalBlock": "AIAgent",
                "publicationDate": PUBLICATION_DATE, "status": "specified", "version": version,
                "description": str(card.get("description") or card.get("name") or "")[:500],
                "maintainers": [{"name": "OpenAN operator", "email": "openan@example.invalid"}],
                "owners": [{"name": "OpenAN operator", "email": "openan@example.invalid"}]},
            "coreFunction": {
                "exposedAPIs": [{
                    "name": API_NAME, "apiType": "a2a",
                    "specification": [{"url": f"http://{name}.{policy.namespace}.svc.cluster.local:{port}"
                                              "/.well-known/agent-card.json"}],
                    "implementation": name, "path": f"/{name}/a2a", "port": port}],
                "dependentAPIs": []},
            "eventNotification": {"publishedEvents": [], "subscribedEvents": []},
            "managementFunction": {"exposedAPIs": []},
            "securityFunction": {"canvasSystemRole": "canvasRole", "exposedAPIs": []},
        },
    }


def _workload(name: str, labels: Dict[str, str], container: Dict[str, Any], replicas: int, port: int,
              volumes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deployment + Service: the Service is the ExposedAPI implementation."""
    container = {**container, "imagePullPolicy": "IfNotPresent", "ports": [{"containerPort": port}],
                 "securityContext": _SECURITY, "resources": _RESOURCES}
    deployment = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "labels": labels},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                 "template": {"metadata": {"labels": {"app": name, COMPONENT_LABEL: name}},
                              "spec": {"automountServiceAccountToken": False, "containers": [container],
                                       "volumes": volumes}}}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "labels": labels},
               "spec": {"selector": {"app": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]}}
    return [deployment, service]


def _deploy_objects(name, card, policy, labels, annotations, child_labels, deploy) -> List[Dict[str, Any]]:
    port, path = deploy["port"], deploy["path"].rstrip("/")
    container = {
        "name": "agent", "image": deploy["image"],
        # OpenAN agents serve the RPC path under the gateway prefix too (the Canvas forwards /<name>/... as is).
        "env": [{"name": "A2A_RPC_PATHS", "value": f"{path or '/'},/{name}/a2a"},
                {"name": "OPENAN_GATEWAY_PATH", "value": f"/{name}/a2a"}],
        "readinessProbe": {"httpGet": {"path": deploy["health"], "port": port}, "periodSeconds": 10}}
    return [_component(name, card, policy, labels, annotations, "deploy", port),
            *_workload(name, child_labels, container, deploy["replicas"], port, [])]


def _mirror_objects(name, card, policy, labels, annotations, child_labels) -> List[Dict[str, Any]]:
    upstream = validate_upstream(primary_url(card), policy.upstream_suffixes)
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "labels": child_labels},
              "data": {"default.conf.template": nginx_conf(name, upstream),
                       "agent-card.json": json.dumps(card, sort_keys=True)}}
    container = {
        "name": "proxy", "image": policy.proxy_image,
        "env": [{"name": "NGINX_ENTRYPOINT_LOCAL_RESOLVERS", "value": "1"}],
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": PROXY_PORT}, "periodSeconds": 10},
        "volumeMounts": [
            {"name": "conf", "mountPath": "/etc/nginx/templates/default.conf.template", "subPath": "default.conf.template"},
            {"name": "conf", "mountPath": "/etc/nginx/card/agent-card.json", "subPath": "agent-card.json"}]}
    volumes = [{"name": "conf", "configMap": {"name": name}}]
    return [_component(name, card, policy, labels, annotations, "mirror", PROXY_PORT), config,
            *_workload(name, child_labels, container, 1, PROXY_PORT, volumes)]


def build(name: str, card: Dict[str, Any], policy: Policy, labels: Dict[str, str],
          annotations: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """([Component, ConfigMap (mirror only), Deployment, Service], warnings). Raises DeploymentError when the agent
    can be neither deployed nor mirrored (no usable URL)."""
    warnings: List[str] = []
    child_labels = {COMPONENT_LABEL: name, **{k: v for k, v in labels.items() if k != COMPONENT_LABEL}}
    deploy = None
    try:
        deploy = parse_deployment(card, policy.image_prefixes)
        if deploy is not None and not policy.deploy_allowed:
            warnings.append("deployment extension ignored: this registry is not trusted to deploy images")
            deploy = None
    except DeploymentError as e:
        warnings.append(f"deployment extension ignored: {e}")
    if deploy is not None:
        return _deploy_objects(name, card, policy, labels, annotations, child_labels, deploy), warnings
    return _mirror_objects(name, card, policy, labels, annotations, child_labels), warnings


def spec_hash(objs: List[Dict[str, Any]]) -> str:
    """Hash of the rendered objects, so template changes roll out as well as card changes."""
    return hashlib.sha256(json.dumps(objs, sort_keys=True).encode()).hexdigest()[:32]
