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
