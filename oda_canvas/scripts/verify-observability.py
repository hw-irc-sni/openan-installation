"""Run inside the portal backend pod: log in and require Langfuse, Grafana and Prometheus to be healthy
as reported by GET /api/observability (what the portal's /observability page shows)."""
import json, os, sys, time, urllib.parse, urllib.request

base = "http://localhost:8080"
req = urllib.request.Request(base + "/auth/login", data=json.dumps(
    {"username": os.environ["PU"], "password": os.environ["PP"]}).encode(),
    headers={"Content-Type": "application/json"})
token = json.load(urllib.request.urlopen(req, timeout=15))["access_token"]
want = {"langfuse", "grafana", "prometheus"}
for attempt in range(30):  # the health checks run from the backend; allow the pods a moment to settle
    r = urllib.request.Request(base + "/api/observability", headers={"Authorization": "Bearer " + token})
    tools = {t["id"]: t for t in json.load(urllib.request.urlopen(r, timeout=30))["tools"]}
    bad = [i for i in want if tools.get(i, {}).get("status") != "healthy"]
    if not bad:
        break
    time.sleep(5)
for i in sorted(want):
    t = tools.get(i, {})
    print(f"{i:11} {t.get('status')}  {t.get('internalUrl')}  {t.get('healthMessage')}")
sys.exit(1 if bad else 0)
