# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Agent registry <-> Agent ODA Component sync: pure planning, manifests, registry sources and the cluster passes
against in-memory fakes of the Kubernetes API and of the Registry Center."""
import base64
import copy
import logging
import re
from types import SimpleNamespace

import httpx
import kopf
import pytest
from kubernetes import client

from src import agent_manifests as am
from src import agent_sync as sync
from src import agent_sync_io as sio
from src import cards, openan_operator as op, secrets_bootstrap, stack
from src.registry_client import RegistryClient
from src.registry_source import NativeSource, SourceUnavailable, Tmf639Source

LOG = logging.getLogger("test")
NS = "components"
PREFIXES = ("ghcr.io/project-openan/",)
SETTINGS = sio.Settings(PREFIXES, "nginxinc/nginx-unprivileged:1.27-alpine")
MANAGED = "openan-registry-center-registrycenter"
RC_URL = "http://openan-registry-center-registry-center.components.svc.cluster.local:5000"
CR = {"metadata": {"name": "openan", "uid": "cr-uid"},
      "spec": {"agentSync": {"enabled": True}, "cardSync": {"enabled": True, "signing": {"enabled": True}}},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


# ---- fixtures ---------------------------------------------------------------------------------------------------

def registry_component(name=MANAGED, cid="TMFC012", api="agentregistry",
                       spec_url="http://rc.components.svc.cluster.local:8081/registry-center.yaml", annotations=None,
                       labels=None, uid="rc-uid"):
    return {"apiVersion": "oda.tmforum.org/v1", "kind": "Component",
            "metadata": {"name": name, "namespace": NS, "uid": uid, "annotations": annotations or {},
                         "labels": labels or {}},
            "spec": {"componentMetadata": {"id": cid},
                     "coreFunction": {"exposedAPIs": [{"name": api, "specification": [{"url": spec_url}],
                                                       "implementation": "openan-registry-center-registry-center",
                                                       "port": 5000}]}}}


def card(name="Weather Agent", org="acme", url="https://agents.example.com/weather/a2a", ext=None, owner=None):
    c = {"name": name, "provider": {"organization": org}, "description": "d", "version": "1.0.0",
         "supportedInterfaces": [{"url": url}], "capabilities": {"extensions": list(ext or [])}}
    return cards.with_owner(c, owner) if owner else c


def deploy_ext(**params):
    return {"uri": am.DEPLOY_EXT, "params": {"v": 1, "image": "ghcr.io/project-openan/agents/w:1.2.0", "port": 8080,
                                             **params}}


def registry_ref(**kw):
    base = dict(namespace=NS, component=MANAGED, uid="rc-uid", flavour="native", base_url=RC_URL,
                managed=True, publish=True, deploy_allowed=True)
    return sync.RegistryRef(**{**base, **kw})


def managed(signing=True, publish=True):
    return sync.Managed(MANAGED, signing, publish)


def dereg(version, name, org="acme", kind=sync.DEREGISTERED):
    return {"registry_version": version, "event_type": kind, "data": {"organization": org, "name": name}}


# ---- discovery ----------------------------------------------------------------------------------------------------

def test_registry_is_matched_on_the_api_specification_not_on_tmfc012_alone():
    assert sync.registry_flavour(registry_component())[0] == "native"
    tmf = registry_component(api="tmf639", spec_url="https://x/tmf639-a2a-agent-v5.yaml")
    assert sync.registry_flavour(tmf)[0] == "tmf639"
    generic = registry_component(api="tmf639", spec_url="https://x/TMF639-ResourceInventory-v5.0.0.oas.yaml")
    assert sync.registry_flavour(generic) is None                       # a generic Resource Inventory
    assert sync.registry_flavour(registry_component(cid="TMFC001")) is None  # spec matches, but not an inventory
    both = registry_component()
    both["spec"]["coreFunction"]["exposedAPIs"].insert(0, {"name": "tmf639", "specification": [
        {"url": "https://x/tmf639-a2a-agent-v5.yaml"}], "implementation": "t", "port": 1})
    assert sync.registry_flavour(both)[0] == "native"                   # native preferred


def test_discover_managed_external_and_skipped_components():
    m = sync.discover(registry_component(), managed())
    assert m.managed and m.publish and m.deploy_allowed and m.credentials_secret is None and m.base_url == RC_URL
    partner = registry_component(name="partner", annotations={sync.A_CREDENTIALS: "partner-creds", sync.A_PUBLISH: "true",
                                                              "openan.dev/agent-deploy": "true"})
    ext = sync.discover(partner, managed())
    assert not ext.managed and ext.publish and ext.credentials_secret == "partner-creds"
    assert not ext.deploy_allowed                                       # an annotation never grants deploy trust
    assert sync.discover(partner, managed(), (f"partner={RC_URL}",)).deploy_allowed
    assert not sync.discover(partner, managed(), ("partner",)).deploy_allowed
    assert not sync.discover(partner, managed(), ("partner=http://elsewhere:1",)).deploy_allowed
    ro = sync.discover(registry_component(name="p2", api="tmf639", spec_url="https://x/tmf639-a2a-agent-v5.yaml",
                                          annotations={sync.A_PUBLISH: "true"}), managed())
    assert ro.flavour == "tmf639" and not ro.publish                    # TMF639 registries are read-only
    terminating = registry_component()
    terminating["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    assert sync.discover(terminating, managed()) is None
    assert sync.discover(registry_component(labels={sync.ORIGIN_LABEL: sync.ORIGIN_REGISTRY}), managed()) is None


def test_managed_context_from_the_cr():
    assert sync.managed_context(CR) == sync.Managed(MANAGED, True, True)
    off = {**CR, "spec": {"cardSync": {"enabled": False, "signing": {"enabled": False}}}}
    assert sync.managed_context(off) == sync.Managed(MANAGED, False, False)


def test_object_names_are_short_dns_names_and_collision_free():
    r1, r2 = registry_ref(), registry_ref(component="partner-registry")
    names = {sync.object_name(r, "acme", n) for r in (r1, r2) for n in ("My Agent", "my-agent", "My  Agent!")}
    assert len(names) == 6
    for n in names:
        assert re.fullmatch(r"[a-z]([a-z0-9-]*[a-z0-9])?", n) and len(n) <= 38
    assert sync.object_name(r1, "acme", "My Agent") == sync.object_name(r1, "acme", "My Agent")
    assert sync.object_name(registry_ref(component="1-reg"), "o", "9 lives")[0].isalpha()


def test_keys_and_annotations_are_parsed_defensively():
    assert sync.parse_key("acme/My Agent") == ("acme", "My Agent") and sync.parse_key("nokey") is None
    assert sync.parse_key(None) is None and sync.format_key("a", "b") == "a/b"
    for raw in ("[]", "not json", '{"x": 1}', ""):
        assert sync.registered_cards({"annotations": {sync.REGISTERED_CARDS: raw}}) == {}
    assert sync.registered_cards({"annotations": {sync.REGISTERED_CARDS: '{"r": "a/b"}'}}) == {"r": "a/b"}
    assert sync.parent_component({"labels": {am.COMPONENT_LABEL: "c"}}) == "c"
    assert sync.parent_component({"ownerReferences": [{"kind": "Component", "name": "o"}], "labels": {}}) == "o"


# ---- plan ---------------------------------------------------------------------------------------------------------

def live(name, reg=None, deleting=False, origin=True):
    labels = sync.labels_for(reg or registry_ref(), name) if origin else {am.COMPONENT_LABEL: name}
    return sync.Live(name, labels, deleting)


def test_plan_creates_published_foreign_cards_only():
    agents = [(card("A"), "published"), (card("B"), "registered"), (card("C"), "rejected"),
              (card("D", owner="components/api/u1"), "published")]  # D was published from this cluster (P)
    p = sync.plan(registry_ref(), agents, [])
    assert [k for _, k, _ in p.apply] == [("acme", "A")] and p.delete == []


def test_plan_deletes_only_its_own_mirrors_and_never_touches_others():
    r, other = registry_ref(), registry_ref(component="partner")
    a, gone = sync.object_name(r, "acme", "A"), sync.object_name(r, "acme", "Gone")
    p = sync.plan(r, [(card("A"), "published")],
                  [live(a, r), live(gone, r), live("foreign", other), live("helm-made", origin=False)])
    assert p.delete == [gone]
    collision = sync.plan(r, [(card("A"), "published")], [live(a, origin=False)])
    assert collision.apply == [] and "not made from" in collision.warnings[0]
    held = sync.plan(r, [(card("A"), "published")], [live(a, r, deleting=True)])
    assert held.apply == [] and held.delete == []                       # refused delete in progress: no flapping


def test_mass_delete_needs_two_consecutive_passes():
    names = [f"n{i}" for i in range(6)]
    now, pending = sync.guard_deletes(names, 6, None)
    assert now == [] and pending == set(names)
    assert sync.guard_deletes(names, 6, pending) == (names, None)
    assert sync.guard_deletes(["a", "b"], 3, None) == (["a", "b"], None)  # small removals are not held


def test_deregistered_uses_the_latest_event_per_card():
    events = [dereg(1, "A", kind="AGENT_REGISTERED"), dereg(2, "A"), dereg(3, "B"), dereg(4, "B", kind="AGENT_REGISTERED")]
    assert sync.deregistered(events) == {("acme", "A")}


def _api(component, ref, name="api"):
    return {"spec": {"apiType": "a2a"}, "metadata": {"name": f"{component}-{name}", "labels": {am.COMPONENT_LABEL: component},
                                                     "annotations": {cards.ANNOTATION: ref} if ref else {}}}


def test_a_component_with_two_agents_is_deleted_only_when_both_cards_are_gone():
    comps = {n: {"metadata": {"name": n}} for n in ("one", "two", "keep")}
    comps["keep"]["metadata"]["annotations"] = {sync.A_KEEP: "true"}
    apis = [_api("one", "acme/A"), _api("two", "acme/B", "x"), _api("two", "acme/C", "y"), _api("keep", "acme/D")]
    gone = {("acme", "A"), ("acme", "B"), ("acme", "D")}
    assert sync.components_to_delete(registry_ref(), apis, comps, gone) == ["one"]
    assert sync.components_to_delete(registry_ref(), apis, comps, gone | {("acme", "C")}) == ["one", "two"]


# ---- manifests ----------------------------------------------------------------------------------------------------

def test_deployment_extension_validation():
    ok = am.parse_deployment(card(ext=[deploy_ext(path="/a2a")]), PREFIXES)
    assert ok == {"image": "ghcr.io/project-openan/agents/w:1.2.0", "port": 8080, "path": "/a2a",
                  "health": "/healthz", "replicas": 1}
    assert am.parse_deployment(card(), PREFIXES) is None
    for bad in (deploy_ext(image="docker.io/evil/x:1"), deploy_ext(image="ghcr.io/project-openan/x:latest"),
                deploy_ext(image="ghcr.io/project-openan/x"), deploy_ext(port=0), deploy_ext(env={"A": "b"}),
                deploy_ext(path="/../etc"), {"uri": am.DEPLOY_EXT, "params": {"v": 2}}):
        with pytest.raises(am.DeploymentError):
            am.parse_deployment(card(ext=[bad]), PREFIXES)


@pytest.mark.parametrize("url", ["http://10.0.0.5/a2a", "http://169.254.169.254/latest", "http://127.0.0.1:8080",
                                 "https://agent.components.svc.cluster.local/a2a", "http://localhost/a2a",
                                 "https://agent.components.svc.cluster.local./a2a", "http://localhost./a2a",
                                 "https://user:pw@agents.example.com/a2a", "ftp://agents.example.com", "http://intranet",
                                 "http://agents.example.com:99999/a2a", "http://agents.example.com:x/a2a", "http://[::1/",
                                 "http://127.0.0.01/a2a", "http://0x7f.1/a2a", "http://2130706433/a2a", "http://10.1/a2a",
                                 "http://agents.example.123/a2a"])
def test_mirror_upstream_refuses_internal_and_malformed_targets(url):
    with pytest.raises(am.DeploymentError):
        am.validate_upstream(url)


def test_mirror_upstream_and_allowlist():
    u = am.validate_upstream("https://Agents.Example.com:8443/weather/a2a/")
    assert (u.origin, u.host, u.authority, u.path) == ("https://agents.example.com:8443", "agents.example.com",
                                                       "agents.example.com:8443", "/weather/a2a")
    with pytest.raises(am.DeploymentError):
        am.validate_upstream("https://agents.other.com/a2a", (".example.com",))
    assert am.validate_upstream("http://agent.partner.svc.cluster.local/a2a", (".partner.svc.cluster.local",)).host


def test_mirror_manifests_proxy_to_the_card_url():
    name, reg = "reg-weather-1234abcd", registry_ref()
    objs, warnings = am.build(name, card(url="https://agents.example.com:8443/weather/a2a"), SETTINGS.policy(reg),
                              sync.labels_for(reg, name), sync.annotations_for(reg, "acme", "Weather Agent"))
    assert warnings == [] and [o["kind"] for o in objs] == ["Component", "ConfigMap", "Deployment", "Service"]
    comp = objs[0]
    api = comp["spec"]["coreFunction"]["exposedAPIs"][0]
    assert (api["apiType"], api["implementation"], api["port"], api["path"]) == ("a2a", name, am.PROXY_PORT, f"/{name}/a2a")
    assert api["specification"][0]["url"] == f"http://{name}.{NS}.svc.cluster.local:8080/.well-known/agent-card.json"
    assert comp["metadata"]["labels"][sync.ORIGIN_LABEL] == "registry"
    assert comp["metadata"]["annotations"][am.MODE_ANNOTATION] == "mirror"
    conf = objs[1]["data"]["default.conf.template"]
    assert "set $agent_upstream https://agents.example.com:8443;" in conf           # origin only in proxy_pass
    assert f"rewrite ^/{name}/a2a(/.*)$ /weather/a2a$1 break;" in conf
    assert "proxy_set_header Host agents.example.com:8443;" in conf and "proxy_ssl_name agents.example.com;" in conf
    assert "${NGINX_LOCAL_RESOLVERS}" in conf and "proxy_buffering off;" in conf
    assert all(o["metadata"]["labels"][am.COMPONENT_LABEL] == name for o in objs)


def test_deploy_only_from_trusted_registries_else_mirror_with_a_warning():
    c = card(ext=[deploy_ext()])
    objs, _ = am.build("n-1", c, am.Policy(NS, PREFIXES, "nginx:1", deploy_allowed=True), {}, {})
    assert objs[0]["metadata"]["annotations"][am.MODE_ANNOTATION] == "deploy" and len(objs) == 3
    pod = objs[1]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"].endswith("w:1.2.0") and pod["automountServiceAccountToken"] is False
    objs, warnings = am.build("n-1", c, am.Policy(NS, PREFIXES, "nginx:1"), {}, {})
    assert objs[0]["metadata"]["annotations"][am.MODE_ANNOTATION] == "mirror" and "not trusted" in warnings[0]
    objs, warnings = am.build("n-1", card(ext=[deploy_ext(image="docker.io/x:1")]),
                              am.Policy(NS, PREFIXES, "nginx:1", deploy_allowed=True), {}, {})
    assert objs[0]["metadata"]["annotations"][am.MODE_ANNOTATION] == "mirror" and "outside the allowed" in warnings[0]


def test_spec_hash_is_stable():
    policy = am.Policy(NS, PREFIXES, "nginx:1")
    a = am.build("n-1", card(), policy, {}, {})[0]
    b = am.build("n-1", card(url="https://o.example.com/x"), policy, {}, {})[0]
    assert am.spec_hash(a) == am.spec_hash(copy.deepcopy(a)) != am.spec_hash(b)


# ---- registry sources ---------------------------------------------------------------------------------------------

class FakeRegistry:
    """Registry Center: manage list, change feed, delete (owner isolation via `refuse`)."""

    def __init__(self, cards_=None, events=None, refuse=False, status=200):
        self.cards = list(cards_ or [])  # (card, status)
        self.events, self.refuse, self.status, self.calls = list(events or []), refuse, status, []

    def handler(self, req: httpx.Request):
        path = req.url.path
        self.calls.append((req.method, path, req.headers.get("authorization")))
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if req.method == "GET" and path.endswith("/agent-cards/manage"):
            return httpx.Response(200, json={"agentCards": [{**c, "publishStatus": s, "registration": {"layer": "x"}}
                                                            for c, s in self.cards], "statusCounts": {}})
        if req.method == "GET" and path.endswith("/changes"):
            since, limit = int(req.url.params["since"]), int(req.url.params["limit"])
            after = [e for e in self.events if e["registry_version"] > since]
            page = after[:limit]
            return httpx.Response(200, json={"changes": page, "has_more": len(after) > limit,
                                             "next_since": page[-1]["registry_version"] if page else since})
        if req.method == "DELETE":
            if self.refuse:
                return httpx.Response(403, json={"detail": "agent belongs to someone else"})
            *_, org, name = path.split("/")
            before = len(self.cards)
            self.cards = [(c, s) for c, s in self.cards if sync.card_key(c) != (org, name)]
            return httpx.Response(200 if len(self.cards) < before else 404, json={})
        return httpx.Response(404, json={})

    def client(self, url="http://rc", token="t"):
        return RegistryClient(url, token, client=httpx.Client(base_url=url, transport=httpx.MockTransport(self.handler)))


def test_native_source_strips_review_metadata_and_pages_the_feed():
    reg = FakeRegistry([(card("A"), "published"), (card("B"), "registered")],
                       events=[dereg(v, "A", kind="AGENT_UPDATED") for v in range(1, 2502)])
    src = NativeSource(reg.client())
    agents = src.list_agents()
    assert [(c["name"], s) for c, s in agents] == [("A", "published"), ("B", "registered")]
    assert "publishStatus" not in agents[0][0] and "registration" not in agents[0][0]
    assert src.head() == 2501
    feed = src.changes(2490)
    assert len(feed.events) == 11 and feed.next_since == 2501


def test_native_source_reports_unavailable_instead_of_an_empty_list():
    with pytest.raises(SourceUnavailable) as e:
        NativeSource(FakeRegistry(status=403).client()).list_agents()
    assert e.value.status == 403

    def boom(req):
        raise httpx.ConnectError("refused")
    rc = RegistryClient("http://rc", "t", client=httpx.Client(base_url="http://rc", transport=httpx.MockTransport(boom)))
    with pytest.raises(SourceUnavailable):
        NativeSource(rc).changes(0)


def test_tmf639_source_pages_and_reads_review_status():
    resources = [{"resourceCharacteristic": [{"name": "agentCard", "value": card(f"A{i}")},
                                             {"name": "reviewStatus", "value": "published" if i % 2 else "rejected"}]}
                 for i in range(150)]

    def handler(req):
        off, lim = int(req.url.params["offset"]), int(req.url.params["limit"])
        assert req.url.params["category"] == "A2AAgent"
        return httpx.Response(200, json=resources[off:off + lim], headers={"X-Total-Count": str(len(resources))})
    src = Tmf639Source("http://rc", "t", client=httpx.Client(base_url="http://rc", transport=httpx.MockTransport(handler)))
    agents = src.list_agents()
    assert len(agents) == 150 and sum(1 for _, s in agents if s == "published") == 75
    assert not Tmf639Source.supports_feed


# ---- credentials and chart values ---------------------------------------------------------------------------------

def test_sync_credential_is_added_once_and_replaced_on_new_token():
    s = secrets_bootstrap.new_stack_secrets("x")
    conf, key = s["registry-credentials"]["integration_credentials.conf"], s["registry-runtime"]["INTEGRATION_TOKEN_HMAC_KEY"]
    once = secrets_bootstrap.with_sync_credential(conf, "tok1", key)
    assert secrets_bootstrap.with_sync_credential(once, "tok1", key) == once
    assert "credential.sync.role=nms_oss" in once and "credential.sync.identity=openan-operator-sync" in once
    assert once.count("token_hash=") == 3 and conf.strip() in once           # existing credentials kept verbatim
    twice = secrets_bootstrap.with_sync_credential(once, "tok2", key)
    assert twice.count("credential.sync.token_hash=") == 1 and secrets_bootstrap.token_fingerprint("tok2", key) in twice


def test_credentials_checksum_is_pinned_and_only_set_by_the_operator():
    spec = {"registryCenter": {"values": {"credentialsChecksum": "forged"}}}
    assert "credentialsChecksum" not in stack.chart_values("o", spec, "registryCenter")
    assert stack.chart_values("o", spec, "registryCenter", credentials_checksum="abc")["credentialsChecksum"] == "abc"


def test_agent_sync_switch_and_cr_choice():
    assert stack.agent_sync_enabled({"agentSync": {"enabled": True}}) and not stack.agent_sync_enabled({})
    assert stack.pick_cr([CR], "agentSync") is CR and stack.pick_cr([{**CR, "spec": {}}], "agentSync") is None


# ---- cluster passes against fakes ---------------------------------------------------------------------------------

def _404():
    return client.ApiException(status=404)


def _merge(a, b):
    for k, v in b.items():
        if v is None:
            a.pop(k, None)
        elif isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


class FakeCustom:
    PLURAL = {"components": "Component", "exposedapis": "ExposedAPI", "openans": "OpenAN"}

    def __init__(self, objs):
        self.objs = {(o.get("kind", "Component"), o["metadata"]["name"]): copy.deepcopy(o) for o in objs}
        self.deleted, self.uid, self.fail = [], 0, {}

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=None):
        kind = self.PLURAL[plural]
        want = dict(p.split("=", 1) for p in label_selector.split(",")) if label_selector else {}
        return {"items": [copy.deepcopy(o) for (k, _), o in self.objs.items()
                          if k == kind and all((o["metadata"].get("labels") or {}).get(a) == b for a, b in want.items())]}

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        o = self.objs.get((self.PLURAL[plural], name))
        if o is None:
            raise _404()
        return copy.deepcopy(o)

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        if body["metadata"]["name"] in self.fail:
            raise client.ApiException(status=self.fail[body["metadata"]["name"]])
        self.uid += 1
        body = copy.deepcopy(body)
        body["metadata"]["uid"] = f"uid-{self.uid}"
        self.objs[(self.PLURAL[plural], body["metadata"]["name"])] = body
        return copy.deepcopy(body)

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        o = self.objs.get((self.PLURAL[plural], name))
        if o is None:
            raise _404()
        return copy.deepcopy(_merge(o, copy.deepcopy(body)))

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        if self.objs.pop((self.PLURAL[plural], name), None) is None:
            raise _404()
        self.deleted.append(name)


class FakeCoreApps:
    """CoreV1Api + AppsV1Api: Secrets plus create/read/replace/delete of ConfigMaps, Services and Deployments."""

    def __init__(self, secrets=None):
        self.secrets = dict(secrets or {})
        self.store = {}

    def read_namespaced_secret(self, name, ns):
        if name not in self.secrets:
            raise _404()
        s = self.secrets[name]
        return client.V1Secret(metadata=client.V1ObjectMeta(name=name, labels=s.get("labels"),
                                                            annotations=s.get("annotations")),
                               data={k: base64.b64encode(v.encode()).decode() for k, v in s["data"].items()})

    def __getattr__(self, attr):  # {read,create,replace,delete}_namespaced_{config_map,service,deployment}
        verb, _, kind = attr.partition("_namespaced_")
        store = self.store

        def read(name, ns):
            if (kind, name) not in store:
                raise _404()
            return copy.deepcopy(store[(kind, name)])

        def create(ns, body):
            store[(kind, body["metadata"]["name"])] = {**copy.deepcopy(body), "metadata": {
                **body["metadata"], "resourceVersion": "1"}}

        def replace(name, ns, body):
            assert body["metadata"]["resourceVersion"] == store[(kind, name)]["metadata"]["resourceVersion"]
            store[(kind, name)] = {**copy.deepcopy(body), "metadata": {**body["metadata"], "resourceVersion": "2"}}

        def delete(name, ns):
            if store.pop((kind, name), None) is None:
                raise _404()
        return {"read": read, "create": create, "replace": replace, "delete": delete}[verb]


def secret(data, labels=None, annotations=None):
    return {"data": data, "labels": labels, "annotations": annotations}


def world(monkeypatch, registry, objs=(), secrets=None):
    sio.STATE.clear()
    custom = FakeCustom([registry_component(), *objs])
    core = FakeCoreApps(secrets if secrets is not None else {
        "openan-sync-token": secret({"token": "reader"}), "openan-operator-token": secret({"token": "writer"})})
    apis = sio.Apis(core, custom, core)
    monkeypatch.setattr(sio, "RegistryClient", lambda url, tok: registry.client(url, tok))
    return SimpleNamespace(apis=apis, custom=custom, core=core, registry=registry,
                           reg=sio.discover_registries(apis, NS, CR)[0])


def test_pass_creates_then_is_quiet_then_removes_mirrors(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published"), (card("B"), "published")]))
    first = sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    assert sorted(first.applied.values()) == ["created", "created"]
    name_a = sync.object_name(w.reg, "acme", "A")
    comp = w.custom.objs[("Component", name_a)]
    assert comp["metadata"]["ownerReferences"][0] == {"apiVersion": "oda.tmforum.org/v1", "kind": "Component",
                                                      "name": MANAGED, "uid": "rc-uid", "blockOwnerDeletion": False}
    assert comp["metadata"]["annotations"][sync.A_SPEC_HASH]
    assert w.core.store[("deployment", name_a)]["metadata"]["ownerReferences"][0]["uid"] == comp["metadata"]["uid"]
    assert all(h == "Bearer reader" for _, _, h in w.registry.calls)    # reads use the read-only identity
    assert not sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1010).ran  # no events, resync not due
    w.registry.events.append(dereg(1, "B"))
    w.registry.cards = w.registry.cards[:1]
    gone = sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1020)
    assert gone.mirrors_removed == [sync.object_name(w.reg, "acme", "B")] and gone.applied == {name_a: "unchanged"}
    assert w.custom.deleted == gone.mirrors_removed


def test_one_failing_agent_does_not_starve_the_others(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published"), (card("B", url="http://x:99999/"), "published"),
                                         (card("C"), "published")]))
    w.custom.fail[sync.object_name(w.reg, "acme", "A")] = 422
    res = sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    assert list(res.applied) == [sync.object_name(w.reg, "acme", "C")]
    assert any("422" in x for x in res.warnings) and any("not mirrored" in x for x in res.warnings)
    st = sio.STATE[w.reg.id]
    assert st.cursor == 0 and st.last_pass == sio.NEVER                  # the next tick runs a full pass again


def test_objects_not_made_by_the_sync_are_never_taken_over(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published"), (card("B"), "published")]))
    a, b = sync.object_name(w.reg, "acme", "A"), sync.object_name(w.reg, "acme", "B")
    foreign = {"kind": "Component", "metadata": {"name": a, "uid": "f-uid", "labels": {"team": "x"}}, "spec": {"x": 1}}
    w.custom.objs[("Component", a)] = copy.deepcopy(foreign)
    w.core.store[("deployment", b)] = {"metadata": {"name": b, "resourceVersion": "7"}, "spec": {"mine": "no"}}
    res = sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    assert w.custom.objs[("Component", a)] == foreign                      # untouched
    assert w.core.store[("deployment", b)]["spec"] == {"mine": "no"}       # untouched
    assert ("Component", b) not in w.custom.objs                           # and no half-made agent left behind
    assert sum("not mirrored" in x for x in res.warnings) == 2 and res.applied == {}


def test_updates_replace_children_and_stamp_the_hash_last(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published")]))
    sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    name = sync.object_name(w.reg, "acme", "A")
    w.custom.objs[("Component", name)]["metadata"]["annotations"][sync.A_OP_DELETE] = "true"  # stale marker
    # the card becomes deployable: mirror -> deploy, the proxy ConfigMap goes, containers are replaced, not merged
    w.registry.cards = [(card("A", ext=[deploy_ext(port=9000)]), "published")]
    res = sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=2000)
    assert res.applied == {name: "updated"}
    containers = w.core.store[("deployment", name)]["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in containers] == ["agent"]
    assert w.core.store[("service", name)]["spec"]["ports"] == [{"name": "http", "port": 9000, "targetPort": 9000}]
    assert ("config_map", name) not in w.core.store
    ann = w.custom.objs[("Component", name)]["metadata"]["annotations"]
    assert ann[am.MODE_ANNOTATION] == "deploy" and sync.A_OP_DELETE not in ann


def test_unreachable_registry_changes_nothing(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published")]))
    sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    before = copy.deepcopy(w.custom.objs)
    w.registry.status = 503
    with pytest.raises(SourceUnavailable):
        sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=2000)
    assert w.custom.objs == before and sio.STATE[w.reg.id].cursor == 0


def test_missing_sync_credential_is_unavailable_not_empty(monkeypatch):
    w = world(monkeypatch, FakeRegistry(), secrets={})
    with pytest.raises(SourceUnavailable):
        sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1)


def _k8s_made(name="sample-agent-sampleagent", keep=False):
    comp = {"kind": "Component", "metadata": {"name": name, "annotations": {sync.A_KEEP: "true"} if keep else {}},
            "spec": {}}
    return comp, {**_api(name, "acme/Sample", "a2tagent"), "kind": "ExposedAPI"}


def test_card_deleted_in_registry_deletes_the_kubernetes_made_component(monkeypatch):
    mine = card("Sample", owner="components/sample-agent-sampleagent-a2tagent/api-uid")
    w = world(monkeypatch, FakeRegistry([(mine, "published")]), objs=_k8s_made())
    sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    assert {k[1] for k in w.custom.objs if k[0] == "Component"} == {MANAGED, "sample-agent-sampleagent"}  # P not mirrored
    # published -> pending also emits DEREGISTERED: the card still exists, nothing is deleted
    w.registry.cards = [(mine, "registered")]
    w.registry.events.append(dereg(1, "Sample"))
    assert sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1010).components_removed == []
    w.registry.cards = []
    w.registry.events.append(dereg(2, "Sample"))
    assert sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1020).components_removed == ["sample-agent-sampleagent"]


def test_a_restart_replays_deletions_from_the_stored_cursor(monkeypatch):
    # the card was deleted (version 2) while the operator was down after version 1
    w = world(monkeypatch, FakeRegistry([], events=[dereg(1, "Sample", kind="AGENT_REGISTERED"), dereg(2, "Sample")]),
              objs=_k8s_made())
    assert not sio.first_pass_done(w.reg.id)
    res = sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000, stored_cursor=1)  # stopped after version 1
    assert res.components_removed == ["sample-agent-sampleagent"] and sio.first_pass_done(w.reg.id)


def test_keep_on_card_delete_opts_out(monkeypatch):
    w = world(monkeypatch, FakeRegistry(), objs=_k8s_made(keep=True))
    sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    w.registry.events.append(dereg(1, "Sample"))
    assert sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1010).components_removed == []
    assert ("Component", "sample-agent-sampleagent") in w.custom.objs


def _mirror_meta(reg, name="A", extra=None):
    return {"name": sync.object_name(reg, "acme", name), "ownerReferences": [{"kind": "Component", "uid": reg.uid}],
            "annotations": {**sync.annotations_for(reg, "acme", name), **(extra or {})}}


def _made_meta(w, name="A"):
    sio.run_pass(w.apis, w.reg, CR, SETTINGS, now=1000)
    return w.custom.objs[("Component", sync.object_name(w.reg, "acme", name))]["metadata"]


def test_release_deletes_the_card_or_holds_on_refusal(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published")]))
    made = _made_meta(w)
    assert sio.release(w.apis, {**made, "annotations": {**made["annotations"], sync.A_OP_DELETE: "true"}},
                       NS, CR, SETTINGS) == "operator-initiated"
    assert sio.release(w.apis, _mirror_meta(w.reg, "Missing"), NS, CR, SETTINGS) == "card absent"
    w.registry.refuse = True
    with pytest.raises(sio.DeleteRefused):
        sio.release(w.apis, made, NS, CR, SETTINGS)
    w.registry.refuse = False
    assert sio.release(w.apis, made, NS, CR, SETTINGS) == "card deleted"
    assert ("DELETE", "/rest/v1/registry-center/agent-cards/acme/A", "Bearer writer") in w.registry.calls
    assert w.registry.cards == []


def test_release_requires_the_rendered_hash_of_a_published_card(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published")]))
    made = _made_meta(w)
    imitation = {**made, "annotations": {k: v for k, v in made["annotations"].items() if k != sync.A_SPEC_HASH}}
    assert sio.release(w.apis, imitation, NS, CR, SETTINGS) == "not made from this registry"
    w.registry.cards = [(card("A"), "registered")]
    assert sio.release(w.apis, made, NS, CR, SETTINGS) == "card not published"


def test_release_ignores_forged_labels_and_annotations(monkeypatch):
    mine = card("Sample", owner="components/sample-agent-sampleagent-a2tagent/api-uid")
    w = world(monkeypatch, FakeRegistry([(card("A"), "published"), (mine, "published")]))
    assert sio.release(w.apis, {**_mirror_meta(w.reg), "name": "any-component"}, NS, CR, SETTINGS) == \
        "not made from this registry"
    assert sio.release(w.apis, {**_mirror_meta(w.reg), "ownerReferences": []}, NS, CR, SETTINGS) == \
        "not made from this registry"
    other_ns = _mirror_meta(w.reg)
    other_ns["annotations"][sync.A_SOURCE] = f"elsewhere/{MANAGED}"
    assert sio.release(w.apis, other_ns, NS, CR, SETTINGS) == "no source"
    assert sio.release(w.apis, _mirror_meta(w.reg, "Sample"), NS, CR, SETTINGS) == "card published from Kubernetes"
    assert not any(m == "DELETE" for m, _, _ in w.registry.calls)


def test_release_passes_when_the_registry_is_gone(monkeypatch):
    w = world(monkeypatch, FakeRegistry([(card("A"), "published")]))
    del w.custom.objs[("Component", MANAGED)]
    assert sio.release(w.apis, _mirror_meta(w.reg), NS, CR, SETTINGS) == "registry gone"


def test_connected_credentials_must_be_labelled_and_bound(monkeypatch):
    w = world(monkeypatch, FakeRegistry())
    ext = sync.discover(registry_component(name="partner", annotations={sync.A_CREDENTIALS: "openan-operator-token"}),
                        managed())
    w.core.secrets["openan-operator-token"] = secret({"token": "writer"},
                                                    {sync.MANAGED_BY_KEY: sync.MANAGED_BY_VALUE})
    with pytest.raises(SourceUnavailable):
        sio.credentials(w.apis, ext, CR)                                 # no exfiltration of the operator token
    partner = registry_component(name="partner", annotations={sync.A_CREDENTIALS: "partner-creds"})
    reg = sync.discover(partner, managed())
    w.core.secrets["partner-creds"] = secret({"token": "w", "readToken": "r"}, {sio.CREDENTIALS_LABEL: "true"},
                                             {sync.A_FOR_REGISTRY: "partner", sync.A_FOR_ENDPOINT: reg.base_url})
    assert sio.credentials(w.apis, reg, CR) == ("r", "w")
    fake = sync.discover(registry_component(name="fake", annotations={sync.A_CREDENTIALS: "partner-creds"}), managed())
    with pytest.raises(SourceUnavailable):
        sio.credentials(w.apis, fake, CR)                                # another registry Component
    partner["spec"]["coreFunction"]["exposedAPIs"][0]["implementation"] = "attacker-svc"
    with pytest.raises(SourceUnavailable):
        sio.credentials(w.apis, sync.discover(partner, managed()), CR)  # same name, re-pointed


# ---- handlers -----------------------------------------------------------------------------------------------------

def test_sync_card_skips_exposedapis_of_registry_made_components(monkeypatch):
    custom = FakeCustom([{"kind": "Component", "metadata": {"name": "m-1", "labels": sync.labels_for(registry_ref(), "m-1")}}])
    monkeypatch.setattr(op, "_custom", lambda: custom)
    monkeypatch.setattr(op, "_registry_for", lambda ns: pytest.fail("must not publish a registry-made agent"))
    meta = {"uid": "u", "labels": {am.COMPONENT_LABEL: "m-1"}, "annotations": {}}
    op.sync_card(spec={"apiType": "a2a"}, name="m-1-a2aagent", namespace=NS, meta=meta, status={}, patch=None, logger=LOG)
    meta["labels"][am.COMPONENT_LABEL] = "deleted-component"             # parent gone: nothing to publish either
    op.sync_card(spec={"apiType": "a2a"}, name="x", namespace=NS, meta=meta, status={}, patch=None, logger=LOG)


def test_sync_card_waits_for_the_first_agent_sync_pass(monkeypatch):
    sio.STATE.clear()
    conn = sio.Connection(registry_ref(), "r", "w")
    monkeypatch.setattr(op, "_registry_made_parent", lambda meta, ns: False)
    monkeypatch.setattr(op, "_registry_for", lambda ns: (object(), "acme", None))
    monkeypatch.setattr(op, "_agent_sync_cr", lambda ns: CR)
    monkeypatch.setattr(op, "_connections", lambda ns, cr, logger: [conn])
    monkeypatch.setattr(op, "fetch_card", lambda urls: pytest.fail("must wait for the first agent sync pass"))
    op.sync_card(spec={"apiType": "a2a"}, name="x", namespace=NS, meta={"uid": "u"}, status={}, patch=None, logger=LOG)


def test_release_handler_keeps_the_finalizer_on_refusal(monkeypatch):
    monkeypatch.setattr(op, "_agent_sync_cr", lambda ns: CR)

    def refuse(*a, **k):
        raise sio.DeleteRefused("owned by someone else")
    monkeypatch.setattr(sio, "release", refuse)
    with pytest.raises(kopf.TemporaryError):
        op.release_agent(name="m-1", namespace=NS, meta={}, logger=LOG)
    monkeypatch.setattr(op, "_agent_sync_cr", lambda ns: None)          # agent sync off: never blocks a delete
    op.release_agent(name="m-1", namespace=NS, meta={}, logger=LOG)


def test_static_label_filter_keeps_the_finalizer_off_other_components():
    """kopf adds the operator's finalizer to every object a timer, daemon or delete handler matches. Reading the
    registered handlers from kopf's registry would test kopf; the property that matters is in the decorators, so
    they are read from the source: the only Component handler is the label-filtered delete handler."""
    import inspect
    src = inspect.getsource(op)
    assert re.findall(r'@kopf\.(\w+(?:\.\w+)?)\(ODA, "v1", "components"', src) == ["on.delete"]
    assert 'labels={agent_sync.ORIGIN_LABEL: agent_sync.ORIGIN_REGISTRY}' in src
