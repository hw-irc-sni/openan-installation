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
make k8s-dashboard-port-forward  # Kubernetes Dashboard https :8444, same detached mode (`make k8s-dashboard-port-forward-stop`)
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
Components and publishes Canvas `a2a` ExposedAPIs as Agent Cards in the Registry Center. With agent sync it also turns
registry cards into Agent ODA Components. It keeps an A2A route for every agent in agentgateway (see "Agent routes in the
gateway" below). See `operator/openan-operator/README.md` for the design. It is not part of `make bootstrap-oda-canvas`:

```bash
make openan-operator-image   # build the operator image, pull RC/OC images, kind load them
make openan-operator         # CRD + operator + OpenAN CR (Registry Center, Orchestration Center); agentgateway routes on
make openan-sample           # Sample-A2A-T-Agent: builds its image (sample-agent/), loads it into kind, deploys it; waits until its card is in the Registry
make agentgateway            # AI gateway (charts/agentgateway from AGENTGATEWAY_SRC): pulls/builds and loads its images, installs it in canvas
make openan-portal-route     # portal on the Istio gateway at "/" (last: it is a catch-all route)
make k8s-dashboard           # official Kubernetes Dashboard (chart 7.14.0) + read-only login account; `make k8s-dashboard-token` issues a 1h token
# or all of it in the right order, on top of a running Canvas:
make bootstrap-openan        # openan-operator-image, openan-operator, openan-sample, agentgateway, openan-portal-route, k8s-dashboard,
                             # openan-port-forward, k8s-dashboard-port-forward
make openan-clean            # remove everything the openan-* targets, agentgateway and k8s-dashboard created (CR, releases, PVCs, operator, CRD, `openan` Helm repo)
make csp-knowledge-sample    # optional, after agentgateway: c-kg knowledge-graph MCP server, registered in agentgateway (see below)
```

**AI gateway.** `make agentgateway` installs the Canvas [agentgateway](https://github.com/agentgateway/agentgateway) chart as
release `agentgateway` in `AGENTGATEWAY_NS` (`canvas`) with its kind overlay: the `OPENAN_LLM_*` model at `/v1/*`, the test MCP
server at `/mcp`, and the Sample-A2A-T-Agent at `/agents/sample-agent`. The chart is not in every Canvas branch, so
`AGENTGATEWAY_SRC` (`config.mk`) points at a checkout that has it, by default `oda-canvas-merged-demo`. Re-running it keeps the
models and MCP servers changed from the Canvas Portal's AI Gateways page. `make openan-clean` uninstalls it; the
`agentgateway-llm-keys` Secret stays.

**Agent routes in the gateway.** With `OPENAN_AGENTGATEWAY_ROUTES=true` (default), `make openan-operator` lets the OpenAN
operator keep one A2A route per a2a ExposedAPI of `OPENAN_NS` in the gateway's live config. This covers agents installed in
Kubernetes as well as agents from the internal or a connected registry that agent sync turned into Components. Each
route is at `/agents/<namespace>/<ExposedAPI>`, for example
`http://agentgateway.canvas.svc.cluster.local/agents/components/sample-agent-sampleagent-a2tagent/.well-known/agent-card.json`
(the chart's own `/agents/sample-agent` route stays). The operator writes only routes named `openan.<namespace>.*` in
the `agentgateway-config` ConfigMap: the standalone gateway has no write API, only this file and the read-only port
8081. A re-run of `make agentgateway` drops them, and the operator adds them back within about 15 s. A route appears
in the gateway, and on the portal's AI Gateways page, about a minute after the ExposedAPI, once the kubelet has
updated the file. The cards published to the Registry Center keep the agent's own URL unless
`OPENAN_AGENTGATEWAY_PUBLISH_URL` is set (e.g. `make openan-operator
OPENAN_AGENTGATEWAY_PUBLISH_URL=http://agentgateway.canvas.svc.cluster.local`): then they advertise the gateway route.
Details: `operator/openan-operator/README.md`, "Agent gateway routes".

The portal is wired to it in `portal-values.yaml.tpl` (`backend.aiGateways.*`, `rbac.agentgatewayConfigAccess`, and
`AGENTGATEWAY_API_KEY`, which `make portal` reads from the `agentgateway-portal-key` Secret). The portal is installed before
the gateway, and a new gateway install generates a new key, so `make agentgateway` then gives the portal the current key with
`helm upgrade --reset-then-reuse-values` (other portal values are kept). It only does so if the portal release runs the chart
version in `PORTAL_SRC`; otherwise it prints the command. These settings need canvas-portal chart 0.1.3 or later, which is why
`PORTAL_SRC` defaults to the `oda-canvas-merged-demo` checkout.

**Kubernetes Dashboard.** `make k8s-dashboard` (part of `make bootstrap-openan`) installs the official Kubernetes
Dashboard with a read-only login account; see [Kubernetes Dashboard access](#kubernetes-dashboard-access).

**Registry Center in the portal.** `portal-values.yaml.tpl` also sets `backend.registryCenter.url` to the OpenAN Registry
Center (`openan-registry-center-registry-center.$OPENAN_NS:5000`), for the portal's AI Registries > Agents section. The Registry
Center only exists after `make bootstrap-openan`. No `REGISTRY_CENTER_TOKEN` is set.

**TMF639 / TMFC012 (optional, off in the demo).** `charts/openan-registry-center` 0.2.0 can declare the Registry Center as a
TMFC012 Resource Inventory of A2A agents (plan and deviations: [`TMF639-FACADE-PLAN.md`](TMF639-FACADE-PLAN.md)). Set these
in the chart values, or under `registryCenter.values` in the OpenAN CR. They need a `registry-center` image that contains
`agent_registry/tmf639`.

| Value | Effect |
|---|---|
| `tmf639.enabled` | Exposes `TMF639` (`/<component>/tmf-api/resourceInventoryManagement/v5`, `required: true`); the native API becomes `required: false`; `componentMetadata` becomes `TMFC012` / `Production` unless `component.id` / `component.functionalBlock` are set. |
| `tmf639.specificationUrl` | Required with `tmf639.enabled`: the release-independent profile `registry-center/docs/openapi/tmf639-a2a-agent-v5.yaml` at a pinned tag or commit. The canonical TMF OAS URL is refused, because the Canvas matches dependencies by exact URL and would route every TMF639 consumer here. |
| `broadcast.enabled`, `broadcast.callbackAllowlist`, `broadcast.allowHttpCallbacks` | Change broadcast for the TMF639 `/hub` (and native webhooks); the allowlist is required. With `tmf639.enabled` the four TMF639 events are declared under `publishedEvents` (delivered as TMF630 callbacks, not a Canvas topic). |
| `broadcast.callbackCaConfigMap`, `broadcast.callbackCaKey` | Extra CAs (PEM, from a ConfigMap) trusted for callback TLS, for listeners with private certificates. Through the operator the ConfigMap must be in `ALLOWED_CALLBACK_CA_CONFIGMAPS`. |
| `catalog.enabled`, `catalog.specificationUrl` | Declares the optional `TMF634` DependentAPI. Only the declaration: the Registry Center does not check cards against a catalog yet (plan §8). The Canvas ignores `required: false`, so the Component stays `InProgress` until a catalog with exactly that URL exists. |

Set `registry.publicBaseUrl` to the gateway URL with the component prefix so resource and event `href`s resolve for clients.

**Gateway path prefix.** The Canvas component gateway forwards `/<componentName>/...` to the service without rewriting it.
The chart therefore sets `REGISTRY_PATH_PREFIX=/<componentName>`, and the Registry Center serves every route under that
prefix as well as without it. Registry Center images from before `path.prefix` (including `v1.1.2`) ignore the variable,
so their ExposedAPI URLs answer 404 through the gateway. The portal and the operator call the Service directly, so they
are unaffected.

**Demo variables** (`config.mk`; override on the `make` command line):

| Variable | Default | Purpose |
|---|---|---|
| `OPENAN_RC_BUILD`, `OPENAN_RC_SRC` | `false`, `../../registry-center` | Build the Registry Center image from source instead of pulling it. Use a non-release `OPENAN_RC_TAG`, for example `src`. `make openan-operator` then restarts the Deployment so the new build runs. |
| `OPENAN_AGENTGATEWAY_ROUTES` | `true` | Chart value `agentGateway.enabled` of the operator: an A2A route per a2a ExposedAPI of `OPENAN_NS` in the agentgateway of `AGENTGATEWAY_NS` (`/agents/<namespace>/<ExposedAPI>`), plus a Role there limited to `agentgateway-config` and `agentgateway-portal-key`. `false`: neither. |
| `OPENAN_AGENTGATEWAY_PUBLISH_URL` | empty | Chart value `agentGateway.publishUrl` (operator env `AGENTGATEWAY_PUBLISH_URL`). Only when set, for example to `http://agentgateway.canvas.svc.cluster.local`, do the cards published to the registries advertise the agent's gateway route instead of its own Service URL. Clear it and re-run `make openan-operator` to go back. |
| `OPENAN_RC_PUBLIC_BASE_URL` | `https://$(INGRESS_ADDRESS)/openan-registry-center-registrycenter` | `registry.publicBaseUrl`, the base of TMF639 `href`s. |
| `OPENAN_TMF639_ENABLED`, `OPENAN_TMF639_SPEC_URL` | `false`, empty | `tmf639.*` in the OpenAN CR. |
| `OPENAN_ALLOWED_SPEC_URL_PREFIXES` | empty (operator default: `project-openan/registry-center` raw URLs) | Operator env `ALLOWED_SPEC_URL_PREFIXES`, for example to accept a fork's profile URL. |
| `OPENAN_BROADCAST_ENABLED`, `OPENAN_BROADCAST_CALLBACK_HOSTS` | `false`, empty | `broadcast.*` in the OpenAN CR: native webhooks and, with TMF639, the `/hub` and its published events. The hosts (comma separated, required when enabled) are also passed to the operator as `ALLOWED_CALLBACK_HOSTS`. Callbacks must use `https`. |
| `OPENAN_BROADCAST_CALLBACK_CA_CONFIGMAP`, `OPENAN_BROADCAST_CALLBACK_CA_KEY` | empty, `ca.crt` | `broadcast.callbackCaConfigMap`/`callbackCaKey` in the OpenAN CR, and the operator's `ALLOWED_CALLBACK_CA_CONFIGMAPS`: a ConfigMap in `OPENAN_NS` with extra CAs (PEM) for listeners with private certificates. Create it yourself; the Registry Center pod waits until it exists. Needs a Registry Center image with `broadcast.callback.ca.file`. |

TMF639 from a `registry-center` checkout of the fork's `oda-integration` branch (before an upstream release). The profile
URL follows the same branch, so it changes when the branch moves; use a commit or tag to pin it:

```bash
make openan-operator-image openan-operator OPENAN_RC_BUILD=true OPENAN_RC_TAG=src \
  OPENAN_TMF639_ENABLED=true \
  OPENAN_TMF639_SPEC_URL=https://raw.githubusercontent.com/hw-irc-sni/registry-center/oda-integration/docs/openapi/tmf639-a2a-agent-v5.yaml \
  OPENAN_ALLOWED_SPEC_URL_PREFIXES=https://raw.githubusercontent.com/hw-irc-sni/registry-center/
```

To enable the `/hub` as well, add `OPENAN_BROADCAST_ENABLED=true OPENAN_BROADCAST_CALLBACK_HOSTS=<host>[,<host>...]`
with the host names of the `https` listeners that will subscribe. For a listener with a private certificate, put its CA in
a ConfigMap and add `OPENAN_BROADCAST_CALLBACK_CA_CONFIGMAP=<name>`, for example:

```bash
kubectl -n components create configmap hub-listener-ca --from-file=ca.crt=ca.pem
make openan-operator-image openan-operator OPENAN_RC_BUILD=true OPENAN_RC_TAG=src ... \
  OPENAN_BROADCAST_ENABLED=true OPENAN_BROADCAST_CALLBACK_HOSTS=hub-listener.components.svc \
  OPENAN_BROADCAST_CALLBACK_CA_CONFIGMAP=hub-listener-ca
```

Through the OpenAN CR, the operator restricts these values:

- **Profile URL:** an allowed prefix (`ALLOWED_SPEC_URL_PREFIXES`).
- **Callback hosts:** only those listed in `ALLOWED_CALLBACK_HOSTS`.
- **Callback CA:** `broadcast.callbackCaConfigMap` only if listed in `ALLOWED_CALLBACK_CA_CONFIGMAPS`.
- **Not settable from a CR:** `broadcast.allowHttpCallbacks` and `catalog.specificationUrl`.

See `operator/openan-operator/README.md`.

**Helm repository.** The charts in `charts/` (`openan-operator`, `openan-registry-center`, `openan-orchestration-center`,
`openan-frontend`) are also published as a Helm repository on GitHub Pages (`gh-pages` branch, by
`.github/workflows/release-charts.yml`; see the repository `README.md`):

```bash
helm repo add openan https://hw-irc-sni.github.io/openan-installation
helm repo update
helm search repo openan
```

The demo does not use it: `make openan-operator` installs `charts/openan-operator` from the checkout, and the operator image
bundles the three component charts (`operator/openan-operator/Dockerfile` copies them to `/charts`), so local chart changes
take effect after `make openan-operator-image` without publishing. `make openan-clean` only removes a client-side `openan`
repo entry if you added one by hand.

**Not installable from the Canvas Portal.** Installing `openan-operator` from the portal's `/components/deploy` page fails with
`customresourcedefinitions.apiextensions.k8s.io is forbidden: User "system:serviceaccount:canvas-portal:canvas-portal-backend-sa"
cannot create resource "customresourcedefinitions" ... at the cluster scope`. This is by design on both sides, not a bug:

- The portal backend runs `helm upgrade --install` as its own ServiceAccount, which has the `admin` ClusterRole only through
  RoleBindings in `rbac.canvasInstallAccess.targetNamespaces` (`components` and `canvas` in `portal-values.yaml.tpl`), plus
  `oda.tmforum.org` access. It cannot create cluster-scoped objects, so it installs namespaced charts (ODA Components) only.
- `openan-operator` is an operator chart: besides the `OpenAN` CRD (`crds/openans.yaml`) it creates a ClusterRole and a
  ClusterRoleBinding (`templates/rbac.yaml`), so `--skip-crds` would only move the failure to the ClusterRole.

Install the operator as a cluster admin (`make openan-operator`, or `helm install` with your own kubeconfig), like the Canvas
operators. Granting the portal ServiceAccount the right to create CRDs, ClusterRoles and ClusterRoleBindings is effectively
cluster-admin and defeats the portal's namespaced design. On kind a portal install would also miss the operator image (built and
`kind load`ed by `make openan-operator-image`, not pulled) and conflict with the release `make openan-operator` manages.

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

### Kubernetes Dashboard access

The official [Kubernetes Dashboard](https://github.com/kubernetes-retired/dashboard) shows the whole kind cluster: the
Canvas, the observability stack, the OpenAN components: their pods, logs and events. `make bootstrap-openan` installs it
and starts its port-forward; on its own:

```bash
make k8s-dashboard                 # chart, images (kind load) and the dashboard-user login account
make k8s-dashboard-port-forward    # https://<host>:8444/ (detached, like the other forwards)
make k8s-dashboard-token           # a login token, valid for 1 hour
```

1. Open `https://localhost:8444/` (or any host address: the forward listens on `FORWARD_ADDRESS`, default `0.0.0.0`) and
   accept the self-signed certificate. The chart's install notes mention port 8443: ignore them, the demo uses
   `K8S_DASHBOARD_LOCAL_PORT` (8444) because 8443 is the usual alternative ingress HTTPS port.
2. Paste the output of `make k8s-dashboard-token` into the **Bearer token** field. When the session ends or the token expires
   (`K8S_DASHBOARD_TOKEN_DURATION`, default `1h`), run it again; tokens are never stored in the cluster.

**Permissions.** The login account `dashboard-user` (`manifests/k8s-dashboard-user.yaml.tpl`) is bound to the ClusterRole
`K8S_DASHBOARD_ROLE`:

| `K8S_DASHBOARD_ROLE` | What the dashboard can do |
|---|---|
| `view` (default) | Read the built-in resources in every namespace (pods and their logs, Deployments, Services, events...). No changes, and Secrets are hidden. Custom resources such as ODA `Component`s and `ExposedAPI`s are not readable: their CRDs do not extend `view`. |
| `cluster-admin` | Everything, including Secrets, `exec` into pods, scaling and deleting. Demo only. |

Switch with `make k8s-dashboard K8S_DASHBOARD_ROLE=cluster-admin`, and back with plain `make k8s-dashboard` (the binding is
re-created, because a binding's role cannot be changed in place). Tokens issued before the switch keep working with the new
role until they expire. Check what the account may do with
`kubectl auth can-i --list --as=system:serviceaccount:kubernetes-dashboard:dashboard-user`. Testing with
`kubectl --token=...` is misleading on kind: the kubeconfig's admin client certificate takes precedence over the token.

**Notes.**

- The project is archived and its Helm repository (`kubernetes.github.io/dashboard`) returns 404. The chart is the tarball of
  its last release on GitHub (`K8S_DASHBOARD_CHART_VERSION=7.14.0`, `K8S_DASHBOARD_CHART_URL` in `config.mk`), downloaded
  once into `.rendered/`. Its images (`kubernetesui/dashboard-*`, `kong`) are pulled by Docker and `kind load`ed.
- There is no metrics-server in the kind cluster, so the CPU and memory graphs stay empty; everything else works.
- `make k8s-dashboard-port-forward-stop` stops the forward; `make k8s-dashboard-clean` removes the forward, the login account,
  the release and the `kubernetes-dashboard` namespace. `make openan-clean` runs it too.
- Earlier versions of `make k8s-dashboard` created a `cluster-admin` account named `dashboard-admin`; `make k8s-dashboard`
  deletes it.

### CSP knowledge sample (c-kg MCP server behind agentgateway)

`make csp-knowledge-sample` deploys c-kg (`CSP_KNOWLEDGE_SRC`, a knowledge graph with an autonomous-networks
ontology, served as an MCP server) next to the AI gateway and registers it there as an MCP target. It needs
`make agentgateway` (or `make bootstrap-openan`) first and is not part of either bootstrap.

```bash
make csp-knowledge-sample    # build c-kg from CSP_KNOWLEDGE_SRC, kind load, deploy, register in agentgateway, check through it
make csp-knowledge-verify    # the check alone
make csp-knowledge-clean     # target, key, Deployment, Service, Secret and PVC (the graph data)
```

What it does:

1. **Image.** `docker build` of `CSP_KNOWLEDGE_SRC` (default `../../../knowledge/c-kg`, its working tree as it is) as
   `CSP_KNOWLEDGE_IMAGE` (`c-kg:sample`), then `kind load`. The pod template carries the image ID, so a rebuild rolls the pod.
2. **Workload** (`manifests/csp-knowledge.yaml.tpl`, adapted from c-kg's `deploy/k8s/c-kg.yaml`) in `CSP_KNOWLEDGE_NS`
   (default: the agentgateway namespace, `canvas`, which has no Istio sidecar): Deployment `csp-knowledge` (1 replica,
   `Recreate`, TCP probes), Service `csp-knowledge:8000` (`/mcp`, Streamable HTTP), PVC `csp-knowledge-data` for
   `kg_data.ttl`. `C_KG_BASE_IRI` is pinned to `CSP_KNOWLEDGE_BASE_IRI` (`http://c-kg.net`).
3. **Key.** c-kg runs open without a key, so a random one is generated once into Secret `csp-knowledge-api-key` (kept
   across re-runs) and copied to `agentgateway-llm-keys` as `csp-knowledge-api-key`. The gateway reads it as
   `/llm-keys/csp-knowledge-api-key`, as for tokens entered on the portal.
4. **Registration** (`scripts/agentgateway-mcp-target.py`): the entry
   `{name: csp-knowledge, mcp: {host: http://csp-knowledge.canvas.svc.cluster.local:8000/mcp}, policies: {backendAuth: {key: {file: /llm-keys/csp-knowledge-api-key}}}}`
   is added to `mcp.targets` in the live `agentgateway-config` ConfigMap, with the tool restriction below. If the key,
   the entry or the restriction changed, the gateway
   Deployment is restarted (rolling), because a gateway that loads a config before its key file exists does not pick the
   file up later. A re-run with nothing changed does not touch the gateway.
5. **Check** (`scripts/agentgateway-mcp-verify.py`, run in the c-kg pod because the gateway image has no shell): MCP
   `initialize` and `tools/list` on `http://agentgateway.canvas.svc.cluster.local/mcp` must list the allowed c-kg tools
   (named `csp-knowledge_<tool>`) and still the `test-mcp` ones, `csp-knowledge_delete_entity` must be neither listed nor
   callable, and `csp-knowledge_graph_stats` must answer. If it fails, the
   target is removed again: with agentgateway's default `failureMode`, one unreachable target fails **every** MCP request
   through the gateway.

**Read-only through the gateway.** The gateway's traffic port has no client authentication: any pod that reaches it uses
c-kg with the gateway's key. So only the tools in `CSP_KNOWLEDGE_ALLOWED_TOOLS` (`config.mk`) go through, by default the 14
read-only ones (`sparql_query`, which refuses SPARQL Update, `get_entity`, `list_classes`, `list_instances`,
`get_class_schema`, `list_ontologies`, `graph_stats`, `validate`, `get_subgraph`, `get_path`, `find_workflows`,
`get_workflow_steps`, `list_agents`, `get_workflow_executions`). The others (add/update/delete, `load_graph`/`save_graph`
with their file paths, ontology, agent, BPMN and reasoning changes) are not listed, and a call gets HTTP 400 with the
JSON-RPC error `unknown resource`. `CSP_KNOWLEDGE_ALLOWED_TOOLS=` (empty) lets all 29 through.

agentgateway v1.7.0 has no per-target tool filter: the restriction is `mcp.policies.mcpAuthorization`, whose rules apply to
every target, are OR'ed, and deny everything they do not allow once one exists. The script therefore writes one rule per
kind (tool, prompt, resource, task) allowing every other target, plus one allowing the listed `csp-knowledge` tools, and
refuses to add them next to rules it did not write (OR'ed rules could not restrict a second target separately).
`make agentgateway` keeps these rules with the targets (`scripts/agentgateway-live-values.py`).

Use the read-only tools from any MCP client in the cluster on the gateway's `/mcp`. Everything, writes included, is on
`http://csp-knowledge.canvas.svc.cluster.local:8000/mcp` with `Authorization: Bearer <key>` (`kubectl -n canvas get secret
csp-knowledge-api-key -o jsonpath='{.data.api-key}' | base64 -d`; `canvas` is the default `CSP_KNOWLEDGE_NS`), for example
`load_graph` with `path=/app/ontologies/an/an_examples.ttl` for sample data. The Canvas Portal's AI Gateways page reads the
same config, so it should list the target (not checked).

Notes:

- **Host header.** c-kg compares the `Host` header literally, port included, and answers 421 otherwise; agentgateway sends
  it without the port. The Deployment therefore allows both forms of every Service name (`C_KG_ALLOWED_HOSTS`).
- **Residual access.** The read-only tools still answer anyone who reaches the gateway, and c-kg's `sparql_query` does not
  refuse `SERVICE`, with which rdflib fetches remote SPARQL endpoints from the c-kg pod. Demo only.
- **Persistence.** The data graph is written to the PVC only on a graceful shutdown (SIGTERM); imported BPMN workflows are
  never persisted (c-kg's `docs/deployment.md`).
- **Gateway re-installs.** `make agentgateway` keeps the live MCP targets and policies, so the registration survives it. `make
  openan-clean` uninstalls the gateway, and a fresh install has only the chart's targets: run `make csp-knowledge-sample`
  again after `make bootstrap-openan`.
