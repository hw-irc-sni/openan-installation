# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""ExposedAPI(a2a) -> signed agent card published in the Registry Center."""
import base64
from typing import Any, Callable, Dict, List, Optional

import httpx

from .card_mapper import CardError, to_registry_card
from .card_signer import sign_card

ANNOTATION = "openan.dev/registered-card"
WELL_KNOWN = "/.well-known/agent-card.json"


def candidate_urls(api_spec: Dict[str, Any], namespace: str) -> List[str]:
    """In-cluster Service URLs to try (the gateway URL in status is for outside traffic)."""
    host = f"http://{api_spec['implementation']}.{namespace}.svc.cluster.local:{api_spec['port']}"
    path = (api_spec.get("path") or "").rstrip("/")
    urls = [host + WELL_KNOWN]
    if path:
        urls.append(host + path + WELL_KNOWN)
    return urls


def fetch_card(urls: List[str], client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    c = client or httpx.Client(timeout=10.0, follow_redirects=False)
    last = ""
    for u in urls:
        try:
            r = c.get(u)
            if r.status_code == 200:
                return r.json()
            last = f"{u} -> {r.status_code}"
        except (httpx.HTTPError, ValueError) as e:
            last = f"{u} -> {type(e).__name__}"
    raise CardError(f"agent card not reachable: {last}")


OWNER_EXT = "https://openan.dev/extensions/source-exposedapi"


class OwnershipError(CardError):
    """The card is not acceptable for this ExposedAPI (foreign owner or foreign endpoint)."""


def owner_ref(namespace: str, name: str, uid: str) -> str:
    return f"{namespace}/{name}/{uid}"


def card_owner(card: Optional[Dict[str, Any]]) -> Optional[str]:
    for ext in ((card or {}).get("capabilities") or {}).get("extensions") or []:
        if ext.get("uri") == OWNER_EXT:
            return (ext.get("params") or {}).get("owner")
    return None


def allowed_hosts(api_spec: Dict[str, Any], namespace: str, extra_urls: List[str]) -> set:
    from urllib.parse import urlparse
    impl = api_spec["implementation"]
    hosts = {impl, f"{impl}.{namespace}", f"{impl}.{namespace}.svc", f"{impl}.{namespace}.svc.cluster.local"}
    hosts |= {urlparse(u).hostname for u in extra_urls if u}
    return {h for h in hosts if h}


def check_interfaces(card: Dict[str, Any], hosts: set) -> None:
    """Every advertised endpoint must point at this ExposedAPI's own Service or gateway address."""
    from urllib.parse import urlparse
    for i in card.get("supportedInterfaces", []):
        host = urlparse(i["url"]).hostname
        if host not in hosts:
            raise OwnershipError(f"interface {i['url']} is not served by this ExposedAPI (allowed: {sorted(hosts)})")


def with_owner(card: Dict[str, Any], owner: str) -> Dict[str, Any]:
    caps = dict(card.get("capabilities") or {})
    exts = [e for e in caps.get("extensions") or [] if e.get("uri") != OWNER_EXT]
    exts.append({"uri": OWNER_EXT, "description": "ExposedAPI that published this card", "params": {"owner": owner}})
    return {**card, "capabilities": {**caps, "extensions": exts}}


def build_signed_card(raw: Dict[str, Any], organization: str, signing: Optional[Dict[str, str]],
                      owner: str = "", hosts: Optional[set] = None,
                      rewrite: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """`rewrite` (the agentgateway route, AGENTGATEWAY_PUBLISH_URL) runs after the endpoint check and before the
    owner extension and the signature, so the signed card is the one published."""
    # Local-trust-store keys are looked up by file name <agent>.json: names must be file-name safe.
    card = to_registry_card(raw, organization, allow_spaces=not (signing and signing.get("trust_store")))
    if hosts is not None:
        check_interfaces(card, hosts)
    if rewrite is not None:
        card = rewrite(card)
    if owner:
        card = with_owner(card, owner)
    if signing:
        card = sign_card(card, signing["private"], signing["kid"], signing.get("jku", ""))
    return card


def is_a2a(spec: Dict[str, Any]) -> bool:
    return spec.get("apiType") == "a2a"


def decode_secret(data: Dict[str, str], key: str) -> str:
    return base64.b64decode(data[key]).decode()


def trust_document(public_jwk: Dict[str, str]) -> str:
    """Registry local key file (etc/sign_verify/jwks/<org>/<agent>.json)."""
    import json
    return json.dumps({"keys": [public_jwk]})
