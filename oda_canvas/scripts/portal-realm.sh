#!/usr/bin/env bash
# Add the canvas-portal clients, canvas-admin group and demo user (from manifests/realm-odari.json)
# to the Canvas Keycloak realm through the admin REST API. Idempotent (409 = already exists).
# (Partial import is avoided: Keycloak 20 throws a NullPointerException on groups.)
set -euo pipefail
: "${CANVAS_NS:?}" "${KEYCLOAK_PORT:?}" "${KC_REALM:?}" "${KC_ADMIN_USER:?}" "${KC_ADMIN_PASSWORD:?}"
lp=18083
kubectl -n "$CANVAS_NS" port-forward svc/canvas-keycloak "$lp:$KEYCLOAK_PORT" >/dev/null &
pf=$!; trap 'kill $pf 2>/dev/null' EXIT
base=http://127.0.0.1:$lp/auth
for _ in $(seq 30); do curl -sf "$base/realms/master" >/dev/null && break; sleep 2; done
envsubst < manifests/realm-odari.json | BASE="$base" python3 -I -c '
import sys, os, json, urllib.request, urllib.parse, urllib.error
base, realm = os.environ["BASE"], os.environ["KC_REALM"]
spec = json.load(sys.stdin)
def call(method, path, body=None, form=None, token=None):
    data = urllib.parse.urlencode(form).encode() if form else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        if e.code == 409: return 409, e.headers, b""
        sys.exit(f"{method} {path}: HTTP {e.code} {e.read()[:300]}")
_, _, b = call("POST", "/realms/master/protocol/openid-connect/token", form={
    "client_id": "admin-cli", "grant_type": "password",
    "username": os.environ["KC_ADMIN_USER"], "password": os.environ["KC_ADMIN_PASSWORD"]})
tok = json.loads(b)["access_token"]
adm = f"/admin/realms/{realm}"
def get(path): return json.loads(call("GET", adm + path, token=tok)[2])
for g in spec["groups"]:
    call("POST", adm + "/groups", g, token=tok)
for c in spec["clients"]:
    print("client", c["clientId"], call("POST", adm + "/clients", c, token=tok)[0])
groups = {g["name"]: g["id"] for g in get("/groups")}
for u in spec["users"]:
    creds, ugroups = u.pop("credentials"), u.pop("groups")
    print("user", u["username"], call("POST", adm + "/users", u, token=tok)[0])
    uid = get("/users?exact=true&username=" + urllib.parse.quote(u["username"]))[0]["id"]
    for cr in creds:
        call("PUT", f"{adm}/users/{uid}/reset-password", cr, token=tok)
    for g in ugroups:
        call("PUT", f"{adm}/users/{uid}/groups/{groups[g]}", token=tok)
print("portal realm objects ready")
'
