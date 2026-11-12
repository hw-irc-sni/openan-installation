# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Agent registry <-> Agent ODA Component sync: pure planning, manifests, registry sources and the cluster passes
against in-memory fakes of the Kubernetes API and of the Registry Center."""
import base64
import copy
import json
import logging
import re

import httpx
import kopf
import pytest
from kubernetes import client

from src import agent_manifests as am
from src import agent_sync as sync
from src import agent_sync_io as io
from src import cards, secrets_bootstrap, stack
from src.registry_client import RegistryClient
from src.registry_source import NativeSource, SourceUnavailable, Tmf639Source

LOG = logging.getLogger("test")
NS = "components"
PREFIXES = ("ghcr.io/project-openan/",)
SETTINGS = io.Settings(PREFIXES, "nginxinc/nginx-unprivileged:1.27-alpine")


# ---- fixtures ---------------------------------------------------------------------------------------------------

def registry_component(name="openan-registry-center-registrycenter", cid="TMFC012", api="agentregistry",
                       spec_url="http://rc.components.svc.cluster.local:8081/registry-center.yaml", annotations=None,
                       labels=None, uid="rc-uid"):
    return {"apiVersion": "oda.tmforum.org/v1", "kind": "Component",
            "metadata": {"name": name, "namespace": NS, "uid": uid, "annotations": annotations or {},
                         "labels": labels or {}},
            "spec": {"componentMetadata": {"id": cid},
                     "coreFunction": {"exposedAPIs": [{"name": api, "specification": [{"url": spec_url}],
                                                       "implementation": "openan-registry-center-registry-center",
                                                       "port": 5000}]}}}


CR = {"metadata": {"name": "openan", "uid": "cr-uid"},
      "spec": {"agentSync": {"enabled": True}, "cardSync": {"enabled": True, "signing": {"enabled": True}}},
      "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
MANAGED = "openan-registry-center-registrycenter"


def card(name="Weather Agent", org="acme", url="https://agents.example.com/weather/a2a", ext=None, owner=None):
    c = {"name": name, "provider": {"organization": org}, "description": "d", "version": "1.0.0",
         "supportedInterfaces": [{"url": url}], "capabilities": {"extensions": list(ext or [])}}
    if owner:
        c = cards.with_owner(c, owner)
    return c


def deploy_ext(**params):
    return {"uri": am.DEPLOY_EXT, "params": {"v": 1, "image": "ghcr.io/project-openan/agents/w:1.2.0", "port": 8080,
                                             **params}}


def ref(**kw):
    base = dict(namespace=NS, component=MANAGED, uid="rc-uid", flavour="native",
                base_url="http://openan-registry-center-registry-center.components.svc.cluster.local:5000",
                managed=True, publish=True, deploy_allowed=True)
    base.update(kw)
    return sync.RegistryRef(**base)


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
    m = sync.discover(registry_component(), MANAGED, True, True)
    assert m.managed and m.publish and m.deploy_allowed and m.credentials_secret is None
    assert m.base_url == "http://openan-registry-center-registry-center.components.svc.cluster.local:5000"
    ext = sync.discover(registry_component(name="partner", annotations={sync.A_CREDENTIALS: "partner-creds",
                                                                        sync.A_PUBLISH: "true"}), MANAGED, True, True)
    assert not ext.managed and ext.publish and not ext.deploy_allowed and ext.credentials_secret == "partner-creds"
    ro = sync.discover(registry_component(name="p2", api="tmf639", spec_url="https://x/tmf639-a2a-agent-v5.yaml",
                                          annotations={sync.A_PUBLISH: "true"}), MANAGED, True, True)
    assert ro.flavour == "tmf639" and not ro.publish                    # TMF639 registries are read-only
    terminating = registry_component()
    terminating["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    assert sync.discover(terminating, MANAGED, True, True) is None
    made = registry_component(labels={sync.ORIGIN_LABEL: sync.ORIGIN_REGISTRY})
    assert sync.discover(made, MANAGED, True, True) is None


def test_object_names_are_short_dns_names_and_collision_free():
    r1, r2 = ref(), ref(component="partner-registry")
    names = {sync.object_name(r, "acme", n) for r in (r1, r2) for n in ("My Agent", "my-agent", "My  Agent!")}
    assert len(names) == 6
    for n in names:
        assert re.fullmatch(r"[a-z]([a-z0-9-]*[a-z0-9])?", n) and len(n) <= 38
    assert sync.object_name(r1, "acme", "My Agent") == sync.object_name(r1, "acme", "My Agent")
    assert sync.object_name(ref(component="1-reg"), "o", "9 lives")[0].isalpha()


# ---- plan ---------------------------------------------------------------------------------------------------------

def live(name, reg=None, deleting=False, origin=True):
    labels = sync.labels_for(reg or ref(), name) if origin else {am.COMPONENT_LABEL: name}
    return sync.Live(name, labels, {}, deleting)


def test_plan_creates_published_foreign_cards_only():
    r = ref()
    agents = [(card("A"), "published"), (card("B"), "registered"), (card("C"), "rejected"),
              (card("D", owner="components/api/u1"), "published")]  # D was published from this cluster (P)
    p = sync.plan(r, agents, [])
    assert [k for _, k, _ in p.apply] == [("acme", "A")] and p.delete == []


def test_plan_deletes_only_its_own_mirrors_and_never_touches_others():
    r, other = ref(), ref(component="partner")
    a = sync.object_name(r, "acme", "A")
    gone = sync.object_name(r, "acme", "Gone")
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
    now, pending = sync.guard_deletes(names, 6, pending)
    assert now == names and pending is None
    assert sync.guard_deletes(["a", "b"], 3, None) == (["a", "b"], None)  # small removals are not held


def test_deregistered_uses_the_latest_event_per_card():
    ev = lambda v, t, n: {"registry_version": v, "event_type": t, "data": {"organization": "acme", "name": n}}
    events = [ev(1, "AGENT_REGISTERED", "A"), ev(2, "AGENT_DEREGISTERED", "A"),
              ev(3, "AGENT_DEREGISTERED", "B"), ev(4, "AGENT_REGISTERED", "B")]
    assert sync.deregistered(events) == {("acme", "A")}


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
                                 "https://user:pw@agents.example.com/a2a", "ftp://agents.example.com", "http://intranet"])
def test_mirror_upstream_refuses_internal_targets(url):
    with pytest.raises(am.DeploymentError):
        am.validate_upstream(url)


def test_mirror_upstream_and_allowlist():
    u = am.validate_upstream("https://Agents.Example.com:8443/weather/a2a/")
    assert (u.origin, u.host, u.path) == ("https://agents.example.com:8443", "agents.example.com", "/weather/a2a")
    with pytest.raises(am.DeploymentError):
        am.validate_upstream("https://agents.other.com/a2a", (".example.com",))
    assert am.validate_upstream("http://agent.partner.svc.cluster.local/a2a", (".partner.svc.cluster.local",)).host


def test_mirror_manifests_proxy_to_the_card_url():
    policy = am.Policy(NS, PREFIXES, "nginxinc/nginx-unprivileged:1.27-alpine")
    mode, objs, warnings = am.build("reg-weather-1234abcd", card(), policy, sync.labels_for(ref(), "reg-weather-1234abcd"),
                                    sync.annotations_for(ref(), "acme", "Weather Agent"))
    assert mode == "mirror" and warnings == [] and [o["kind"] for o in objs] == ["Component", "ConfigMap", "Deployment",
                                                                                    "Service"]
    comp = objs[0]
    api = comp["spec"]["coreFunction"]["exposedAPIs"][0]
    assert api["apiType"] == "a2a" and api["implementation"] == "reg-weather-1234abcd" and api["port"] == am.PROXY_PORT
    assert comp["metadata"]["labels"][sync.ORIGIN_LABEL] == "registry"
    conf = objs[1]["data"]["default.conf.template"]
    assert "set $agent_upstream https://agents.example.com;" in conf            # origin only in proxy_pass
    assert "rewrite ^/reg-weather-1234abcd/a2a(/.*)$ /weather/a2a$1 break;" in conf
    assert "${NGINX_LOCAL_RESOLVERS}" in conf and "proxy_buffering off;" in conf
    assert all(o["metadata"]["labels"][am.COMPONENT_LABEL] == "reg-weather-1234abcd" for o in objs)


def test_deploy_only_from_trusted_registries_else_mirror_with_a_warning():
    c = card(ext=[deploy_ext()])
    trusted = am.Policy(NS, PREFIXES, "nginx:1", deploy_allowed=True)
    mode, objs, _ = am.build("n-1", c, trusted, {}, {})
    assert mode == "deploy" and objs[1]["spec"]["template"]["spec"]["containers"][0]["image"].endswith("w:1.2.0")
    assert objs[1]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    mode, _, warnings = am.build("n-1", c, am.Policy(NS, PREFIXES, "nginx:1"), {}, {})
    assert mode == "mirror" and "not trusted" in warnings[0]
    mode, _, warnings = am.build("n-1", card(ext=[deploy_ext(image="docker.io/x:1")]), trusted, {}, {})
    assert mode == "mirror" and "outside the allowed" in warnings[0]


def test_spec_hash_is_stable():
    policy = am.Policy(NS, PREFIXES, "nginx:1")
    a = am.build("n-1", card(), policy, {}, {})[1]
    assert am.spec_hash(a) == am.spec_hash(copy.deepcopy(a)) != am.spec_hash(am.build("n-1", card(url="https://o.example.com/x"),
                                                                                   policy, {}, {})[1])


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
            org, name = [httpx.URL(req.url).path.split("/")[-2], httpx.URL(req.url).path.split("/")[-1]]
            before = len(self.cards)
            self.cards = [(c, s) for c, s in self.cards if sync.card_key(c) != (org, name.replace("%20", " "))]
            return httpx.Response(200 if len(self.cards) < before else 404, json={})
        return httpx.Response(404, json={})

    def client(self, url="http://rc", token="t"):
        return RegistryClient(url, token, client=httpx.Client(base_url=url, transport=httpx.MockTransport(self.handler)))


def test_native_source_strips_review_metadata_and_pages_the_feed():
    reg = FakeRegistry([(card("A"), "published"), (card("B"), "registered")],
                       events=[{"registry_version": v, "event_type": "AGENT_UPDATED", "data": {}} for v in range(1, 2502)])
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
    assert stack.agent_sync_enabled({"agentSync": {"enabled": True}}) and not stack.agent_sync_enabled({})
    assert stack.pick_cr([CR], "agentSync") is CR and stack.pick_cr([{**CR, "spec": {}}], "agentSync") is None


# ---- cluster passes against fakes ---------------------------------------------------------------------------------

def _404():
    return client.ApiException(status=404)


def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


class FakeCustom:
    def __init__(self, objs):
        self.objs = {(o.get("kind", "Component"), o["metadata"]["name"]): copy.deepcopy(o) for o in objs}
        self.deleted, self.uid = [], 0

    def _plural(self, plural):
        return {"components": "Component", "exposedapis": "ExposedAPI", "openans": "OpenAN"}[plural]

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=None):
        kind, out = self._plural(plural), []
        want = dict(p.split("=", 1) for p in label_selector.split(",")) if label_selector else {}
        for (k, _), o in self.objs.items():
            labels = o["metadata"].get("labels") or {}
            if k == kind and all(labels.get(a) == b for a, b in want.items()):
                out.append(copy.deepcopy(o))
        return {"items": out}

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        o = self.objs.get((self._plural(plural), name))
        if o is None:
            raise _404()
        return copy.deepcopy(o)

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        self.uid += 1
        body = copy.deepcopy(body)
        body["metadata"]["uid"] = f"uid-{self.uid}"
        self.objs[(self._plural(plural), body["metadata"]["name"])] = body
        return copy.deepcopy(body)

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        o = self.objs.get((self._plural(plural), name))
        if o is None:
            raise _404()
        _merge(o, copy.deepcopy(body))
        return copy.deepcopy(o)

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        if self.objs.pop((self._plural(plural), name), None) is None:
            raise _404()
        self.deleted.append(name)


class FakeCoreApps:
    def __init__(self, secrets=None):
        self.secrets = dict(secrets or {})
        self.store, self.patched = {}, []

    def read_namespaced_secret(self, name, ns):
        if name not in self.secrets:
            raise _404()
        data, labels = self.secrets[name]
        return client.V1Secret(metadata=client.V1ObjectMeta(name=name, labels=labels),
                               data={k: base64.b64encode(v.encode()).decode() for k, v in data.items()})

    def __getattr__(self, attr):  # read/create/patch_namespaced_{config_map,service,deployment}
        op, _, kind = attr.partition("_namespaced_")
        if op == "read":
            def read(name, ns):
                if (kind, name) not in self.store:
                    raise _404()
                return self.store[(kind, name)]
            return read
        if op == "create":
            return lambda ns, body: self.store.__setitem__((kind, body["metadata"]["name"]), body)
        if op == "patch":
            return lambda name, ns, body: (self.patched.append((kind, name)), self.store.__setitem__((kind, name), body))
        raise AttributeError(attr)


def world(monkeypatch, registry, objs=(), secrets=None):
    io.STATE.clear()
    custom = FakeCustom([registry_component(), *objs])
    core = FakeCoreApps(secrets if secrets is not None else {
        "openan-sync-token": ({"token": "reader"}, {}), "openan-operator-token": ({"token": "writer"}, {})})
    apis = io.Apis(lambda: core, lambda: custom, lambda: core)
    monkeypatch.setattr(io, "RegistryClient", lambda url, tok: registry.client(url, tok))
    reg = io.discover_registries(apis, NS, CR)[0]
    return apis, custom, core, reg


def test_pass_creates_then_is_quiet_then_removes_mirrors(monkeypatch):
    registry = FakeRegistry([(card("A"), "published"), (card("B"), "published")])
    apis, custom, core, reg = world(monkeypatch, registry)
    first = io.run_pass(apis, reg, CR, SETTINGS, now=1000)
    assert sorted(first.applied.values()) == ["created", "created"]
    name_a = sync.object_name(reg, "acme", "A")
    comp = custom.objs[("Component", name_a)]
    assert comp["metadata"]["ownerReferences"][0] == {"apiVersion": "oda.tmforum.org/v1", "kind": "Component",
                                                      "name": MANAGED, "uid": "rc-uid", "blockOwnerDeletion": False}
    assert core.store[("deployment", name_a)]["metadata"]["ownerReferences"][0]["uid"] == comp["metadata"]["uid"]
    assert all(h == "Bearer reader" for _, _, h in registry.calls)        # reads use the read-only identity
    quiet = io.run_pass(apis, reg, CR, SETTINGS, now=1010)                # no events, resync not due
    assert not quiet.ran
    registry.events.append({"registry_version": 1, "event_type": "AGENT_DEREGISTERED",
                            "data": {"organization": "acme", "name": "B"}})
    registry.cards = registry.cards[:1]
    gone = io.run_pass(apis, reg, CR, SETTINGS, now=1020)
    assert gone.deleted == [sync.object_name(reg, "acme", "B")] and gone.applied == {name_a: "unchanged"}
    assert custom.deleted == gone.deleted


def test_unreachable_registry_changes_nothing(monkeypatch):
    registry = FakeRegistry([(card("A"), "published")])
    apis, custom, _, reg = world(monkeypatch, registry)
    io.run_pass(apis, reg, CR, SETTINGS, now=1000)
    before = copy.deepcopy(custom.objs)
    registry.status = 503
    with pytest.raises(SourceUnavailable):
        io.run_pass(apis, reg, CR, SETTINGS, now=2000)
    assert custom.objs == before and io.STATE[reg.id].cursor == 0


def test_missing_sync_credential_is_unavailable_not_empty(monkeypatch):
    apis, _, _, reg = world(monkeypatch, FakeRegistry(), secrets={})
    with pytest.raises(SourceUnavailable):
        io.run_pass(apis, reg, CR, SETTINGS, now=1)


def _k8s_made(name="sample-agent-sampleagent", keep=False):
    comp = {"kind": "Component", "metadata": {"name": name, "annotations": {sync.A_KEEP: "true"} if keep else {}},
            "spec": {}}
    api = {"kind": "ExposedAPI", "spec": {"apiType": "a2a"},
           "metadata": {"name": f"{name}-a2tagent", "uid": "api-uid", "labels": {am.COMPONENT_LABEL: name},
                        "annotations": {cards.ANNOTATION: "acme/Sample"}}}
    return comp, api


def test_card_deleted_in_registry_deletes_the_kubernetes_made_component(monkeypatch):
    mine = card("Sample", owner="components/sample-agent-sampleagent-a2tagent/api-uid")
    registry = FakeRegistry([(mine, "published")])
    apis, custom, _, reg = world(monkeypatch, registry, objs=_k8s_made())
    io.run_pass(apis, reg, CR, SETTINGS, now=1000)
    assert ("Component", "sample-agent-sampleagent") in custom.objs      # P cards are never mirrored
    assert not any(k[1].startswith("openan-regi") for k in custom.objs if k[0] == "Component" and k[1] != MANAGED)
    # published -> pending also emits DEREGISTERED: the card still exists, nothing is deleted
    registry.cards = [(mine, "registered")]
    registry.events.append({"registry_version": 1, "event_type": "AGENT_DEREGISTERED",
                            "data": {"organization": "acme", "name": "Sample"}})
    assert io.run_pass(apis, reg, CR, SETTINGS, now=1010).k8s_deleted == []
    registry.cards = []
    registry.events.append({"registry_version": 2, "event_type": "AGENT_DEREGISTERED",
                            "data": {"organization": "acme", "name": "Sample"}})
    assert io.run_pass(apis, reg, CR, SETTINGS, now=1020).k8s_deleted == ["sample-agent-sampleagent"]


def test_keep_on_card_delete_opts_out(monkeypatch):
    registry = FakeRegistry([], events=[])
    apis, custom, _, reg = world(monkeypatch, registry, objs=_k8s_made(keep=True))
    io.run_pass(apis, reg, CR, SETTINGS, now=1000)
    registry.events.append({"registry_version": 1, "event_type": "AGENT_DEREGISTERED",
                            "data": {"organization": "acme", "name": "Sample"}})
    assert io.run_pass(apis, reg, CR, SETTINGS, now=1010).k8s_deleted == []
    assert ("Component", "sample-agent-sampleagent") in custom.objs


def _mirror_meta(reg, name="A", extra=None):
    return {"name": sync.object_name(reg, "acme", name), "annotations": {**sync.annotations_for(reg, "acme", name),
                                                                         **(extra or {})}}


def test_release_deletes_the_card_or_holds_on_refusal(monkeypatch):
    registry = FakeRegistry([(card("A"), "published")])
    apis, _, _, reg = world(monkeypatch, registry)
    assert io.release(apis, _mirror_meta(reg, extra={sync.A_OP_DELETE: "true"}), NS, CR) == "operator-initiated"
    assert io.release(apis, _mirror_meta(reg, "Missing"), NS, CR) == "card absent"
    registry.refuse = True
    with pytest.raises(io.DeleteRefused):
        io.release(apis, _mirror_meta(reg), NS, CR)
    registry.refuse = False
    assert io.release(apis, _mirror_meta(reg), NS, CR) == "card deleted"
    assert ("DELETE", "/rest/v1/registry-center/agent-cards/acme/A", "Bearer writer") in registry.calls
    assert registry.cards == []


def test_release_passes_when_the_registry_is_gone(monkeypatch):
    apis, custom, _, reg = world(monkeypatch, FakeRegistry([(card("A"), "published")]))
    del custom.objs[("Component", MANAGED)]
    assert io.release(apis, _mirror_meta(reg), NS, CR) == "registry gone"


def test_foreign_credentials_secret_must_be_labelled(monkeypatch):
    apis, custom, core, _ = world(monkeypatch, FakeRegistry())
    ext = registry_component(name="partner", annotations={sync.A_CREDENTIALS: "openan-operator-token"})
    reg = sync.discover(ext, MANAGED, True, True)
    core.secrets["openan-operator-token"] = ({"token": "writer"}, {"app.kubernetes.io/managed-by": "openan-operator"})
    with pytest.raises(SourceUnavailable):
        io.credentials(apis, reg, CR)                                     # no exfiltration of the operator token
    core.secrets["partner-creds"] = ({"token": "w", "readToken": "r"}, {io.CREDENTIALS_LABEL: "true"})
    reg = sync.discover(registry_component(name="partner", annotations={sync.A_CREDENTIALS: "partner-creds"}),
                        MANAGED, True, True)
    assert io.credentials(apis, reg, CR) == ("r", "w")


# ---- handlers -----------------------------------------------------------------------------------------------------

def test_sync_card_skips_exposedapis_of_registry_made_components(monkeypatch):
    from src import openan_operator as op
    custom = FakeCustom([{"kind": "Component", "metadata": {"name": "m-1", "labels": sync.labels_for(ref(), "m-1")}}])
    monkeypatch.setattr(op, "_custom", lambda: custom)
    monkeypatch.setattr(op, "_registry_for", lambda ns: pytest.fail("must not publish a registry-made agent"))
    meta = {"uid": "u", "labels": {am.COMPONENT_LABEL: "m-1"}, "annotations": {}}
    op.sync_card(spec={"apiType": "a2a"}, name="m-1-a2aagent", namespace=NS, meta=meta, status={}, patch=None, logger=LOG)
    meta["labels"][am.COMPONENT_LABEL] = "deleted-component"             # parent gone: nothing to publish either
    op.sync_card(spec={"apiType": "a2a"}, name="x", namespace=NS, meta=meta, status={}, patch=None, logger=LOG)


def test_release_handler_keeps_the_finalizer_on_refusal(monkeypatch):
    from src import openan_operator as op
    monkeypatch.setattr(op, "_agent_sync_cr", lambda ns: CR)

    def refuse(*a, **k):
        raise io.DeleteRefused("owned by someone else")
    monkeypatch.setattr(io, "release", refuse)
    with pytest.raises(kopf.TemporaryError):
        op.release_agent(name="m-1", namespace=NS, meta={}, logger=LOG)


def test_static_label_filter_keeps_the_finalizer_off_other_components():
    from src import openan_operator as op
    import inspect
    src = inspect.getsource(op)
    # The only Component handler is the label-filtered delete handler; timers/daemons on Components would put the
    # operator's finalizer on Helm-owned registry Components.
    assert re.findall(r'@kopf\.(\w+(?:\.\w+)?)\(ODA, "v1", "components"', src) == ["on.delete"]
    assert 'labels={agent_sync.ORIGIN_LABEL: agent_sync.ORIGIN_REGISTRY}' in src
