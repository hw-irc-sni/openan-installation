# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import hashlib
import hmac

import httpx
import pytest

from src import card_mapper, card_signer, cards, secrets_bootstrap, stack
from src.registry_client import RegistryClient, RegistryError

V03 = {"name": "Weather Agent!", "description": "d", "version": "1.2", "url": "http://a:8080/a2a",
       "skills": [{"id": "w", "name": "weather", "description": "x"}], "capabilities": {"streaming": True}}


def test_mapper_converts_pre_v1_card_and_injects_provider():
    c = card_mapper.to_registry_card(V03, "acme")
    assert c["provider"] == {"organization": "acme"}
    assert c["name"] == "Weather Agent-"
    assert c["supportedInterfaces"][0]["url"] == "http://a:8080/a2a"
    assert c["skills"][0]["id"] == "w"


def test_mapper_limits_and_errors():
    big = {**V03, "description": "x" * 5000, "skills": [{"id": str(i)} for i in range(300)]}
    c = card_mapper.to_registry_card(big, "o")
    assert len(c["description"]) == 1000 and len(c["skills"]) == 100
    with pytest.raises(card_mapper.CardError):
        card_mapper.to_registry_card({"name": "n", "description": "d"}, "o")
    with pytest.raises(card_mapper.CardError):
        card_mapper.to_registry_card(V03, "")


def test_sign_and_verify_roundtrip_and_tamper():
    priv, pub = card_signer.generate_keypair()
    card = card_mapper.to_registry_card(V03, "acme")
    signed = card_signer.sign_card(card, priv, "k1", "https://keys.example/jwks")
    assert signed["signatures"][0]["protected"] and card_signer.verify_card(signed, pub)
    signed["description"] = "tampered"
    assert not card_signer.verify_card(signed, pub)
    assert card_signer.public_jwk(pub, "k1")["crv"] == "P-256"


def test_credentials_use_registry_hmac_algorithm():
    s = secrets_bootstrap.new_stack_secrets("x")
    key = s["registry-runtime"]["INTEGRATION_TOKEN_HMAC_KEY"]
    token = s["operator-token"]["token"]
    want = hmac.new(key.encode(), token.encode(), hashlib.sha256).hexdigest()
    conf = s["registry-credentials"]["integration_credentials.conf"]
    assert f"token_hash={want}" in conf and "role=vendor_agent" in conf
    assert s["orchestration-runtime"]["REGISTRY_ACCESS_TOKEN"] != token


def test_chart_values_wiring_and_override():
    spec = {"registryCenter": {"values": {"image": {"tag": "1"}}},
            "orchestrationCenter": {"values": {"image": {"tag": "2"}, "registryUrl": "http://custom"}},
            "frontend": {"values": {"image": {"tag": "3"}}}, "cardSync": {"signing": {"enabled": False}}}
    rc = stack.chart_values("o", spec, "registryCenter")
    assert rc["credentialSecret"] == "o-registry-credentials" and rc["registry"]["signatureValidation"] is False
    assert rc["image"]["tag"] == "1"
    # registryUrl is pinned: the OC sends its Registry token there, so a CR may not redirect it
    assert stack.chart_values("o", spec, "orchestrationCenter")["registryUrl"] == "http://o-registry-center-registry-center:5000"
    assert stack.chart_values("o", spec, "frontend")["backendUrl"] == "http://o-orchestration-center-orchestration-center:5001"


def test_readiness_and_enabled_order():
    assert stack.is_ready({"status": {"summary/status": {"deployment_status": "Complete"}}})
    assert not stack.is_ready({"status": {}}) and not stack.is_ready(None)
    assert stack.enabled_components({"frontend": {"enabled": False}}) == ["registryCenter", "orchestrationCenter"]


def test_candidate_urls():
    u = cards.candidate_urls({"implementation": "agent", "port": 8080, "path": "/c/a2a"}, "components")
    assert u[0] == "http://agent.components.svc.cluster.local:8080/.well-known/agent-card.json" and len(u) == 2


class FakeRC:
    def __init__(self, exists=False, post_status=201, empty_ok=False):
        self.calls, self.exists, self.post_status, self.empty_ok = [], exists, post_status, empty_ok

    def handler(self, req: httpx.Request):
        self.calls.append((req.method, req.url.raw_path.decode(), req.headers.get("authorization")))
        if req.method == "GET":
            if self.empty_ok:
                return httpx.Response(200, json={"agentCards": [CARD] if self.exists else []})
            return httpx.Response(200 if self.exists else 404, json={})
        if req.method == "POST":
            return httpx.Response(self.post_status, json={})
        return httpx.Response(200, json={})


def make(fake):
    return RegistryClient("http://rc", "tok", client=httpx.Client(base_url="http://rc", transport=httpx.MockTransport(fake.handler)))


CARD = {"name": "My Agent", "provider": {"organization": "acme"}}


def test_upsert_creates_then_replaces_and_url_encodes():
    f = FakeRC(); assert make(f).upsert(CARD) == "created"
    assert [c[0] for c in f.calls] == ["GET", "POST"] and f.calls[0][2] == "Bearer tok"
    assert "My%20Agent" in f.calls[0][1]
    f = FakeRC(exists=True); assert make(f).upsert(CARD) == "updated"
    assert [c[0] for c in f.calls] == ["GET", "PUT"]


def test_upsert_conflict_race_falls_back_to_put_and_errors_surface():
    f = FakeRC(post_status=409); assert make(f).upsert(CARD) == "updated"
    assert [c[0] for c in f.calls] == ["GET", "POST", "GET", "PUT"]  # the re-read after 409 re-checks ownership
    with pytest.raises(RegistryError):
        make(FakeRC(post_status=422)).upsert(CARD)


def test_delete_missing_is_not_an_error():
    class F(FakeRC):
        def handler(self, req):
            return httpx.Response(404)
    assert make(F()).delete("acme", "x") is False


def test_unknown_card_returned_as_empty_list_is_treated_as_missing():
    f = FakeRC(empty_ok=True); assert make(f).upsert(CARD) == "created"
    assert [c[0] for c in f.calls] == ["GET", "POST"]
    f = FakeRC(exists=True, empty_ok=True); assert make(f).upsert(CARD) == "updated"
    assert [c[0] for c in f.calls] == ["GET", "PUT"]


def test_signing_trust_store_wiring_and_file_safe_names():
    spec = {"registry": {"organization": "acme"}, "registryCenter": {"values": {"image": {"tag": "1"}}}}
    rc = stack.chart_values("o", spec, "registryCenter")
    assert rc["trust"] == {"organization": "acme", "configMap": "o-registry-trust"}
    assert rc["registry"]["signatureValidation"] is True
    off = stack.chart_values("o", {**spec, "cardSync": {"signing": {"enabled": False}}}, "registryCenter")
    assert "trust" not in off
    card = {**V03, "name": "Sample Weather Agent"}
    assert card_mapper.to_registry_card(card, "acme", allow_spaces=False)["name"] == "Sample-Weather-Agent"
    priv, pub = card_signer.generate_keypair()
    signed = cards.build_signed_card(card, "acme", {"private": priv, "kid": "k", "trust_store": True})
    assert signed["name"] == "Sample-Weather-Agent" and card_signer.verify_card(signed, pub)
    doc = __import__("json").loads(cards.trust_document(card_signer.public_jwk(pub, "k")))
    assert doc["keys"][0]["kid"] == "k"


def test_card_is_bound_to_source_exposedapi_and_own_endpoints():
    api = {"implementation": "agent", "port": 8080}
    hosts = cards.allowed_hosts(api, "components", ["https://127.0.0.1/x/a2a"])
    raw = {**V03, "url": "http://agent.components.svc.cluster.local:8080/a2a"}
    me = cards.owner_ref("components", "api-a", "uid-1")
    card = cards.build_signed_card(raw, "acme", None, owner=me, hosts=hosts)
    assert cards.card_owner(card) == me and cards.card_owner({"capabilities": {}}) is None
    with pytest.raises(cards.OwnershipError):  # endpoint of somebody else's service
        cards.build_signed_card({**V03, "url": "http://other.components.svc:8080/a2a"}, "acme", None, owner=me, hosts=hosts)
    again = cards.with_owner(card, cards.owner_ref("components", "api-b", "uid-2"))
    assert len(again["capabilities"]["extensions"]) == 1 and cards.card_owner(again) != me


def test_owner_extension_is_covered_by_signature():
    priv, pub = card_signer.generate_keypair()
    card = cards.build_signed_card(V03, "acme", {"private": priv, "kid": "k"}, owner="ns/a/u1")
    assert card_signer.verify_card(card, pub)
    card["capabilities"]["extensions"][0]["params"]["owner"] = "ns/evil/u2"
    assert not card_signer.verify_card(card, pub)


def test_orchestration_access_password_hash_matches_oc_scheme():
    import bcrypt
    s = secrets_bootstrap.new_stack_secrets("x")["orchestration-runtime"]
    stored = s["ORCH_ACCESS_PASSWORD"]
    assert stored.startswith("v4:")
    digest = hashlib.sha256(b"openan-password-v4\0" + s["OC_ADMIN_INITIAL_PASSWORD"].encode()).hexdigest().encode()
    assert bcrypt.checkpw(digest, stored[3:].encode())


def test_retain_data_detaches_pvcs_before_uninstall(monkeypatch):
    from types import SimpleNamespace as NS
    from src import openan_operator as op
    events = []

    class Core:
        def list_namespaced_persistent_volume_claim(self, ns, label_selector):
            events.append(("list", label_selector))
            owned = NS(metadata=NS(name="rc-pvc", owner_references=[object()]))
            free = NS(metadata=NS(name="free-pvc", owner_references=None))
            return NS(items=[owned, free])

        def patch_namespaced_persistent_volume_claim(self, name, ns, body):
            events.append(("patch", name, body))

        def delete_namespaced_persistent_volume_claim(self, name, ns):
            events.append(("delete", name))

    monkeypatch.setattr(op, "_core", lambda: Core())
    monkeypatch.setattr(op.helm_driver, "uninstall", lambda rel, ns: events.append(("uninstall", rel)))
    monkeypatch.setattr(op, "_orphan_secrets", lambda n, ns: events.append(("secrets", n)))
    spec = {"retainData": True, "frontend": {"enabled": False}}
    op.teardown(spec=spec, name="o", namespace="ns", logger=None)
    kinds = [e[0] for e in events]
    assert ("patch", "rc-pvc", {"metadata": {"ownerReferences": None}}) in events
    assert all(e[1] != "free-pvc" for e in events if e[0] == "patch")
    assert "delete" not in kinds and kinds.index("patch") < kinds.index("uninstall")

    events.clear()
    op.teardown(spec={"frontend": {"enabled": False}}, name="o", namespace="ns", logger=None)
    assert ("delete", "rc-pvc") in events and ("delete", "free-pvc") in events and "patch" not in [e[0] for e in events]


def test_existing_detached_secrets_are_readopted_not_regenerated(monkeypatch):
    from types import SimpleNamespace as NS
    from src import openan_operator as op
    patched, created = [], []

    class Core:
        def read_namespaced_secret(self, name, ns):
            return NS(metadata=NS(owner_references=None if name.endswith("token") else [object()]))

        def patch_namespaced_secret(self, name, ns, body):
            patched.append((name, body["metadata"]["ownerReferences"][0]["uid"]))

        def create_namespaced_secret(self, ns, body):
            created.append(body)

    monkeypatch.setattr(op, "_core", lambda: Core())
    op.ensure_secrets("o", "ns", {"metadata": {"name": "o", "uid": "u1"}})
    assert patched == [("o-operator-token", "u1")] and created == []


def test_cr_values_cannot_redirect_credentials():
    evil = {"registryCenter": {"values": {"image": {"tag": "1"}, "runtimeSecret": "kube-root-secret",
                                          "credentialSecret": "other", "trust": {"configMap": "x", "organization": "y"}}},
            "orchestrationCenter": {"values": {"runtimeSecret": "other"}}}
    rc = stack.chart_values("o", evil, "registryCenter")
    assert rc["runtimeSecret"] == "o-registry-runtime" and rc["credentialSecret"] == "o-registry-credentials"
    assert rc["trust"] == {"organization": "openan", "configMap": "o-registry-trust"}
    assert stack.chart_values("o", evil, "orchestrationCenter")["runtimeSecret"] == "o-orchestration-runtime"
    off = {**evil, "cardSync": {"signing": {"enabled": False}}}
    assert "trust" not in stack.chart_values("o", off, "registryCenter")  # no trust mount is injected either


def test_orchestration_center_has_its_own_registry_identity():
    s = secrets_bootstrap.new_stack_secrets("x")
    op, oc = s["operator-token"]["token"], s["orchestration-runtime"]["REGISTRY_ACCESS_TOKEN"]
    assert op != oc
    conf = s["registry-credentials"]["integration_credentials.conf"]
    key = s["registry-runtime"]["INTEGRATION_TOKEN_HMAC_KEY"]
    for token in (op, oc):
        assert secrets_bootstrap.token_fingerprint(token, key) in conf
    assert conf.count("token_hash=") == 2 and "identity=openan-orchestration-center" in conf


def _delete_setup(monkeypatch, existing, org="acme"):
    from src import openan_operator as op
    calls = []

    class RC:
        def get(self, o, n):
            return existing

        def delete(self, o, n):
            calls.append(("delete", o, n))

    monkeypatch.setattr(op, "_registry_for", lambda ns: (RC(), org, {"trust_store": True}))
    monkeypatch.setattr(op, "remove_trust_key", lambda sig, ns, n: calls.append(("trust", n)))
    monkeypatch.setattr(op, "_agent_sync_cr", lambda ns: None)  # agent sync off: no manage-list fallback
    meta = {"annotations": {cards.ANNOTATION: "acme/My-Agent"}, "uid": "u1"}
    return op, calls, meta


def test_delete_requires_registry_confirmed_ownership(monkeypatch):
    mine = {"capabilities": {"extensions": [{"uri": cards.OWNER_EXT, "params": {"owner": "ns/api/u1"}}]}}
    op, calls, meta = _delete_setup(monkeypatch, mine)
    op.remove_card(name="api", meta=meta, namespace="ns", logger=__import__("logging").getLogger())
    assert calls == [("delete", "acme", "My-Agent"), ("trust", "My-Agent")]

    foreign = {"capabilities": {"extensions": [{"uri": cards.OWNER_EXT, "params": {"owner": "ns/other/u9"}}]}}
    op, calls, meta = _delete_setup(monkeypatch, foreign)  # annotation forged to point at somebody else's card
    op.remove_card(name="api", meta=meta, namespace="ns", logger=__import__("logging").getLogger())
    assert calls == []

    op, calls, meta = _delete_setup(monkeypatch, mine, org="other-org")
    op.remove_card(name="api", meta=meta, namespace="ns", logger=__import__("logging").getLogger())
    assert calls == []
    op, calls, meta = _delete_setup(monkeypatch, None)  # already gone: nothing to do
    op.remove_card(name="api", meta=meta, namespace="ns", logger=__import__("logging").getLogger())
    assert calls == []


def test_destination_values_are_pinned_and_dangerous_overrides_rejected():
    evil = {"orchestrationCenter": {"values": {"registryUrl": "http://attacker"}},
            "frontend": {"values": {"backendUrl": "http://attacker", "registryUrl": "http://attacker"}}}
    assert stack.chart_values("o", evil, "orchestrationCenter")["registryUrl"] == "http://o-registry-center-registry-center:5000"
    fe = stack.chart_values("o", evil, "frontend")
    assert "attacker" not in fe["backendUrl"] + fe["registryUrl"]
    with pytest.raises(stack.ValuesError):
        stack.validate_values("orchestrationCenter", {"orchestration": {"extraEnv": {"agent_registry_url": "http://a"}}})
    with pytest.raises(stack.ValuesError):
        stack.validate_values("registryCenter", {"registry": {"extraEnv": {"REGISTRY_SIGNATURE_VALIDATION_ENABLED": "false"}}})
    stack.validate_values("registryCenter", {"registry": {"extraEnv": {"REGISTRY_LOG_LEVEL": "debug"}}})  # harmless keys pass


def test_image_repository_must_come_from_an_allowed_registry():
    stack.validate_values("registryCenter", {"image": {"repository": "ghcr.io/project-openan/registry-center"}})
    with pytest.raises(stack.ValuesError):
        stack.validate_values("registryCenter", {"image": {"repository": "evil.example/registry-center"}})
    stack.validate_values("registryCenter", {"image": {"repository": "reg.corp/openan/registry-center"}}, ("reg.corp/openan/",))


def test_reconcile_rejects_a_bad_cr_before_installing_anything(monkeypatch):
    import kopf
    from src import openan_operator as op
    monkeypatch.setattr(op, "ensure_secrets", lambda *a, **k: (_ for _ in ()).throw(AssertionError("secrets created")))
    monkeypatch.setattr(op.helm_driver, "upgrade_install", lambda *a, **k: (_ for _ in ()).throw(AssertionError("installed")))
    spec = {"registryCenter": {"values": {"image": {"tag": "1", "repository": "evil.example/rc"}}}}
    with pytest.raises(kopf.PermanentError):
        op.reconcile(spec=spec, name="o", namespace="ns", body={"metadata": {}}, patch=None, logger=None)


def test_card_sync_only_trusts_the_oldest_ready_cr():
    def cr(name, ts, ready, sync=True):
        return {"metadata": {"name": name, "creationTimestamp": ts}, "spec": {"cardSync": {"enabled": sync}},
                "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
    items = [cr("evil", "2026-01-02", False), cr("new", "2026-01-03", True), cr("old", "2026-01-01", True),
             cr("off", "2025-01-01", True, sync=False)]
    assert stack.pick_cr(items)["metadata"]["name"] == "old"
    assert stack.pick_cr([cr("evil", "2026-01-02", False)]) is None


def test_registry_auth_and_signature_policy_are_not_cr_controlled():
    spec = {"registryCenter": {"values": {"registry": {"signatureValidation": False, "authMode": "none", "identityMode": "none",
                                                      "publicBaseUrl": "https://x"}}}}
    with pytest.raises(stack.ValuesError):
        stack.validate_values("registryCenter", spec["registryCenter"]["values"])
    rc = stack.chart_values("o", spec, "registryCenter")  # even if validation were skipped, the policy is forced
    assert rc["registry"]["signatureValidation"] is True and "authMode" not in rc["registry"] and "identityMode" not in rc["registry"]
    assert rc["registry"]["publicBaseUrl"] == "https://x"  # unrelated registry values still pass through
    off = stack.chart_values("o", {**spec, "cardSync": {"signing": {"enabled": False}}}, "registryCenter")
    assert off["registry"]["signatureValidation"] is False  # only the CR's own cardSync switch can change it


@pytest.mark.parametrize("env", [{"agent_registry_url": "x"}, {"AGENT_REGISTRY_URL ": "x"}, {"A B": "x"}, {"X: y\n  Z": "v"},
                                 {"1BAD": "x"}, {"OK": {"nested": "x"}}, {"OK": ["list"]}, {"OK": None}])
def test_extra_env_names_and_values_are_strict(env):
    with pytest.raises(stack.ValuesError):
        stack.validate_values("orchestrationCenter", {"orchestration": {"extraEnv": env}})


def test_extra_env_accepts_plain_variables():
    stack.validate_values("orchestrationCenter", {"orchestration": {"extraEnv": {"LOG_LEVEL": "debug", "WORKERS": 4, "FLAG": True}}})
    stack.validate_values("registryCenter", {"registry": {"extraEnv": {"REGISTRY_LOG_LEVEL": "debug"}}})


@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                                  "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "REGISTRY_ENABLE_HTTPS", "REGISTRY_VERIFY_CLIENT",
                                  "ORCH_ENABLE_HTTPS", "PERSISTENCE_MODE", "DB_HOST", "DB_PASSWORD", "LLM_CONFIG_FILE",
                                  "REGISTRY_REGISTRY_SIGN_ENABLED", "REGISTRY_OWNER_ISOLATION_ENABLED", "REGISTRY_JWK_ALLOWLIST",
                                  "INTEGRATION_TOKEN_HMAC_KEY", "REGISTRY_CIPHER_KEY", "REGISTRY_PUBLIC_BASE_URL",
                                  "ORCH_API_TOKEN", "KUBERNETES_SERVICE_HOST", "HELM_DRIVER", "AGENT_REGISTRY_URL"])
def test_extra_env_rejects_security_and_process_variables(name):
    for comp, section, key in (("registryCenter", "registry", "extraEnv"), ("orchestrationCenter", "orchestration", "extraEnv")):
        with pytest.raises(stack.ValuesError):
            stack.validate_values(comp, {section: {key: {name: "x"}}})


@pytest.mark.parametrize("name", ["LOG_LEVEL", "WORKERS", "REGISTRY_LOG_LEVEL", "ORCH_LOG_LEVEL", "TZ", "RETRIEVAL_VECTOR_TOP_K"])
def test_extra_env_still_allows_tuning_variables(name):
    stack.validate_values("registryCenter", {"registry": {"extraEnv": {name: "1"}}})
    stack.validate_values("orchestrationCenter", {"orchestration": {"extraEnv": {name: "1"}}})


# ---- security review: value injection and the 409 race ----------------------------------------------------------
OWNER_A = "ns/a/uid-a"
OWNER_B = "ns/b/uid-b"


def _owned(owner):
    return {**CARD, "capabilities": {"extensions": [{"uri": cards.OWNER_EXT, "params": {"owner": owner}}]}}


class RaceRC(FakeRC):
    """GET says 'missing' first, POST answers 409, later GETs return a card owned by `rival`."""
    def __init__(self, rival):
        super().__init__(post_status=409)
        self.rival, self.gets = rival, 0

    def handler(self, req):
        if req.method == "GET":
            self.calls.append(("GET", req.url.raw_path.decode(), None))
            self.gets += 1
            return httpx.Response(404, json={}) if self.gets == 1 else httpx.Response(200, json=_owned(self.rival))
        return super().handler(req)


def test_upsert_409_race_rechecks_ownership_and_refuses_foreign_card():
    f = RaceRC(OWNER_B)
    with pytest.raises(cards.OwnershipError):
        make(f).upsert(_owned(OWNER_A), owner=OWNER_A)
    assert "PUT" not in [c[0] for c in f.calls]
    f = RaceRC(OWNER_A)  # the winner of the race is us: replacing is fine
    assert make(f).upsert(_owned(OWNER_A), owner=OWNER_A) == "updated"


def test_upsert_refuses_foreign_existing_card_when_owner_given():
    class Existing(FakeRC):
        def handler(self, req):
            if req.method == "GET":
                return httpx.Response(200, json=_owned(OWNER_B))
            return super().handler(req)
    f = Existing()
    with pytest.raises(cards.OwnershipError):
        make(f).upsert(_owned(OWNER_A), owner=OWNER_A)
    assert [c[0] for c in f.calls] == []


@pytest.mark.parametrize("values", [
    {"image": {"tag": 'x"\n  command: [evil]'}}, {"image": {"tag": "a b"}}, {"image": {"tag": ""}},
    {"image": {"tag": "ok", "pullPolicy": "Always\nhostNetwork: true"}},
    {"replicas": "1\nhostNetwork: true"}, {"replicas": True}, {"replicas": -1},
    {"port": "5000, evil: 1"}, {"port": 0}, {"port": 70000}, {"specPort": "8080\n"},
    {"component": {"name": "a\nkind: Evil"}}, {"component": {"version": "1.0 # x"}},
])
def test_validate_values_rejects_injection_prone_scalars(values):
    with pytest.raises(stack.ValuesError):
        stack.validate_values("frontend", values)


def test_validate_values_accepts_normal_scalars():
    stack.validate_values("frontend", {"image": {"tag": "v1.2.3-rc_1", "pullPolicy": "IfNotPresent"}, "replicas": 2,
                                       "port": 8080, "specPort": 8081, "component": {"name": "openan-frontend", "version": "1.0.0"}})


# ---- stuck pending Helm releases -------------------------------------------------------------------------------------

def _helm_history(*revs):
    """`helm history -o json` rows: (revision, status, updated)."""
    import json
    return json.dumps([{"revision": r, "status": st, "updated": ts, "chart": "c-0.1.0"} for r, st, ts in revs])


def _fake_helm(monkeypatch, history_json, calls):
    from src import helm_driver

    def run(args, timeout=300):
        calls.append(args[0])
        if args[0] == "history":
            if history_json is None:
                raise helm_driver.HelmError("Error: release: not found")
            return history_json
        return ""
    monkeypatch.setattr(helm_driver, "_run", run)
    return helm_driver


NOW = None


def _now():
    from datetime import datetime, timezone
    return datetime(2026, 11, 2, 21, 10, 0, tzinfo=timezone.utc)


OLD, FRESH = "2026-11-02T21:06:35.123456789Z", "2026-11-02T21:09:50.5Z"


def test_helm_recover_ignores_healthy_missing_and_young_pending_releases(monkeypatch):
    for hist in (None, _helm_history((1, "deployed", OLD)),
                 _helm_history((1, "deployed", OLD), (2, "pending-upgrade", FRESH))):
        calls = []
        drv = _fake_helm(monkeypatch, hist, calls)
        assert drv.recover_if_stuck("rel", "ns", stale_after=150, now=_now()) is None
        assert calls == ["history"]


def test_helm_recover_rolls_a_stuck_upgrade_back_to_the_last_good_revision(monkeypatch):
    runs = []
    drv = _fake_helm(monkeypatch, _helm_history((1, "superseded", OLD), (2, "deployed", OLD), (3, "pending-upgrade", OLD)), [])
    monkeypatch.setattr(drv, "_run", lambda args, timeout=300: runs.append(args) or (
        _helm_history((1, "superseded", OLD), (2, "deployed", OLD), (3, "pending-upgrade", OLD)) if args[0] == "history" else ""))
    assert drv.recover_if_stuck("rel", "ns", stale_after=150, now=_now()) == "rolled-back"
    assert ["rollback", "rel", "2", "-n", "ns"] in runs and not any(a[0] == "uninstall" for a in runs)


def test_helm_recover_uninstalls_only_a_stuck_first_install(monkeypatch):
    runs = []
    drv = _fake_helm(monkeypatch, None, [])
    hist = _helm_history((1, "pending-install", OLD))
    monkeypatch.setattr(drv, "_run", lambda args, timeout=300: runs.append(args) or (hist if args[0] == "history" else ""))
    assert drv.recover_if_stuck("rel", "ns", stale_after=150, now=_now()) == "uninstalled"
    assert ["uninstall", "rel", "-n", "ns"] in runs and not any(a[0] == "rollback" for a in runs)


def test_helm_recover_never_uninstalls_a_release_that_may_hold_data(monkeypatch):
    # pending upgrade with nothing to roll back to, and a pending install that is not the first revision
    for hist in (_helm_history((1, "failed", OLD), (2, "pending-upgrade", OLD)),
                 _helm_history((1, "failed", OLD), (2, "pending-install", OLD)),
                 _helm_history((1, "failed", OLD), (2, "pending-rollback", OLD))):
        runs = []
        drv = _fake_helm(monkeypatch, hist, [])
        monkeypatch.setattr(drv, "_run", lambda args, timeout=300, hist=hist, runs=runs: runs.append(args) or (
            hist if args[0] == "history" else ""))
        with pytest.raises(drv.HelmError, match="not uninstalling"):
            drv.recover_if_stuck("rel", "ns", stale_after=150, now=_now())
        assert [a[0] for a in runs] == ["history"]


def test_helm_stale_window_never_drops_below_the_helm_timeout():
    from src import helm_driver as drv
    assert drv._stale_after(None) == 150
    assert drv._stale_after("20") == drv.UPGRADE_TIMEOUT + 30
    assert drv._stale_after("not a number") == 150
    assert drv._stale_after("600") == 600


def test_helm_upgrade_install_repairs_before_upgrading_and_reports_it(monkeypatch):
    runs = []
    from src import helm_driver as drv
    hist = _helm_history((1, "deployed", OLD), (2, "pending-upgrade", OLD))
    monkeypatch.setattr(drv, "_run", lambda args, timeout=300: runs.append(args[0]) or (hist if args[0] == "history" else ""))
    monkeypatch.setattr(drv, "STALE_AFTER", 0)
    assert drv.upgrade_install("rel", "/chart", "ns", {"a": 1}) == "rolled-back"
    assert runs == ["history", "rollback", "upgrade"]


def test_helm_parses_nanosecond_timestamps():
    from src import helm_driver as drv
    assert drv._parse_time("2026-11-02T21:51:17.570140138Z").microsecond == 570140
    assert drv._parse_time("2026-11-02T21:09:50.5Z").microsecond == 500000


PROFILE = "https://raw.githubusercontent.com/project-openan/registry-center/v1.2.0/docs/openapi/tmf639-a2a-agent-v5.yaml"


def test_tmf639_profile_url_must_come_from_an_allowed_prefix():
    stack.validate_values("registryCenter", {"tmf639": {"enabled": True, "specificationUrl": PROFILE}})
    for url in ("https://tmf-open-api-table-documents.s3.eu-west-1.amazonaws.com/OpenApiTable/TMF639_Resource_Inventory/"
                "5.0.0/swagger/TMF639-Resource_Inventory_Management-v5.0.0.oas.yaml",
                "https://evil.example/tmf639-a2a-agent-v5.yaml",
                "https://raw.githubusercontent.com/project-openan/registry-center/v1/other.yaml"):
        with pytest.raises(stack.ValuesError, match="tmf639.specificationUrl"):
            stack.validate_values("registryCenter", {"tmf639": {"specificationUrl": url}})
    stack.validate_values("registryCenter", {"tmf639": {"specificationUrl": "https://mirror.corp/rc/tmf639-a2a-agent-v5.yaml"}},
                          allowed_spec_prefixes=("https://mirror.corp/",))


def test_catalog_url_and_http_callbacks_are_not_cr_settings():
    with pytest.raises(stack.ValuesError, match="catalog.specificationUrl"):
        stack.validate_values("registryCenter", {"catalog": {"enabled": True, "specificationUrl": "https://x/y.yaml"}})
    stack.validate_values("registryCenter", {"catalog": {"enabled": True}})
    with pytest.raises(stack.ValuesError, match="allowHttpCallbacks"):
        stack.validate_values("registryCenter", {"broadcast": {"allowHttpCallbacks": True}})


def test_callback_hosts_must_be_allowed_by_the_operator():
    values = {"broadcast": {"enabled": True, "callbackAllowlist": "listener.example, Other.example"}}
    with pytest.raises(stack.ValuesError, match="ALLOWED_CALLBACK_HOSTS"):
        stack.validate_values("registryCenter", values)
    with pytest.raises(stack.ValuesError, match=r"\['other.example'\]"):
        stack.validate_values("registryCenter", values, allowed_callback_hosts=("listener.example",))
    stack.validate_values("registryCenter", values, allowed_callback_hosts=("listener.example", "other.example"))


def test_callback_ca_configmap_must_be_allowed_by_the_operator():
    values = {"broadcast": {"enabled": True, "callbackCaConfigMap": "listener-ca"}}
    with pytest.raises(stack.ValuesError, match="ALLOWED_CALLBACK_CA_CONFIGMAPS"):
        stack.validate_values("registryCenter", values)
    with pytest.raises(stack.ValuesError, match="'listener-ca'"):
        stack.validate_values("registryCenter", values, allowed_callback_ca_configmaps=("other-ca",))
    stack.validate_values("registryCenter", values, allowed_callback_ca_configmaps=("other-ca", "listener-ca"))
    stack.validate_values("registryCenter", {"broadcast": {"callbackCaConfigMap": ""}})



def test_empty_tmf639_profile_url_is_left_to_the_chart():
    # The demo CR template always renders the key; an empty URL only fails in the chart when TMF639 is enabled.
    stack.validate_values("registryCenter", {"tmf639": {"enabled": False, "specificationUrl": ""}})
