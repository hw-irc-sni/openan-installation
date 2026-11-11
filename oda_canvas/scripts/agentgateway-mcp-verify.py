"""Check one MCP target through agentgateway: list the tools on the gateway's /mcp and call one of the target's.

Runs inside a pod with python3 and no extra packages (the gateway image has no shell):
  kubectl exec -i <pod> -- python3 - <gateway mcp url> <target name> <tool> [<other target>|!<tool>...] < agentgateway-mcp-verify.py

The tool is called with no arguments. Each <other target> must still have tools listed: with agentgateway's default
failureMode, one unreachable target fails every MCP request through the gateway. Each !<tool> of the target must be
hidden by the gateway's authorization: not listed, and a call (no arguments) refused by the gateway before it reaches the server
(HTTP 400 with a JSON-RPC error, "unknown resource": the gateway treats it as not existing). Retries for about a minute.
"""
import json
import sys
import time
import urllib.error
import urllib.request

url, target, tool, *rest = sys.argv[1:]
others = [r for r in rest if not r.startswith("!")]
hidden = [r[1:] for r in rest if r.startswith("!")]


class RpcError(Exception):
    pass


def post(body, session=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        session = resp.headers.get("Mcp-Session-Id") or session
        text = resp.read().decode()
    if "id" not in body:
        return None, session
    for line in text.splitlines() if not text.lstrip().startswith("{") else [text]:
        data = line[5:].strip() if line.startswith("data:") else line
        if data.startswith("{"):
            msg = json.loads(data)
            if msg.get("id") == body["id"]:
                if "error" in msg:
                    raise RpcError(msg["error"])
                return msg["result"], session
    raise RuntimeError(f"no response to {body['method']}: {text[:300]}")


def check():
    init = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "oda-canvas-verify", "version": "1"}}
    _, session = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    names = [t["name"] for t in post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)[0]["tools"]]
    # With several targets, agentgateway names each tool <target>_<tool>.
    mine = [n for n in names if n.startswith(f"{target}_")]
    missing = [o for o in others if not any(n.startswith(f"{o}_") for n in names)]
    if not mine or missing:
        raise RuntimeError(f"{len(mine)} {target} tools, missing targets {missing}; listed: {names}")
    listed = [h for h in hidden if f"{target}_{h}" in names]
    if listed:
        raise RuntimeError(f"listed although not allowed: {listed}")
    for h in hidden:
        try:
            result, _ = post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": f"{target}_{h}", "arguments": {}}}, session)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                error = json.loads(body).get("error")
            except ValueError:
                error = None
            if exc.code == 400 and error:
                print(f"{target}_{h}: refused by the gateway ({error.get('message')})")
                continue
            raise
        raise RuntimeError(f"{target}_{h} reached the server although not allowed: {result}")
    result, _ = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": f"{target}_{tool}", "arguments": {}}}, session)
    if result.get("isError"):
        raise RuntimeError(f"{target}_{tool} failed: {result}")
    text = " ".join(c.get("text", "") for c in result.get("content", []))
    others_text = f"; {', '.join(others)} still listed" if others else ""
    print(f"{len(mine)} {target} tools through {url}{others_text}")
    print(f"{target}_{tool}: {text[:400]}")


for attempt in range(1, 13):
    try:
        check()
        sys.exit(0)
    except Exception as exc:  # the gateway may still be reloading or restarting
        print(f"attempt {attempt}: {exc}", file=sys.stderr)
        time.sleep(5)
sys.exit(1)
