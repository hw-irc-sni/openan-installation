# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Render contracts for the OpenAN ODA Component charts (charts/openan-*)."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
CHARTS = {
    "openan-registry-center": {"image.tag": "t", "runtimeSecret": "rt", "credentialSecret": "cred"},
    "openan-orchestration-center": {"image.tag": "t", "runtimeSecret": "rt", "registryUrl": "http://rc:5000"},
    "openan-frontend": {"image.tag": "t", "backendUrl": "http://oc:5001", "registryUrl": "http://rc:5000"},
}


def render(chart, settings=None, success=True):
    if not HELM:
        pytest.skip("helm not on PATH")
    values = {**CHARTS[chart], **(settings or {})}
    cmd = [HELM, "template", "rel", str(ROOT / "charts" / chart), "-n", "components"]
    for k, v in values.items():
        cmd += ["--set", f"{k}={v}"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert (res.returncode == 0) is success, res.stderr
    if not success:
        return res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if d]


@pytest.mark.parametrize("chart", CHARTS)
def test_component_and_children_carry_component_label(chart):
    docs = render(chart)
    comps = [d for d in docs if d["kind"] == "Component"]
    assert len(comps) == 1
    name = comps[0]["metadata"]["name"]
    assert comps[0]["spec"]["componentMetadata"]["name"] in name
    for d in docs:
        if d["kind"] in ("Deployment", "Service", "PersistentVolumeClaim"):
            assert d["metadata"]["labels"]["oda.tmforum.org/componentName"] == name, d["kind"]


@pytest.mark.parametrize("chart", CHARTS)
def test_image_tag_is_required(chart):
    err = render(chart, {"image.tag": ""}, success=False)
    assert "image.tag is required" in err


@pytest.mark.parametrize("chart", ["openan-registry-center", "openan-orchestration-center"])
def test_exposed_api_matches_service_and_has_spec(chart):
    docs = render(chart)
    comp = next(d for d in docs if d["kind"] == "Component")
    api = comp["spec"]["coreFunction"]["exposedAPIs"][0]
    assert api["apiType"] == "openapi"
    svc = next(d for d in docs if d["kind"] == "Service")
    assert api["implementation"] == svc["metadata"]["name"]
    assert api["port"] in [p["port"] for p in svc["spec"]["ports"]]
    assert api["specification"][0]["url"].endswith(".yaml")
    spec_cm = next(d for d in docs if d["metadata"]["name"].endswith("-openapi"))
    assert yaml.safe_load(next(iter(spec_cm["data"].values())))["openapi"].startswith("3.")


def test_registry_requires_secrets():
    assert "runtimeSecret is required" in render("openan-registry-center", {"runtimeSecret": ""}, success=False)
    assert "credentialSecret is required" in render("openan-registry-center", {"credentialSecret": ""}, success=False)


def test_registry_stays_single_replica_recreate():
    dep = next(d for d in render("openan-registry-center") if d["kind"] == "Deployment")
    assert dep["spec"]["replicas"] == 1 and dep["spec"]["strategy"]["type"] == "Recreate"


def test_operator_crd_copy_in_chart_matches_source():
    src = (ROOT / "operator/openan-operator/crd/openan-crd.yaml").read_text()
    assert (ROOT / "charts/openan-operator/crds/openans.yaml").read_text() == src
    crd = yaml.safe_load(src)
    assert crd["spec"]["group"] == "openan.dev" and crd["spec"]["scope"] == "Namespaced"


def _operator_docs(namespaces="components"):
    if not HELM:
        pytest.skip("helm not on PATH")
    out = subprocess.run([HELM, "template", "x", str(ROOT / "charts/openan-operator"), "-n", "canvas",
                          "--set-string", f"deployment.monitoredNamespaces={namespaces}"],
                         capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _seen(doc):
    return {(g, r, v) for rule in doc["rules"] for g in rule["apiGroups"] for r in rule["resources"] for v in rule["verbs"]}


def test_operator_writes_are_namespaced_not_cluster_wide():
    docs = _operator_docs("components\\,odacompns-*")
    cluster = _seen(next(d for d in docs if d["kind"] == "ClusterRole"))
    for resource in ("secrets", "deployments", "persistentvolumeclaims", "configmaps", "components", "exposedapis"):
        assert not any(r == resource and v in ("create", "update", "patch", "delete") for _, r, v in cluster), resource
    roles = [d for d in docs if d["kind"] == "Role"]
    assert [r["metadata"]["namespace"] for r in roles] == ["components"]  # the wildcard gets no grant
    seen = _seen(roles[0])
    for need in [("openan.dev", "openans/status", "patch"), ("oda.tmforum.org", "exposedapis", "patch"),
                 ("", "secrets", "create"), ("apps", "deployments", "delete"), ("oda.tmforum.org", "components", "create")]:
        assert need in seen, need
    binding = next(d for d in docs if d["kind"] == "RoleBinding")
    assert binding["metadata"]["namespace"] == "components" and binding["subjects"][0]["namespace"] == "canvas"


@pytest.mark.parametrize("chart,section", [("openan-registry-center", "registry"), ("openan-orchestration-center", "orchestration")])
def test_extra_env_keys_are_quoted_in_the_configmap(chart, section, tmp_path):
    """A hostile key must stay one literal ConfigMap key and never inject YAML (defense in depth)."""
    if not HELM:
        pytest.skip("helm not on PATH")
    values = tmp_path / "v.yaml"
    values.write_text(yaml.safe_dump({section: {"extraEnv": {"EVIL: injected\nOTHER": "v"}}}))
    cmd = [HELM, "template", "rel", str(ROOT / "charts" / chart), "-n", "components", "-f", str(values)]
    for k, v in CHARTS[chart].items():
        cmd += ["--set", f"{k}={v}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    cm = next(d for d in yaml.safe_load_all(out) if d and d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config"))
    assert cm["data"]["EVIL: injected\nOTHER"] == "v" and "OTHER" not in cm["data"]


@pytest.mark.parametrize("chart,comp,extra_key", [("openan-registry-center", "registryCenter", "registry"),
                                                  ("openan-orchestration-center", "orchestrationCenter", "orchestration")])
def test_generated_env_list_matches_the_templates_and_wins_over_extra_env(chart, comp, extra_key, tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "operator/openan-operator"))
    from src import stack
    values = tmp_path / "v.yaml"
    values.write_text(yaml.safe_dump({extra_key: {"extraEnv": {k: "attacker" for k in sorted(stack.GENERATED_ENV[comp])}}}))
    cmd = [HELM, "template", "rel", str(ROOT / "charts" / chart), "-n", "components", "-f", str(values)]
    for k, v in CHARTS[chart].items():
        cmd += ["--set", f"{k}={v}"]
    docs = [d for d in yaml.safe_load_all(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-config"))
    assert set(cm["data"]) == set(stack.GENERATED_ENV[comp])  # list in the operator == keys the template generates
    assert "attacker" not in cm["data"].values()  # generated values win over every colliding extraEnv entry



RUNTIME_CHARTS = ["openan-registry-center", "openan-orchestration-center"]


def _runtime_pod(chart, settings=None):
    docs = render(chart, settings)
    return next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]


@pytest.mark.parametrize("chart", RUNTIME_CHARTS)
@pytest.mark.parametrize("uid,gid", [(10001, 10001), (1000, 1000), (1000, 2000)])
def test_prepare_runtime_runs_as_root_with_minimal_caps_and_chowns_to_pod_user(chart, uid, gid):
    # A hostPath/local PV root is root:root 0755 and ignores fsGroup, so the app user cannot create
    # data/ and log/ itself (mirrors upstream a27e74b); the owner must follow podSecurityContext.
    pod = _runtime_pod(chart, {"podSecurityContext.runAsUser": uid, "podSecurityContext.runAsGroup": gid,
                               "podSecurityContext.runAsNonRoot": "true"})
    assert (pod["securityContext"]["runAsUser"], pod["securityContext"]["runAsGroup"]) == (uid, gid)
    init = pod["initContainers"][0]
    assert init["name"] == "prepare-runtime" and init["image"] == pod["containers"][0]["image"]
    assert init["command"][:2] == ["/bin/sh", "-c"]
    sc = init["securityContext"]
    # runAsNonRoot: false overrides a pod-level runAsNonRoot: true, which would otherwise block the init.
    assert sc["runAsUser"] == 0 and sc["runAsNonRoot"] is False and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"]}
    for c in pod["containers"]:  # app containers inherit the pod user; none may override it
        assert "runAsUser" not in c.get("securityContext", {}), c["name"]
    script = init["command"][2]
    assert f"chown {uid}:{gid} /runtime/data /runtime/log" in script and f"chgrp {gid} /runtime" in script
    assert f"os.setgid({gid}), os.setuid({uid})" in script


def _run_prepare_runtime(chart, tmp_path, path_dirs=()):
    # Render for the current uid/gid so the ownership commands also run (and must succeed) on non-root CI.
    uid, gid = (os.getuid(), os.getgid()) if os.getuid() else (10001, 10001)
    script = _runtime_pod(chart, {"podSecurityContext.runAsUser": uid, "podSecurityContext.runAsGroup": gid}
                          )["initContainers"][0]["command"][2]
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = dict(os.environ, PATH=os.pathsep.join([*map(str, path_dirs), os.environ["PATH"]]))
    res = subprocess.run(["/bin/sh", "-c", script.replace("/runtime", str(runtime))],
                         capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 0, res.stderr
    for sub in ("data", "log"):
        st = (runtime / sub).stat()
        assert (st.st_uid, st.st_gid) == (uid, gid) and st.st_mode & 0o777 == 0o770
    return runtime, res


@pytest.mark.parametrize("chart", RUNTIME_CHARTS)
def test_prepare_runtime_script_sets_ownership_and_modes(chart, tmp_path):
    runtime, res = _run_prepare_runtime(chart, tmp_path)
    assert runtime.stat().st_mode & 0o7777 == 0o1770 and "owned by" in res.stdout


@pytest.mark.parametrize("chart", RUNTIME_CHARTS)
def test_prepare_runtime_falls_back_to_the_pod_user_when_ownership_changes_are_refused(chart, tmp_path):
    # NFS root_squash: root cannot chown, so the init must not crash-loop but create the dirs as the pod user.
    if not shutil.which("python"):
        pytest.skip("python not on PATH")
    stub = tmp_path / "bin"
    stub.mkdir()
    for tool in ("chown", "chgrp"):
        (stub / tool).write_text("#!/bin/sh\nexit 1\n")
        (stub / tool).chmod(0o755)
    runtime, res = _run_prepare_runtime(chart, tmp_path, [stub])
    assert runtime.stat().st_mode & 0o7777 != 0o1770  # the root step stopped before touching anything
    assert "cannot set ownership as root" in res.stderr


@pytest.mark.parametrize("chart", RUNTIME_CHARTS)
@pytest.mark.parametrize("settings", [{"persistence.prepareAsRoot": "false"}, {"podSecurityContext": "null"}])
def test_prepare_runtime_stays_non_root_when_disabled_or_without_a_pod_user(chart, settings):
    # Pod Security "restricted" (fsGroup-capable storage) or images that rely on their own USER.
    init = _runtime_pod(chart, settings)["initContainers"][0]
    assert init["command"][:2] == ["python", "-c"]
    assert init["securityContext"] == {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


@pytest.mark.parametrize("chart", RUNTIME_CHARTS)
@pytest.mark.parametrize("key,bad", [("runAsUser", "0"), ("runAsUser", "abc"), ("runAsUser", "-1"), ("runAsGroup", "0")])
def test_prepare_runtime_rejects_non_positive_pod_user(chart, key, bad):
    err = render(chart, {f"podSecurityContext.{key}": bad}, success=False)
    assert "must be positive integers" in err
