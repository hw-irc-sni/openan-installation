"""Add or remove one MCP target in agentgateway's live config (ConfigMap agentgateway-config, key config.yaml), as the
Canvas Portal's AI Gateways page does. Prints "changed" or "unchanged".

  agentgateway-mcp-target.py <namespace> add <name> <url> [<key file>]
  agentgateway-mcp-target.py <namespace> remove <name>

<key file> is a path inside the gateway pod (e.g. /llm-keys/<key>): sent upstream as `Authorization: Bearer <content>`.
The gateway expands environment variables across the whole file, so a value with `$` is refused.
"""
import json
import subprocess
import sys

import yaml


class NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def kubectl(*args, stdin=None):
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True, check=True).stdout


ns, action, name, *rest = sys.argv[1:]
cm = json.loads(kubectl("-n", ns, "get", "configmap", "agentgateway-config", "-o", "json"))
doc = yaml.safe_load(cm["data"]["config.yaml"]) or {}
mcp = doc.get("mcp")
if not isinstance(mcp, dict):
    sys.exit("agentgateway config has no mcp section")
targets = mcp.setdefault("targets", [])
others = [t for t in targets if not (isinstance(t, dict) and t.get("name") == name)]

if action == "add":
    url, *key_file = rest
    entry = {"name": name, "mcp": {"host": url}}
    if key_file:
        entry["policies"] = {"backendAuth": {"key": {"file": key_file[0]}}}
    if "$" in json.dumps(entry):
        sys.exit("refused: '$' in the target")
    new = others + [entry]
    current = [t for t in targets if isinstance(t, dict) and t.get("name") == name]
    if current == [entry]:
        new = targets  # already registered as is: keep the order
elif action == "remove":
    new = others
else:
    sys.exit(f"unknown action {action}")

if new == targets:
    print("unchanged")
    sys.exit(0)
mcp["targets"] = new
text = yaml.dump(doc, Dumper=NoAliases, sort_keys=False)
kubectl("-n", ns, "patch", "configmap", "agentgateway-config", "--type", "merge",
        "-p", json.dumps({"data": {"config.yaml": text}}))
print("changed")
