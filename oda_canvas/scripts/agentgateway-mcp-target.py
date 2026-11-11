"""Add or remove one MCP target in agentgateway's live config (ConfigMap agentgateway-config, key config.yaml), as the
Canvas Portal's AI Gateways page does. Prints "changed" or "unchanged".

  agentgateway-mcp-target.py <namespace> add <name> <url> [--key-file <path>] [--allow-tools <tool,tool,...>]
  agentgateway-mcp-target.py <namespace> remove <name>

--key-file: a path inside the gateway pod (e.g. /llm-keys/<key>), sent upstream as `Authorization: Bearer <content>`.
--allow-tools: only these tools of the target are listed and callable through the gateway; without it, all of them.

agentgateway v1.7.0 only has mcpAuthorization for the whole target set (mcp.policies), not per target. Its rules are
OR'ed "allow" CEL expressions checked for each tool, prompt, resource and task, and once there is any rule everything
else is denied. So a restriction is one rule per kind that allows every *other* target, plus one that allows the listed
tools of this target (a rule that reads a field the item does not have evaluates to false, hence one rule per kind).
These rules name the target, which is how this script finds them again. OR'ed rules cannot restrict a second target
independently, so the script refuses to add rules next to rules it did not write.
The gateway expands environment variables across the whole file, so a value with `$` is refused.
"""
import argparse
import json
import subprocess
import sys

import yaml


class NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def kubectl(*args):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=True).stdout


def restriction_rules(name, tools):
    q = json.dumps(name)
    return [{"allow": f"mcp.{kind}.target != {q}"} for kind in ("tool", "prompt", "resource", "task")] + [
        {"allow": f"mcp.tool.target == {q} && mcp.tool.name in {json.dumps(tools)}"}
    ]


def owned(rule, name):
    exprs = [rule] if isinstance(rule, str) else [v for v in rule.values() if isinstance(v, str)] if isinstance(rule, dict) else []
    return any(json.dumps(name) in e for e in exprs)


p = argparse.ArgumentParser()
p.add_argument("namespace")
p.add_argument("action", choices=["add", "remove"])
p.add_argument("name")
p.add_argument("url", nargs="?")
p.add_argument("--key-file")
p.add_argument("--allow-tools", default="")
a = p.parse_args()

cm = json.loads(kubectl("-n", a.namespace, "get", "configmap", "agentgateway-config", "-o", "json"))
doc = yaml.safe_load(cm["data"]["config.yaml"]) or {}
before = json.dumps(doc, sort_keys=True)
mcp = doc.get("mcp")
if not isinstance(mcp, dict):
    sys.exit("agentgateway config has no mcp section")

# Target entry
targets = mcp.get("targets") or []
current = [t for t in targets if isinstance(t, dict) and t.get("name") == a.name]
others = [t for t in targets if t not in current]
if a.action == "add":
    if not a.url:
        sys.exit("add needs a url")
    entry = {"name": a.name, "mcp": {"host": a.url}}
    if a.key_file:
        entry["policies"] = {"backendAuth": {"key": {"file": a.key_file}}}
    if current != [entry]:
        mcp["targets"] = others + [entry]
else:
    mcp["targets"] = others

# Tool restriction (mcp.policies.mcpAuthorization.rules)
policies = mcp.get("policies") if isinstance(mcp.get("policies"), dict) else {}
authz = policies.get("mcpAuthorization") if isinstance(policies.get("mcpAuthorization"), dict) else {}
rules = authz.get("rules") or []
foreign = [r for r in rules if not owned(r, a.name)]
tools = [t for t in a.allow_tools.split(",") if t]
wanted = restriction_rules(a.name, tools) if a.action == "add" and tools else []
if wanted and foreign:
    sys.exit(f"refused: mcp.policies.mcpAuthorization already has other rules, which would be OR'ed with these: {foreign}")
new_rules = foreign + wanted
if new_rules != rules:
    authz["rules"] = new_rules
    if not new_rules:
        del authz["rules"]
    policies["mcpAuthorization"] = authz
    if not authz:
        del policies["mcpAuthorization"]
    mcp["policies"] = policies
    if not policies:
        del mcp["policies"]

if "$" in json.dumps({"targets": mcp.get("targets"), "rules": new_rules}):
    sys.exit("refused: '$' in the target or rules")
if json.dumps(doc, sort_keys=True) == before:
    print("unchanged")
    sys.exit(0)
text = yaml.dump(doc, Dumper=NoAliases, sort_keys=False)
kubectl("-n", a.namespace, "patch", "configmap", "agentgateway-config", "--type", "merge",
        "-p", json.dumps({"data": {"config.yaml": text}}))
print("changed")
