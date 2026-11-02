# ODA Canvas deployment automation

Automates a development / quick-demo ODA Canvas environment on a local **kind** cluster. The deployment is split
into stages that follow the Canvas `installation/README.md`, plus the portal and a sample component on top:

| Target | What it does (Canvas installation README) |
|---|---|
| `make environment` | **Environment installation 1.-4.** Pre-flight checks (`scripts/preflight.sh`: docker, kind, kubectl, helm >= `HELM_MIN`, envsubst, python3, curl, git, openssl, Docker daemon, Canvas checkout and its git revision); kind cluster (`kind-config.yaml.tpl`, Kubernetes 1.30: the Canvas supports <= 1.30); Helm repositories and chart dependencies; **Istio** (Helm, `ISTIO_VERSION`) with the `istio-ingress` address patched (kind has no LoadBalancer); Gateway API CRDs; **Vault prerequisites**; Kubernetes permission check (`kubectl auth can-i`, as `installation/precheck.sh`). |
| `make oda-canvas` | **Reference implementation (5.)**: `charts/canvas-oda` from `CANVAS_SRC` with `canvas-values.yaml.tpl`: CRDs, operators, cert-manager, **Vault** (namespace `canvas-vault`) and the **bundled Keycloak** (realm `odari`, service `canvas-keycloak:8083`, admin user `admin`). Demo profile: Istio gateway only; info-service, resource-inventory, pdb and component-registry are off. Installed **without `--wait`** (the post-install hooks would deadlock); the Makefile waits afterwards and runs `canvas-verify`. |
| `make observability` | The Canvas **`charts/observability-stack`** (Prometheus + Grafana via kube-prometheus-stack, OpenTelemetry collector, Jaeger) as release `monitoring` in namespace `monitoring`, plus **Langfuse** v3 (`manifests/langfuse.yaml`: web, worker, Postgres, ClickHouse, Redis, MinIO). |
| `make canvas-portal` | **canvas-portal-v2** on top of the Canvas Keycloak (below): portal images, realm setup, Helm release, health checks. Needs the Canvas (`oda-canvas`) and, for its `/observability` page, the observability stack. |
| `make component-sample` | The TM Forum reference Product Catalog component (release `pc` in `components`); fails unless every component reaches `Complete`. |
| `make bootstrap-oda-canvas` | `environment`, `oda-canvas`, `observability`, `canvas-portal`, `component-sample`, one after the other. |

`make canvas-portal` does:

1. **Portal images**, built from `PORTAL_SRC` (the published tags do not exist) and `kind load`ed.
2. **Portal realm setup** (`scripts/portal-realm.sh`): adds the `canvas-portal` / `canvas-portal-admin`
   clients, the `canvas-admin` group and the demo user to the Canvas realm (Keycloak admin REST API, idempotent).
3. **canvas-portal-v2**, from `tmforum-oda/oda-canvas/canvas-portal-v2/helm/canvas-portal` with
   `portal-values.yaml.tpl`, pointed at the Canvas Keycloak.
4. **Health checks** (`make verify`): backend `/health`, and the portal must see Langfuse, Grafana and Prometheus as healthy.

The Canvas must be in the namespace `canvas`: the portal detects the gateway type there. The
`/model-gateway-services` page stays "not available": its CRD is not part of `charts/oda-crds`.

### Environment stage

- **Host tools are checked, never installed.** A missing tool is reported with an install hint; nothing is
  downloaded and no `sudo` is used. Helm >= 3.16 is required (the Makefile uses `--skip-schema-validation`); Helm 4 is
  accepted (the Canvas documents Helm 3, this deployment is exercised with Helm 4; `--force-conflicts` is only passed to Helm 4).
- **Dependencies**: `helm repo add` for istio, bitnami, hashicorp, jetstack, kong, apiseven, prometheus-community,
  open-telemetry and jaegertracing. Kong and APISIX are not installed by the demo, but the umbrella chart cannot be
  packaged without their dependencies. Dependencies are built only when a chart reports a non-`ok` status
  (`helm dependency build`, which honours `Chart.lock` and keeps the Canvas checkout clean; `update` is the fallback).
- **Vault** is a sub-chart of `canvas-oda`, so `make oda-canvas` installs it (README 4. describes it as part of the
  Canvas). `environment` only makes sure it can be installed: hashicorp repo, dependencies, and a warning if
  `canvas-values.yaml.tpl` disables `canvas-vault` (components requesting Secrets-Management would then stay
  `InProgress-SecretsConfig`).
- **Reproducibility**: the pre-flight prints the `CANVAS_SRC` git revision (and chart version). Set `CANVAS_REF` in
  `config.mk` to fail when the checkout is at another revision.

### Observability stage

- The release is named `monitoring` and the Prometheus sub-chart gets `fullnameOverride: monitoring-kube-prometheus`,
  so the Services are `monitoring-grafana`, `monitoring-kube-prometheus-prometheus` and `langfuse-web` in `monitoring`:
  the three tools on the portal's `/observability` page, which finds them by name. Overrides are in
  `observability-values.yaml.tpl`: no Alertmanager/node-exporter, no scrape of the kind control-plane components, 7d
  retention, Grafana admin password, Jaeger pinned to `JAEGER_IMAGE_TAG` (chart default `latest`). The chart already
  provides cross-namespace ServiceMonitor discovery and the containerd cAdvisor relabelings.
- The Canvas `api-operator-istio` creates `ServiceMonitor`s (`openMetricsImplementation: ServiceMonitor`), so Prometheus
  scrapes the metrics of every deployed component.
- **Tracing**: components can send OTLP/HTTP to
  `http://monitoring-opentelemetry-collector.monitoring.svc.cluster.local:4318/v1/traces`; traces show in Jaeger
  (`make oda-canvas-port-forward`).
- Random Langfuse secrets are generated into the cluster Secret `langfuse-secrets` (never in git).
- **Upgrading an older cluster**: earlier versions installed a plain `kube-prometheus-stack` as release `monitoring`.
  `make observability` stops with the command to remove it (`helm uninstall monitoring -n monitoring`) because
  Helm cannot turn it into the Canvas stack in place. `make down && make bootstrap-oda-canvas` is the clean alternative.

## Usage

Prerequisites: docker, kind, kubectl, helm (>= 3.16), envsubst (gettext), python3, curl, git, openssl, make.

**Resources** (Canvas minimum: 4 CPU / 16 GB RAM / 50 GB storage, see the installation README): the observability
stack (Prometheus, Grafana, Jaeger, OTel, and Langfuse with ClickHouse, Postgres, Redis, MinIO) is added on top of that,
so plan for **at least 6 CPU, 20 GB RAM and 60 GB free disk** for the full `make bootstrap-oda-canvas`, plus room for the components.

```bash
cd oda_canvas
cp config.env.example config.env   # then set the passwords/secrets
make environment              # pre-flight + kind + Helm repos/deps + Istio + Vault prerequisites
make oda-canvas               # the ODA Reference Implementation (slow: many images)
make observability            # Prometheus, Grafana, OpenTelemetry, Jaeger, Langfuse
make canvas-portal            # canvas-portal-v2 on the Canvas Keycloak
make component-sample         # reference component must reach "Complete"
# or all five in one go (slow: many images):
make bootstrap-oda-canvas
make oda-canvas-port-forward  # all UIs on every host IP (FORWARD_ADDRESS=0.0.0.0): portal :8080, Grafana :3001,
                              # Prometheus :9090, Jaeger :16686, Langfuse :3002, OpenAN portal :8082, ExposedAPIs :80/:443 (Ctrl-C to stop;
                              # already the last step of bootstrap-oda-canvas)
make status
make down                     # delete the cluster
```

All targets are idempotent. `make preflight` (alias `check`) runs the host checks alone; `make canvas-verify` checks the
Canvas (8 `oda.tmforum.org` CRDs, Component `v1` served, `istio-ingress` address set, pods).

Login: `portaladmin` / the `PORTAL_USER_PASSWORD` from `config.env` (members of `canvas-admin` may use the Helm and
conformance APIs). Settings are in `config.mk`; passwords and secrets (portal user, Keycloak admin, Grafana admin, Langfuse initial user) are in `config.env` (untracked: `cp config.env.example config.env` first); override per run,
e.g. `make canvas-portal PORTAL_BUILD=false` to pull
`tmforumodacanvas/canvas-portal:{frontend,backend}-0.1.0` instead of building the images from `PORTAL_SRC` (the default, as those tags were not found on Docker Hub).

### ExposedAPI reachability and conformance testing

`make environment` gives `istio-ingress` the address `INGRESS_ADDRESS` (default `127.0.0.1`) so the ExposedAPI URLs are
populated; this must happen before components are installed (Canvas issue #621) and the patch is lost if the Service is
recreated: `make ingress-address` re-applies it. The URLs are reachable only while `make oda-canvas-port-forward` runs
(ports `INGRESS_HTTP_PORT`/`INGRESS_HTTPS_PORT`, default 80/443, so privileges or other ports may be needed:
`make oda-canvas-port-forward INGRESS_HTTP_PORT=8081 INGRESS_HTTPS_PORT=8443`).

The TM Forum CTK containers call those URLs from inside Docker, where `127.0.0.1` is the container itself. Use an address
both sides reach, and keep it identical for the patch and the forward, e.g. on Linux
`make oda-canvas-port-forward INGRESS_ADDRESS=172.17.0.1` (the `docker0` address; the forwards listen on `0.0.0.0` by default), on Docker
Desktop `INGRESS_ADDRESS=host.docker.internal`.

### Component namespaces

The Canvas operators only reconcile `components` and `odacompns-*`. The demo creates `components` (Canvas chart) and
labels every namespace in `COMPONENT_NAMESPACES` with `istio-injection=enabled`. The former `temp-namespace` was removed:
no operator watched it and it had no sidecar injection, so the portal's conformance RBAC now targets `components` only.

## Checking component metrics in Prometheus

After `make component-sample` (release `pc`) the Canvas creates a `ServiceMonitor`, and Prometheus lists the target
`pc-productcatalogmanagement-sm` as `up`. The Product Catalog counter is named after the release:
**`pc_productcatalogmanagement_api_counter`** (label `NotificationEvent`). The Canvas tutorial calls it
`product_catalog_api_counter`, which returns nothing here. The series appears only after the API handles events;
for example, with `kubectl -n components port-forward svc/pc-prodcatapi 18080:8080`, POST/PATCH/DELETE under
`http://localhost:18080/pc-productcatalogmanagement/tmf-api/productCatalogManagement/v4/category`. Then query
`sum by (NotificationEvent) (pc_productcatalogmanagement_api_counter)` (`make oda-canvas-port-forward`, Prometheus on
`localhost:$(PROMETHEUS_LOCAL_PORT)`).

## Reference timings

One measured run of `make down && make bootstrap-oda-canvas INGRESS_HTTP_PORT=8081 INGRESS_HTTPS_PORT=8443` (2026-11-02,
Linux host, kind cluster `oda-canvas`). The forwards were listening 25 min 25 s after `make down` started. Treat the numbers as an
order of magnitude: they depend on network speed and on which images are already in the local Docker cache.

| Stage / target | Time | Notes |
|---|---|---|
| `make down` | 37.0 s | deletes the kind cluster |
| **`environment`** | **2 m 30 s** | |
| ↳ `preflight` | 1.2 s | |
| ↳ `cluster` | 48.3 s | kind create |
| ↳ `deps` | 29.2 s | |
| ↳ `istio` | 62.8 s | |
| ↳ `ingress-address`, `gateway-api-crds`, `vault-prepare`, `permissions` | 0.6 s, 2.9 s, 1.5 s, 3.6 s | |
| **`oda-canvas`** | **6 m 40 s** | |
| ↳ `deps` | 27.5 s | |
| ↳ image preload, Helm install and waits | 6 m 08 s | the longest stage |
| ↳ `canvas-verify` | 2.9 s | |
| **`observability`** | **5 m 05 s** | includes `deps` (28 s) |
| **`canvas-portal`** | **1 m 44 s** | |
| ↳ `images` | 72.5 s | |
| ↳ `portal-realm` | 3.6 s | |
| ↳ `portal` | 24.4 s | |
| ↳ `verify` | 3.3 s | |
| **`component-sample`** | **8 m 08 s** | |
| ↳ pull and load images, Helm install | 7 m 06 s | |
| ↳ `component-verify` | 41.5 s | Complete on the 5th poll |
| **`oda-canvas-port-forward`** | 0.6 s to listening | services exposed |
| **Bootstrap total (to forwards up)** | **24 m 48 s** | |
| **Total including `make down`** | **25 m 25 s** | |

`component-sample` and `oda-canvas` are about 59% of the total, mostly image pulls, loading them into kind and the
Helm waits.

## Design notes

- Portal Services are `ClusterIP` (the chart has no nodePort setting), so access is by `port-forward`.
  `CORS_ORIGINS` is set for `http://localhost:$LOCAL_PORT`.
- Backend and the nginx proxy reach Keycloak at the in-cluster URL
  `http://canvas-keycloak.canvas.svc.cluster.local:8083/auth`; login goes through the backend, so the token
  issuer matches what the backend validates.
- `cookieSecure` stays `false` (plain HTTP). Credentials are demo-only; do not reuse outside a local kind cluster.
- `.rendered/` holds generated values and the kind config and is gitignored.
- `dependentapi-simple-operator` no longer points at the disabled component-registry (`componentRegistryUrl: ""`). The operator
  image itself may still log `CANVAS_INFO_ENDPOINT=http://info.canvas.svc.cluster.local` (an image default; the info service
  is off). Whether a component with dependent APIs resolves cleanly without both services has not been exercised.

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
   rerun `make bootstrap-oda-canvas` (it is idempotent) or pre-pull the base images while the route works:
   `node:20-alpine`, `nginx:1.27-alpine`, `python:3.12-slim`, `python:3.12-alpine`. `make` reuses local
   images. Canvas images (incl. `bitnamilegacy/*`) are pulled by `scripts/preload-images.sh`.

The kind node does not pull the images: each stage `kind load`s its own (`scripts/preload-images.sh environment|canvas|observability`).
The Canvas hook Jobs default to `bitnamilegacy/kubectl:latest`, which Kubernetes always pulls; `canvas-values.yaml.tpl` pins it
to `HOOK_KUBECTL_TAG`, so, once the image is loaded, the hooks no longer need the registry (not exercised on an air-gapped host).

## OpenAN operator (optional)

An external kopf operator (`operator/openan-operator`, chart `charts/openan-operator`) manages OpenAN as ODA
Components and publishes Canvas `a2a` ExposedAPIs as Agent Cards in the Registry Center. See
`operator/openan-operator/README.md` for the design. It is not part of `make bootstrap-oda-canvas`:

```bash
make openan-operator-image   # build the operator image, pull RC/OC images, kind load them
make openan-operator         # CRD + operator + OpenAN CR (Registry Center, Orchestration Center)
make openan-sample           # sample a2a Component; waits until its card is in the Registry
```

Demo limits: Agent Cards are ES256-signed (`cardSync.signing.enabled: true` in `openan-cr.yaml.tpl`); the Registry verifies them with keys from the operator-maintained trust ConfigMap (`<cr>-registry-trust`), so no HTTPS `jku` endpoint is needed (the ConfigMap is created when the first a2a Agent Card is fetched, so it is absent until then, e.g. while `sample-agent` is in `ImagePullBackOff`); RC `v1.1.2` runs as uid 10001 and OC `v1.0.0` as 1000
(`values.podSecurityContext`).

### OpenAN portal access

The portal (`openan-frontend-frontend`, namespace `components`, image `openan-portal:$OPENAN_FE_TAG`) is the only OpenAN
component with a web UI; the Registry and Orchestration Centers are REST APIs only. The kind cluster maps no host ports, so
it is reached through a port-forward, in one of two ways:

| Access | URL | How |
|---|---|---|
| Direct to the service | `http://<host>:8082/` (`OPENAN_PORTAL_LOCAL_PORT`) | `make oda-canvas-port-forward` (skipped if the portal is not deployed), or `kubectl -n components port-forward --address 0.0.0.0 svc/openan-frontend-frontend 8082:8080` |
| Through the Istio gateway | `https://<host>:8443/` | `make openan-portal-route` once, then `make oda-canvas-port-forward INGRESS_HTTP_PORT=8081 INGRESS_HTTPS_PORT=8443` |

`<host>` is `localhost` or any host address (the forwards listen on `FORWARD_ADDRESS`, default `0.0.0.0`). The gateway
serves a self-signed certificate (`curl -k`), and plain HTTP on the gateway redirects to HTTPS on port 443, so open the
HTTPS URL directly when using non-default ports.

`manifests/openan/portal-virtualservice.yaml` routes `/` on `canvas/component-gateway` to the portal. It is a catch-all
because the portal's nginx uses absolute paths (`/api/orchestrate`, `/assets`) and cannot sit under a prefix. Istio merges
VirtualServices of host `*` in creation order, so it must be created after the ExposedAPI routes: if a Component is
recreated, its route lands behind `/` and is shadowed until `make openan-portal-route` is run again.

Login: the Orchestration Center's own user accounts (not Keycloak). The admin account is seeded only when the users
table is empty, with the password from `OC_ADMIN_INITIAL_PASSWORD` in the `openan-orchestration-runtime` Secret:

```bash
kubectl -n components get secret openan-orchestration-runtime -o jsonpath='{.data.OC_ADMIN_INITIAL_PASSWORD}' | base64 -d
```

