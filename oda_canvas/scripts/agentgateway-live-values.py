"""Turn agentgateway's live config.yaml (stdin) into a Helm values file (stdout) that keeps the models and MCP servers
the Canvas Portal (AI Gateways page) wrote to the ConfigMap. Without it, `helm upgrade` puts back the chart's lists.

A section the live config does not have is set to null, which removes it from the chart's values too.
"""
import json
import sys

import yaml

live = yaml.safe_load(sys.stdin) or {}
config = {}
for section, key in (("llm", "models"), ("mcp", "targets")):
    if isinstance(live.get(section), dict):
        config[section] = {key: live[section].get(key) or []}
    else:
        config[section] = None
json.dump({"agentgateway-standalone": {"config": config}}, sys.stdout, indent=2)
print()
