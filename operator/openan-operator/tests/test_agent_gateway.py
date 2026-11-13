# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""a2a ExposedAPIs -> agentgateway routes: pure planning and the ConfigMap pass against in-memory fakes of the
Kubernetes API and of the gateway's read port."""
import base64
from types import SimpleNamespace

import pytest
import yaml
from kubernetes import client

from src import agent_manifests as am
from src import cards
from src import gateway_io as gio
from src import gateway_routes as gr
from src import openan_operator as op
from src import stack

NS = "components"
TARGET = gr.GatewayTarget("canvas", "agentgateway-config", "agentgateway-portal-key",
                          "http://agentgateway.canvas.svc.cluster.local:8081", "default", "/agents")
FOREIGN = [{"name": "sample-agent", "matches": [{"path": {"pathPrefix": "/agents/sample-agent"}}],
            "backends": [{"host": "sample-agent.components.svc.cluster.local:8080"}]},
           {"name": "traffic-demo", "matches": [{"path": {"pathPrefix": "/demo/traffic"}, "method": "GET"}],
            "backends": [{"host": "agentgateway-test-mcp-server:8000"}]}]
BASE = {"binds": [{"port": 8081, "listeners": [{"routes": []}]}], "llm": {"models": [{"name": "m"}]},
        "mcp": {"targets": [{"name": "test-mcp"}]}, "routes": FOREIGN}


def exposed(name, component, api_type="a2a", impl=None, port=8080, deleting=False):
    meta = {"name": name, "labels": {"oda.tmforum.org/componentName": component}}
    if deleting:
        meta["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    return {"metadata": meta, "spec": {"apiType": api_type, "implementation": impl or component, "port": port,
                                       "path": f"/{component}/a2a"}}


def component(name, deleting=False):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    return {"metadata": meta}


# ---- pure planning ------------------------------------------------------------------------------------------------

def test_one_a2a_route_per_exposedapi_to_its_own_service():
    apis = [exposed("sample-agent-sampleagent-a2tagent", "sample-agent-sampleagent", impl="sample-agent"),
            exposed("pc-api", "pc", api_type="openapi"),
            exposed("gone-a2a", "gone"),                              # parent Component missing
            exposed("old-a2a", "old", deleting=True),
            exposed("dying-a2a", "dying")]
    comps = {"sample-agent-sampleagent": component("sample-agent-sampleagent"), "pc": component("pc"),
             "old": component("old"), "dying": component("dying", deleting=True)}
    routes, warnings = gr.desired_routes(TARGET, NS, apis, comps)
    assert warnings == []
    assert routes == [{
        "name": "openan.components.sample-agent-sampleagent-a2tagent", "gateways": "default",
        "matches": [{"path": {"pathPrefix": "/agents/components/sample-agent-sampleagent-a2tagent"}}],
        "policies": {"a2a": {}, "urlRewrite": {"path": {"prefix": "/"}}, "timeout": {"requestTimeout": "120s"}},
        "backends": [{"host": "sample-agent.components.svc.cluster.local:8080"}]}]


@pytest.mark.parametrize("impl,port", [("evil.example.com", 8080), ("UPPER", 8080), ("ok", 0), ("ok", "80"),
                                       ("ok", True), ("$HOME", 8080)])
def test_bad_implementation_or_port_is_reported_not_routed(impl, port):
    routes, warnings = gr.desired_routes(TARGET, NS, [exposed("x-a2a", "x", impl=impl, port=port)],
                                         {"x": component("x")})
    assert routes == [] and len(warnings) == 1


def test_merge_keeps_foreign_routes_in_order_and_replaces_only_this_namespaces_routes():
    other_ns = {"name": "openan.components-x.agent", "matches": [{"path": {"pathPrefix": "/agents/components-x/a"}}]}
    stale = {"name": "openan.components.removed", "matches": [{"path": {"pathPrefix": "/agents/components/removed"}}]}
    wanted, _ = gr.desired_routes(TARGET, NS, [exposed("a-a2a", "a")], {"a": component("a")})
    routes, warnings = gr.merge(FOREIGN + [other_ns, stale], wanted, NS)
    assert routes == FOREIGN + [other_ns] + wanted and warnings == []
    assert gr.merge(routes, wanted, NS)[0] == routes            # steady state: nothing to change


def test_merge_never_takes_a_path_another_route_serves():
    taken = {"name": "manual", "matches": [{"path": {"pathPrefix": "/agents/components/a-a2a/"}}]}
    wanted, _ = gr.desired_routes(TARGET, NS, [exposed("a-a2a", "a")], {"a": component("a")})
    routes, warnings = gr.merge([taken], wanted, NS)
    assert routes == [taken] and "already served" in warnings[0]


def test_routes_with_a_dollar_are_unsafe():
    assert gr.unsafe([{"name": "openan.ns.$X"}]) and not gr.unsafe([{"name": "openan.ns.x"}])


def test_applied_reads_route_names_from_the_config_dump():
    dump = {"binds": [{"listeners": {"l": {"routes": {
        "default/openan.components.a-a2a": {"name": "openan.components.a-a2a"},
        "default/sample-agent": {"name": "sample-agent"}}}}}]}
    assert gr.applied(dump, ["openan.components.a-a2a"], NS)
    assert not gr.applied(dump, ["openan.components.a-a2a", "openan.components.b-a2a"], NS)
    assert not gr.applied(dump, [], NS)                          # a removed route still running
    assert gr.applied({"binds": []}, [], NS)


def test_path_prefix_setting_is_validated(monkeypatch):
    monkeypatch.setenv("AGENTGATEWAY_NAMESPACE", "canvas")
    monkeypatch.setenv("AGENTGATEWAY_PATH_PREFIX", "/a2a/")
    t = op.gateway_target()
    assert (t.path_prefix, t.config_url) == ("/a2a", "http://agentgateway.canvas.svc.cluster.local:8081")
    for bad in ("agents", "/a/../b", "/a b", "/"):
        monkeypatch.setenv("AGENTGATEWAY_PATH_PREFIX", bad)
        with pytest.raises(Exception):
            op.gateway_target()
    monkeypatch.setenv("AGENTGATEWAY_NAMESPACE", "")
    assert op.gateway_target() is None


def test_gateway_routes_follow_the_oldest_ready_cr_and_can_be_switched_off():
    ready = {"conditions": [{"type": "Ready", "status": "True"}]}
    a = {"metadata": {"name": "a", "creationTimestamp": "1"}, "spec": {}, "status": ready}
    b = {"metadata": {"name": "b", "creationTimestamp": "2"}, "spec": {}, "status": ready}
    assert stack.pick_cr([b, a], "agentGateway")["metadata"]["name"] == "a"
    a["spec"]["agentGateway"] = {"enabled": False}
    assert stack.pick_cr([b, a], "agentGateway")["metadata"]["name"] == "b"


def test_mirror_serves_a_card_that_points_at_the_proxy():
    c = {"name": "W", "supportedInterfaces": [{"url": "https://agents.example.com/w/a2a", "protocolBinding": "JSONRPC"},
                                              {"url": "https://agents.example.com/w/grpc"}]}
    served = am.served_card("reg-w-1234abcd", c, NS)
    assert served["supportedInterfaces"] == [
        {"url": "http://reg-w-1234abcd.components.svc.cluster.local:8080/reg-w-1234abcd/a2a", "protocolBinding": "JSONRPC"},
        {"url": "https://agents.example.com/w/grpc"}]
    assert c["supportedInterfaces"][0]["url"] == "https://agents.example.com/w/a2a"   # input unchanged


# ---- the ConfigMap pass ---------------------------------------------------------------------------------------

class FakeKube:
    """CoreV1Api (ConfigMap with resourceVersion, key Secret) and CustomObjectsApi (ExposedAPIs, Components)."""

    def __init__(self, doc=None, apis=(), comps=()):
        self.text, self.version, self.writes = yaml.safe_dump(doc if doc is not None else BASE), 1, []
        self.apis, self.comps, self.conflict, self.missing = list(apis), list(comps), False, False

    def read_namespaced_config_map(self, name, ns):
        assert (name, ns) == ("agentgateway-config", "canvas")
        if self.missing:
            raise client.ApiException(status=404)
        return SimpleNamespace(data={"config.yaml": self.text}, metadata=SimpleNamespace(resource_version=str(self.version)))

    def patch_namespaced_config_map(self, name, ns, body):
        if self.conflict or body["metadata"]["resourceVersion"] != str(self.version):
            raise client.ApiException(status=409)
        self.text, self.version = body["data"]["config.yaml"], self.version + 1
        self.writes.append(self.text)
        return SimpleNamespace(metadata=SimpleNamespace(resource_version=str(self.version)))

    def read_namespaced_secret(self, name, ns):
        assert (name, ns) == ("agentgateway-portal-key", "canvas")
        return SimpleNamespace(data={"key": base64.b64encode(b"k").decode()})

    def list_namespaced_custom_object(self, group, version, ns, plural):
        return {"items": self.apis if plural == "exposedapis" else self.comps}

    def routes(self):
        return yaml.safe_load(self.text).get("routes")

    def outside_writes(self, doc):  # Helm or the portal rewriting the file
        self.text, self.version = yaml.safe_dump(doc), self.version + 1


class FakeGateway:
    """Port 8081: reload status and a config dump made of the route names of the file it has loaded."""

    def __init__(self, kube):
        self.kube, self.loaded, self.error, self.down = kube, kube.routes(), None, False

    def reload(self, error=None):
        self.error = error
        if error is None:
            self.loaded = self.kube.routes()

    def get(self, url, headers=None, timeout=None):
        assert headers == {"Authorization": "Bearer k"}
        if self.down:
            raise gio.httpx.ConnectError("down")
        body = ({"configReload": {"lastError": self.error}} if url.endswith("/api/runtime") else
                {"binds": [{"listeners": {"l": {"routes": {f"default/{r['name']}": {"name": r["name"]}
                                                           for r in self.loaded or []}}}}]})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


@pytest.fixture(autouse=True)
def fresh_state():
    gio.STATE.clear()
    yield
    gio.STATE.clear()


def world(**kw):
    kube = FakeKube(apis=[exposed("a-a2a", "a"), exposed("b-a2a", "b", impl="b-svc", port=9000)],
                    comps=[component("a"), component("b")], **kw)
    return kube, FakeGateway(kube)


def run(kube, gw, now=0.0):
    return gio.sync_routes(kube, kube, NS, TARGET, now=now, get=gw.get)


def test_pass_adds_routes_keeps_the_rest_of_the_file_then_is_quiet():
    kube, gw = world()
    res = run(kube, gw)
    assert res.changed and res.routes == ["openan.components.a-a2a", "openan.components.b-a2a"]
    doc = yaml.safe_load(kube.text)
    assert doc["routes"][:2] == FOREIGN and doc["llm"] == BASE["llm"] and doc["mcp"] == BASE["mcp"]
    assert doc["routes"][3]["backends"] == [{"host": "b-svc.components.svc.cluster.local:9000"}]
    assert run(kube, gw, now=1).changed is False and len(kube.writes) == 1       # waits for the gateway
    gw.reload()
    res = run(kube, gw, now=2)
    assert res.confirmed and not res.changed and len(kube.writes) == 1           # loaded, nothing to change


def test_removed_agent_loses_its_route_and_helm_resets_are_repaired():
    kube, gw = world()
    run(kube, gw); gw.reload(); run(kube, gw, now=1)
    kube.apis = kube.apis[:1]
    assert run(kube, gw, now=2).changed and [r["name"] for r in kube.routes()][2:] == ["openan.components.a-a2a"]
    gw.reload(); run(kube, gw, now=3)
    kube.outside_writes(BASE)                                                     # `make agentgateway`
    assert run(kube, gw, now=4).changed and kube.routes()[2]["name"] == "openan.components.a-a2a"


def test_a_write_the_gateway_rejects_is_put_back_and_not_retried_until_the_routes_change():
    kube, gw = world()
    original = kube.text
    run(kube, gw)
    gw.reload(error="invalid route")
    res = run(kube, gw, now=1)
    assert res.reverted and kube.text == original and "rejected" in res.warnings[0]
    assert run(kube, gw, now=2).changed is False and kube.text == original
    kube.apis.append(exposed("c-a2a", "c")); kube.comps.append(component("c"))
    assert run(kube, gw, now=3).changed                                           # different routes: tried again


def test_a_concurrent_change_is_not_overwritten():
    kube, gw = world()
    kube.conflict = True
    res = run(kube, gw)
    assert not res.changed and "same time" in res.warnings[0] and kube.writes == []


def test_someone_writing_after_us_ends_the_wait_and_an_unreachable_gateway_times_out():
    kube, gw = world()
    run(kube, gw)
    kube.outside_writes({**yaml.safe_load(kube.text), "llm": {"models": []}})   # portal edits a model
    assert run(kube, gw, now=1).changed is False                                  # our routes are still in it
    kube2, gw2 = world()
    gw2.down = True
    run(kube2, gw2)
    assert run(kube2, gw2, now=10).warnings == []
    res = run(kube2, gw2, now=gio.CONFIRM_SECONDS + 1)
    assert "not confirmed" in res.warnings[0]


def test_missing_configmap_is_reported_as_unavailable():
    kube, gw = world()
    kube.missing = True
    with pytest.raises(gio.GatewayUnavailable):
        run(kube, gw)


def test_warnings_are_logged_once_per_appearance():
    st = gio.State()
    assert st.new_warnings(["a", "b"]) == ["a", "b"]
    assert st.new_warnings(["a", "b"]) == []
    assert st.new_warnings(["b"]) == [] and st.new_warnings(["a"]) == ["a"]



# ---- cards advertising the gateway route (AGENTGATEWAY_PUBLISH_URL, opt-in) ----------------------------------

SERVICE = "http://sample-agent.components.svc.cluster.local:8080"
RAW = {"name": "Sample", "description": "d", "version": "1",
       "supportedInterfaces": [{"url": SERVICE + "/a2a", "protocolBinding": "JSONRPC"},
                               {"url": "https://127.0.0.1/sample-agent-sampleagent/a2a"}]}
API_SPEC = {"apiType": "a2a", "implementation": "sample-agent", "port": 8080}


def test_card_points_at_the_gateway_route_only_when_asked():
    hosts = cards.allowed_hosts(API_SPEC, NS, ["https://127.0.0.1/x"])
    plain = cards.build_signed_card(RAW, "OpenAN", None, hosts=hosts)
    assert plain["supportedInterfaces"][0]["url"] == SERVICE + "/a2a"
    rewrite = lambda c: gr.via_gateway(c, cards.allowed_hosts(API_SPEC, NS, []),  # noqa: E731
                                       "http://agentgateway.canvas.svc.cluster.local/", TARGET, NS, "sa-a2a")
    card = cards.build_signed_card(RAW, "OpenAN", None, owner="o", hosts=hosts, rewrite=rewrite)
    assert [i["url"] for i in card["supportedInterfaces"]] == [
        "http://agentgateway.canvas.svc.cluster.local/agents/components/sa-a2a/a2a",
        "https://127.0.0.1/sample-agent-sampleagent/a2a"]                     # Canvas gateway URL kept
    assert cards.card_owner(card) == "o"


def test_a_foreign_endpoint_is_refused_before_any_rewrite():
    raw = {**RAW, "supportedInterfaces": [{"url": "http://evil.example.com/a2a"}]}
    with pytest.raises(cards.OwnershipError):
        cards.build_signed_card(raw, "OpenAN", None, hosts=cards.allowed_hosts(API_SPEC, NS, []),
                                rewrite=lambda c: gr.via_gateway(c, {"evil.example.com"}, "http://gw", TARGET, NS, "x"))


@pytest.mark.parametrize("url,ok", [("http://agentgateway.canvas.svc.cluster.local", True),
                                    ("https://gw.example.com:8443/base/", True), ("http://gw/", True),
                                    ("ftp://gw", False), ("http://u:p@gw", False), ("http://gw?x=1", False),
                                    ("http://$HOST", False), ("http://gw:99999", False), ("gw", False)])
def test_publish_url_is_validated(url, ok):
    assert gr.valid_publish_url(url) is ok


def test_publish_url_is_off_by_default_and_needs_the_routes(monkeypatch):
    log = SimpleNamespace(info=lambda *_: None)
    monkeypatch.delenv("AGENTGATEWAY_PUBLISH_URL", raising=False)
    monkeypatch.setenv("AGENTGATEWAY_NAMESPACE", "canvas")
    assert op._gateway_card_rewrite(API_SPEC, "sa-a2a", NS, log) is None
    monkeypatch.setenv("AGENTGATEWAY_PUBLISH_URL", "http://agentgateway.canvas.svc.cluster.local")
    ready = {"metadata": {"name": "openan", "creationTimestamp": "1"}, "spec": {},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    custom = SimpleNamespace(list_namespaced_custom_object=lambda *a: {"items": [ready]})
    monkeypatch.setattr(op, "_custom", lambda: custom)
    rewrite = op._gateway_card_rewrite(API_SPEC, "sa-a2a", NS, log)
    assert rewrite({"supportedInterfaces": [{"url": SERVICE + "/a2a"}]})["supportedInterfaces"][0]["url"] == \
        "http://agentgateway.canvas.svc.cluster.local/agents/components/sa-a2a/a2a"
    ready["spec"]["agentGateway"] = {"enabled": False}
    assert op._gateway_card_rewrite(API_SPEC, "sa-a2a", NS, log) is None
    monkeypatch.setenv("AGENTGATEWAY_NAMESPACE", "")
    ready["spec"] = {}
    assert op._gateway_card_rewrite(API_SPEC, "sa-a2a", NS, log) is None
