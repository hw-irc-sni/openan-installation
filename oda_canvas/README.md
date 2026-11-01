# ODA Canvas deployment automation

Automates a development / quick-demo ODA Canvas environment on a local **kind** cluster.
Stages run in this order (`make up`):

1. **kind cluster** (`kind-config.yaml`, cluster `oda-canvas`, Kubernetes 1.30: the Canvas supports <= 1.30).
2. **Istio** (Helm, `ISTIO_VERSION`) and a patched `istio-ingress` address (kind has no LoadBalancer).
3. **ODA Canvas** (`charts/canvas-oda` from `CANVAS_SRC`, values in `canvas-values.yaml.tpl`): CRDs,
   operators, cert-manager, Vault and the **bundled Keycloak** (realm `odari`, service
   `canvas-keycloak:8083`, admin user `admin`). Demo profile: Istio gateway only; info-service,
   resource-inventory, pdb and component-registry are off. Installed
   without `--wait` (the post-install hooks would deadlock); the Makefile waits afterwards.
4. **Portal realm setup** (`scripts/portal-realm.sh`): adds the `canvas-portal` / `canvas-portal-admin`
   clients, the `canvas-admin` group and the demo user to the Canvas realm (Keycloak partial import,
   idempotent).
5. **canvas-portal-v2**, from `tmforum-oda/oda-canvas/canvas-portal-v2/helm/canvas-portal` with
   `portal-values.yaml.tpl`, pointed at the Canvas Keycloak.

The Canvas must be in the namespace `canvas`: the portal detects the gateway type there. The
`/model-gateway-services` page stays "not available": its CRD is not part of `charts/oda-crds`.

## Usage

Prerequisites: docker, kind, kubectl, helm, envsubst (gettext), python3, curl, make. About 4 CPU / 16 GB RAM.

```bash
cd oda_canvas
make up              # cluster + istio + canvas + portal (slow: many images)
make port-forward    # http://localhost:8080 (Ctrl-C to stop)
make component-sample   # optional: gives /components and /exposedapis data
make status
make down            # delete the cluster
```

Login: `portaladmin` / the `PORTAL_USER_PASSWORD` from `config.env` (members of `canvas-admin` may use the Helm and
conformance APIs). Settings are in `config.mk`; passwords and secrets are in `config.env` (untracked: `cp config.env.example config.env` first); override per run,
e.g. `make up PORTAL_BUILD=false` to pull
`tmforumodacanvas/canvas-portal:{frontend,backend}-0.1.0` instead of building the images from `PORTAL_SRC` (the default, as those tags were not found on Docker Hub).

## Design notes

- Portal Services are `ClusterIP` (the chart has no nodePort setting), so access is by `port-forward`.
  `CORS_ORIGINS` is set for `http://localhost:$LOCAL_PORT`.
- Backend and the nginx proxy reach Keycloak at the in-cluster URL
  `http://canvas-keycloak.canvas.svc.cluster.local:8083/auth`; login goes through the backend, so the token
  issuer matches what the backend validates.
- `cookieSecure` stays `false` (plain HTTP). Credentials are demo-only; do not reuse outside a local kind cluster.
- `.rendered/` holds generated values and is gitignored.

## Network / proxy troubleshooting

Symptom: `failed to fetch anonymous token ... dial tcp ...:443: connect: no route to host` during
`make images`. Image pulls and builds are done by the Docker daemon, which ignores your shell's `http_proxy`.

1. `docker info | grep -i proxy` -- if empty while your shell uses a proxy, give the daemon the proxy:

   ```bash
   sudo mkdir -p /etc/systemd/system/docker.service.d
   sudo tee /etc/systemd/system/docker.service.d/proxy.conf <<EOT
   [Service]
   Environment="HTTP_PROXY=http://<proxy>:3128" "HTTPS_PROXY=http://<proxy>:3128" "NO_PROXY=localhost,127.0.0.1,10.0.0.0/8"
   EOT
   sudo systemctl daemon-reload && sudo systemctl restart docker
   ```

2. If the daemon already shows the proxy, the failure can be a transient route/DNS problem to Docker Hub:
   rerun `make up` (it is idempotent) or pre-pull the base images while the route works:
   `node:20-alpine`, `nginx:1.27-alpine`, `python:3.12-slim`, `python:3.12-alpine`. `make` reuses local
   images. Canvas images (incl. `bitnamilegacy/*`) are pulled by `scripts/preload-images.sh`.

The kind node pulls nothing from the internet: every image is `kind load`ed.
