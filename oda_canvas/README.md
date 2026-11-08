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
                              # Prometheus :9090, Jaeger :16686, Langfuse :3002, ExposedAPIs :80/:443 (detached: systemd user services, survive a closed
                              # terminal and auto-restart; `make oda-canvas-port-forward-stop`; already the last step of bootstrap-oda-canvas)
make openan-port-forward      # OpenAN portal :8082, same detached mode, independent of the above (`make openan-port-forward-stop`)
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

### OpenAN recycle timing

One measured run of `make openan-clean && make bootstrap-openan` (2026-11-02, same Linux host and kind cluster, Canvas
already running, Docker image cache warm: nothing was pulled or rebuilt, so a cold run is slower). Everything was serving
(`complete` event with the LLM answer from `Sample-A2A-T-Agent` through the Orchestration Center) 7 min 00 s after the clean
started. An identical run just before took 6 min 54 s, so the numbers are repeatable within a few seconds on a warm host. Same
caveat as above: treat them as an order of magnitude.

| Target | Time | Notes |
|---|---|---|
| **`make openan-clean`** | **1 m 06 s** | |
| ↳ route, sample agent, OpenAN CR, operator release, CRD | 5.7 s | the operator's finalizers uninstall the three component releases |
| ↳ data PVCs | 59.5 s | `kubectl delete pvc` waits for the old pods to finish terminating (60 s grace period) |
| ↳ `openan` Helm repo | < 1 s | removed only if present (it was already gone in this run) |
| **`bootstrap-openan`** | **5 m 54 s** | |
| ↳ `openan-operator-image` | 2.5 s | images already built and cached |
| ↳ **`openan-operator`** | **3 m 32 s** | |
| &nbsp;&nbsp;&nbsp;↳ CRD, operator Helm install | 3.9 s | |
| &nbsp;&nbsp;&nbsp;↳ restart, rollout, wait for the old operator pod to be gone | 33.2 s | the 30 s grace period of the old pod |
| &nbsp;&nbsp;&nbsp;↳ `OpenAN` CR to Ready (Registry Center, Orchestration Center, portal) | 2 m 55 s | the longest step: three Helm releases one after the other, each waiting for its Component to be Complete |
| ↳ **`openan-sample`** | **2 m 13 s** | |
| &nbsp;&nbsp;&nbsp;↳ image build (cached) and `kind load`, apply | 1.9 s | |
| &nbsp;&nbsp;&nbsp;↳ `openan-verify` | 2 m 11 s | operator fetch and signing, then the Registry accepts the card once the kubelet has synced the trust ConfigMap into its pod |
| ↳ `openan-portal-route` | 0.9 s | |
| ↳ `openan-port-forward` | 4.3 s | systemd user service |
| **Total (clean + bootstrap)** | **7 m 00 s** | |

`openan-operator` waits for the old operator pod to terminate before it applies the CR. Without that wait, the terminating
pod and the new one reconcile the CR at the same time and both run `helm upgrade --install`; the pod that is killed
mid-upgrade leaves the release in `pending-upgrade`, every later upgrade fails with "another operation (install/upgrade/
rollback) is in progress", and the CR never gets Ready (`make bootstrap-openan` times out after 10 minutes). The operator
now repairs such a release by itself (rollback, after 150 s without progress; see
`operator/openan-operator/README.md`), so a stuck release delays the CR by about 2.5 minutes instead of blocking it; if you
have an older operator image, `make openan-clean` removes the stuck release and a new `make bootstrap-openan` starts clean.

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
make openan-sample           # Sample-A2A-T-Agent: builds its image (sample-agent/), loads it into kind, deploys it; waits until its card is in the Registry
make openan-portal-route     # portal on the Istio gateway at "/" (last: it is a catch-all route)
# or all of it in the right order, on top of a running Canvas:
make bootstrap-openan        # openan-operator-image, openan-operator, openan-sample, openan-portal-route, openan-port-forward
make openan-clean            # remove everything the openan-* targets created (CR, releases, PVCs, operator, CRD, `openan` Helm repo)
```

Demo limits: Agent Cards are ES256-signed (`cardSync.signing.enabled: true` in `openan-cr.yaml.tpl`); the Registry verifies them with keys from the operator-maintained trust ConfigMap (`<cr>-registry-trust`), so no HTTPS `jku` endpoint is needed (the ConfigMap is created when the first a2a Agent Card is fetched, so it is absent until then, e.g. while `sample-agent` is in `ImagePullBackOff`). The Registry mounts it as optional, so the first publish can get `401 no valid signature with a trusted key` until the kubelet syncs the mount into the pod (about a minute); the operator retries and then succeeds. RC `v1.1.2` runs as uid 10001 and OC `v1.0.0` as 1000
(`values.podSecurityContext`).

### Sample-A2A-T-Agent

`sample-agent/` is a small A2A + A2A-T agent that passes every message it receives to the LLM and replies with the answer
(`OPENAN_LLM_BASE_URL` / `OPENAN_LLM_MODEL` in `config.mk`, an OpenAI-compatible endpoint reachable from the pods).

- **A2A** (`a2a-sdk` 1.x): an `AgentExecutor` behind `DefaultRequestHandler` and a `TaskUpdater`
  (submitted, working, artifact, completed/failed/rejected), JSON-RPC v1.0 with v0.3 compatibility, streaming
  (`message/stream`) and blocking (`message/send`) calls, and the public Agent Card (also under the Istio gateway path).
- **A2A-T** (`a2a-t-sdk`): the card declares the Task-T extension. When a client activates it (`A2A-Extensions` header) the
  business prompt in the message metadata is checked with `A2ATServer.check_task_prompt` before it reaches the model; a
  non-compliant prompt is `REJECTED`. Terminal status updates carry the `__sdk_event__` metadata that the Orchestration
  Center's `/execute` reads to finish a run. Negotiation-T is not implemented.
- **LLM**: the model's answer is the artifact and the final status message. Qwen-style reasoning models answer in a hidden
  field unless thinking is off, so `LLM_ENABLE_THINKING` defaults to `false`; the A2A-T SDK's several leading `system`
  messages are merged into one (strict chat templates reject them). The check itself needs the model, so a Task-T prompt
  takes a while to validate.

Try it through the Orchestration Center (any workflow id works; `target_agent` bypasses planning):
`GET /rest/v1/orchestrate/execute?psop_id=<id>&target_agent=Sample-A2A-T-Agent&user_intent=<text>` streams a `complete`
event whose `result` is the LLM answer.

### OpenAN portal access

The portal (`openan-frontend-frontend`, namespace `components`, image `openan-portal:$OPENAN_FE_TAG`) is the only OpenAN
component with a web UI; the Registry and Orchestration Centers are REST APIs only. The kind cluster maps no host ports, so
it is reached through a port-forward, in one of two ways:

| Access | URL | How |
|---|---|---|
| Direct to the service | `http://<host>:8082/` (`OPENAN_PORTAL_LOCAL_PORT`) | `make openan-port-forward` (detached), or `kubectl -n components port-forward --address 0.0.0.0 svc/openan-frontend-frontend 8082:8080` |
| Through the Istio gateway | `https://<host>:8443/` | `make openan-portal-route` once, then `make oda-canvas-port-forward INGRESS_HTTP_PORT=8081 INGRESS_HTTPS_PORT=8443` |

`<host>` is `localhost` or any host address (the forwards listen on `FORWARD_ADDRESS`, default `0.0.0.0`). The gateway
serves a self-signed certificate (`curl -k`), and plain HTTP on the gateway redirects to HTTPS on port 443, so open the
HTTPS URL directly when using non-default ports.

`manifests/openan/portal-virtualservice.yaml` routes `/` on `canvas/component-gateway` to the portal. It is a catch-all
because the portal's nginx uses absolute paths (`/api/orchestrate`, `/assets`) and cannot sit under a prefix. Istio merges
VirtualServices of host `*` in creation order, so it must be created after the ExposedAPI routes: if a Component is
recreated, its route lands behind `/` and is shadowed until `make openan-portal-route` is run again.

Login: user `admin` and the Orchestration Center's own accounts (not Keycloak). `make openan-credentials` prints the URL, the
user and the password. The password is generated by the operator when it creates the `openan-orchestration-runtime` Secret
(owned by the `OpenAN` CR) and the account is seeded from it only while the users table is empty. **`make openan-clean`
deletes the CR, the Secret and the data volume, so every `make bootstrap-openan` has a new password** (so does `make down`,
which deletes the whole cluster; after it, `make bootstrap-oda-canvas` then `make bootstrap-openan`): the one from before
a recycle is rejected ("cannot log in" right after a re-deployment). The same value, by hand:

```bash
kubectl -n components get secret openan-orchestration-runtime -o jsonpath='{.data.OC_ADMIN_INITIAL_PASSWORD}' | base64 -d
```

