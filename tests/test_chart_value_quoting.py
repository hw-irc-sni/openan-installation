"""Chart scalars from values must be quoted/typed in the rendered manifests: no YAML injection through values."""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHARTS = {
    "openan-registry-center": ["--set", "runtimeSecret=a", "--set", "credentialSecret=b"],
    "openan-orchestration-center": ["--set", "runtimeSecret=a", "--set", "registryUrl=http://rc:5000"],
    "openan-frontend": ["--set", "backendUrl=http://oc:5001", "--set", "registryUrl=http://rc:5000"],
}
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not on PATH")


def template(chart, *extra):
    return subprocess.run(["helm", "template", "t", str(ROOT / "charts" / chart), "--set", "image.tag=ci", *CHARTS[chart], *extra],
                          capture_output=True, text=True)


@pytest.mark.parametrize("chart", CHARTS)
def test_normal_values_render_valid_yaml(chart):
    r = template(chart)
    assert r.returncode == 0, r.stderr
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    dep = next(d for d in docs if d.get("kind") == "Deployment")
    assert all(c["image"].endswith(":ci") for c in dep["spec"]["template"]["spec"]["containers"])


@pytest.mark.parametrize("chart", CHARTS)
@pytest.mark.parametrize("key", ["image.tag", "image.pullPolicy", "replicas", "port", "specPort", "component.name", "component.version"])
def test_injected_value_never_adds_yaml_structure(chart, key):
    payload = "x\n        hostNetwork: true\n        injected: yes"
    r = template(chart, "--set-string", f"{key}={payload}")
    out = r.stdout if r.returncode == 0 else ""
    assert "hostNetwork" not in [k for d in yaml.safe_load_all(out) if d for k in _keys(d)]


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


@pytest.mark.parametrize("key", ["tmf639.specificationUrl", "catalog.specificationUrl", "broadcast.callbackAllowlist",
                                 "broadcast.callbackCaConfigMap", "broadcast.callbackCaKey",
                                 "component.id", "component.functionalBlock"])
def test_tmf639_values_never_add_yaml_structure(key):
    payload = "x\n        hostNetwork: true\n        injected: yes"
    r = template("openan-registry-center", "--set", "tmf639.enabled=true", "--set", "tmf639.specificationUrl=https://p/tmf639-a2a-agent-v5.yaml",
                 "--set", "catalog.enabled=true", "--set", "broadcast.enabled=true",
                 "--set", "broadcast.callbackAllowlist=l.example", "--set-string", f"{key}={payload}")
    out = r.stdout if r.returncode == 0 else ""
    assert "hostNetwork" not in [k for d in yaml.safe_load_all(out) if d for k in _keys(d)]


def test_tmf639_quoting_baseline_renders():
    """Guards the test above: with a valid profile URL the enabled chart must render at all."""
    r = template("openan-registry-center", "--set", "tmf639.enabled=true",
                 "--set", "tmf639.specificationUrl=https://p/tmf639-a2a-agent-v5.yaml")
    assert r.returncode == 0, r.stderr


def test_callback_ca_quoting_baseline_renders():
    """Guards the callback CA cases above: valid values must render (the injected ones fail validation)."""
    r = template("openan-registry-center", "--set", "broadcast.enabled=true", "--set", "broadcast.callbackAllowlist=l.example",
                 "--set", "broadcast.callbackCaConfigMap=listener-ca", "--set", "broadcast.callbackCaKey=ca.crt")
    assert r.returncode == 0, r.stderr
