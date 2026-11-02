# `make up` vs. the full ODA Canvas deployment: gap analysis

Scope: `oda_canvas/Makefile` + `oda_canvas/README.md` (this repo, branch `oda-integration`) compared with
`tmforum-oda/oda-canvas` (`installation/README.md`, `charts/canvas-oda`, every chart under `charts/`;
checkout at commit `b37bb155`, umbrella `canvas-oda` 1.2.6-hv1).

Method: file reading, plus `helm template` of the umbrella with (a) the rendered demo values
(`.rendered/canvas-values.yaml`) and (b) chart defaults + `preqrequisitechecks.istio=false`, plus a read-only
look at the live `kind-oda-canvas` cluster. Items marked **[verified]** were observed; **[inferred]** were
deduced from files and need a run or operator-source check to confirm. Helm in this environment is v4.3.1.

---

## 1. Umbrella chart `canvas-oda` vs. the charts in `charts/`

### 1.1 Charts present in `charts/` but NOT wired into the umbrella [verified]

| Chart | Version | What it is | Consequence for any umbrella-based install (incl. `make up`) |
|---|---|---|---|
| `credentialsmanagement-operator` | 1.0.1 | Simulates handing client credentials/secrets to components from Keycloak | Never deployed. Yet the umbrella's Keycloak realm still creates the `credentialsmanagement-operator` client and its `view-clients` service account. Must be installed separately for that function. |
| `carbon-management-operator` | 1.0.0 | Carbon-aware management function | Separate install only |
| `carbon-intensity-service` | 1.0.1 | Carbon intensity data service | Separate install only |
| `apigee-gateway` | 1.0.0 | Apigee API operator | Not selectable. The schema only allows Istio, Kong or APISIX. |
| `observability-stack` | 0.1.0 | Prometheus, OpenTelemetry collector, Jaeger | Separate install. Its three dependencies show `missing` locally. `make up` does not use it (see 2.9). |
| `experimental/oauth2-envoyfilter-operator` | n/a | Experimental | Not installable via the umbrella |

### 1.2 Naming and wiring quirks inside the umbrella [verified]

- Directory and dependency names differ: `kong-gateway/` is dependency `api-operator-kong`, and `apisix-gateway/`
  is `api-operator-apisix`. The enable switches are `kong-gateway-install.enabled` and
  `apisix-gateway-install.enabled`, which do not match the dependency name. Sub-chart values go under
  `api-operator-kong.*` / `api-operator-apisix.*`. This trips anyone writing values by hand.
- All 19 dependency versions in `Chart.yaml` match the `Chart.yaml` of the sibling charts. No version skew.
- Dependencies installed **unconditionally** (no `condition:`): `cert-manager-init`, `component-operator`,
  `identityconfig-operator-keycloak`, `secretsmanagement-operator`, `oda-webhook`.
  `secretsmanagement-operator` stays installed when `canvas-vault.enabled=false`. The installation README warns this
  gives `CreateContainerConfigError` on `canvas-smanop`.
- The schema (`values.schema.json`) enforces exactly one gateway operator and exactly one dependency-management
  operator. It does not enforce cross-chart consistency, e.g. `dependentapi-simple-operator.componentRegistryUrl`
  set while `component-registry.enabled=false` (see 2.5).
- `charts/canvas-oda/README.md` is stale (shows 1.2.3 and old sub-chart versions), so it is not a reliable value reference.
- `oda-crds` has 8 CRDs (availabilitypolicy, component, dependentapi, exposedapi, identityconfig,
  publishednotification, secretsmanagement, subscribednotification). There is no model-gateway CRD, as the README of
  this repo already notes.
- Dependency tarballs (`charts/*/charts/*.tgz`) are gitignored in the Canvas repo. A fresh clone needs
  `helm dependency update` before anything renders (see 2.2).

### 1.3 Delta rendered by the demo profile vs. chart defaults [verified]

Disabled by `canvas-values.yaml.tpl`: `canvas-info-service` (+ MongoDB, PVC), `resource-inventory`,
`pdb-management-operator` (Deployment, PDB, serving Certificate, extra Mutating+Validating webhooks),
`component-registry` (+ PVC `canvas-compreg-data`). Active: `api-operator-istio` (switched to `ServiceMonitor`
metrics), `dependentapi-simple-operator`, `canvas-vault`, `secretsmanagement-operator`, Keycloak+Postgres,
cert-manager, `component-operator`, `identityconfig-operator-keycloak`, `oda-webhook`.

---

## 2. Installation README recommendations vs. `make up`

### 2.1 Conforms (no gap)

- Kubernetes 1.30 = max supported for component `v1` (`kindest/node:v1.30.0`). Kind is a listed, tested target.
- Istio via the documented Helm steps (base, istiod, gateway in `istio-ingress` with `istio-injection=enabled`, labels
  `app=istio-ingress`, `istio=ingressgateway`). The Makefile adds a version pin, `--wait` on all releases, and `--force-conflicts`.
- Canvas installed **without `--wait`**, with `--timeout` (15m), `--create-namespace`, in namespace `canvas`.
- Default Istio API operator, with Kong and APISIX off and the single-gateway rule respected.
- kind workarounds from the README: `preqrequisitechecks.istio=false`, and the `istio-ingress` status patched
  to `127.0.0.1` **before** any component is installed (ordering is correct, issue #621).
- `helm repo add` for jetstack and bitnami (plus istio, hashicorp, prometheus-community).
- Vault left enabled, so Secrets-Management works.

### 2.2 Gaps and deviations

| # | Topic | README recommends | `make up` does | Impact / status |
|---|---|---|---|---|
| 2.2 | Dependency preparation | `helm-resolve-deps` plugin | `helm dependency update` on 6 charts (`cert-manager-init apisix-gateway kong-gateway canvas-vault pdb-management-operator canvas-oda`), only when status is not `ok` | Works only if the needed repos exist. The `repos` target does **not** add `https://charts.konghq.com` or `https://charts.apiseven.com`, although `kong-gateway`/`apisix-gateway` depend on them. The tarballs are gitignored, so on a fresh checkout `helm dependency update` for those two charts fails with "no repository definition". It worked here only because the tarballs already existed. **[inferred]** from the gitignore + missing `repo add`. Fix: add the two repos, or drop those two charts from `CANVAS_DEPS` and build only what the demo needs. |
| 2.3 | Ingress reachability | Set the IP **and** `kubectl port-forward -n istio-ingress svc/istio-ingress 80:80 443:443`; for conformance use `docker0` (172.17.0.1) + `--address 0.0.0.0` (Linux) | Patches the IP to `127.0.0.1` only. No ingress port-forward target; `make port-forward` forwards the portal only | ExposedAPI URLs are populated but unreachable until the user forwards manually. The TMF CTK containers can't use `127.0.0.1`. The portal's conformance feature is therefore not functional out of the box **[inferred]**. Also, the status patch is lost if the Service is recreated. |
| 2.4 | Gateway API CRDs | Only for Kong, v1.0.0 | Always applies v1.2.1 | Justified (portal lists HTTPRoutes). Version differs from the README, and the CRDs are an extra cluster-wide dependency not part of the Canvas. |
| 2.5 | Component Registry vs. DependentAPI operator | The values comment says comment out `componentRegistryUrl` if the registry is not used | `component-registry.enabled=false` but `dependentapi-simple-operator` still gets `COMPONENT_REGISTRY_URL=http://canvas-compreg.canvas.svc.cluster.local` **[verified]** in rendered manifests and in the live Deployment `canvas-depapi-op`. The live operator also logs `CANVAS_INFO_ENDPOINT=http://info.canvas.svc.cluster.local` while `canvas-info-service` is off **[verified]** | Dangling endpoints. Whether the operator tolerates them (timeouts, failed DependentAPI resolution) needs the operator source or a test with a component that has dependent APIs. Fix: set `componentRegistryUrl: ""`/null in the template, or enable the registry. |
| 2.6 | Disabled optional services | Reference implementation includes info-service, resource-inventory, pdb operator, component-registry | All four off | Documented in this repo's README. Missing capabilities: TMF638 service inventory, TMF639 resource inventory, automatic PDBs, cross-canvas component discovery. Not a "full Canvas" (by design). The portal pages depending on them will be empty. |
| 2.7 | Hook images | n/a | README says "The kind node pulls nothing from the internet" | **[verified]** the chart's hook Jobs use `bitnamilegacy/kubectl:latest`. Kubernetes defaults to `imagePullPolicy: Always` for `:latest`, and `kind load` doesn't satisfy that. The statement in this README is therefore not strictly true, and in a restricted network those hooks (pre-install/delete, vault init) could fail. The preload script does load the image, so the failure only appears when the registry is unreachable. |
| 2.8 | Hardware | 4 CPU / 16 GB / **50 GB** storage for Canvas + tests | README quotes "4 CPU / 16 GB" only | Observability (kube-prometheus-stack, Langfuse with ClickHouse, Postgres, Redis, MinIO) is added on top of the Canvas minimum, so the stated figure is optimistic. Disk is not mentioned. |
| 2.9 | Observability | The Canvas ships `charts/observability-stack` (Prometheus, Grafana, Jaeger, OTel) | Uses kube-prometheus-stack 92.17.0 (the Canvas stack pins 55.5.0) + Langfuse; no Jaeger/OTel | Documented choice. Gap vs. Canvas: no tracing backend (Jaeger/OTel), so traces from components aren't collected. The Canvas observability tutorial's metric names also differ (see README "Checking component metrics"). |
| 2.10 | Third-party versions | Istio 1.16.1, Helm 3.10, cert-manager 1.20, Keycloak 20.0.3, Postgres 15.0.1 | Istio 1.26.3; local Helm v4.3.1; cert-manager 1.11.0 (the chart's own pin); Keycloak 20.0.5, Postgres 15.2.0 | The README table is itself inconsistent with the charts (cert-manager 1.20 vs 1.11.0 in `cert-manager-init`). The Makefile's `check` target does not verify the Helm version, and the README says "Helm 3.0+", so Helm 4 is untested territory. Istio 1.26 is far above the documented tested version. |
| 2.11 | Credentials | Chart defaults (`adpass`, `keycloakdbuser`) | Does not override them; `KC_ADMIN_PASSWORD` in `config.env` **must equal** the chart default | Works for a demo, but the "secret" is a published default and the Makefile cannot rotate it. Vault root token lives in `canvas-vault-hc-secrets`; `vault_skip_verify` is `"true"`. |
| 2.12 | Component namespaces | Operators monitor `components,odacompns-*` | Creates `components`, `canvas`, `temp-namespace` | `temp-namespace` is not in the operators' `monitoredNamespaces` and gets no `istio-injection` label (only `components` does). The portal's conformance RBAC targets it **[inferred]**: components deployed there would not be reconciled. |
| 2.13 | Permissions pre-check | `installation/precheck.sh` / `kubectl auth can-i ...` | Not run | Harmless on kind (admin), relevant if the Makefile is pointed at another cluster. The `cluster` target also hardcodes the kind context. |
| 2.14 | Post-install validation | Check `components.oda.tmforum.org` served versions; deploy a reference component | `canvas-verify` just lists CRDs/pods; `component-sample` is optional and outside `up` | `make up` can succeed without ever proving a component reaches `Complete`. The Makefile waits for Deployments `available` (excludes the openan-operator) and the Keycloak StatefulSet only. |
| 2.15 | Recovery / uninstall | Uninstall components first; delete stale Postgres PVC after a failed install | `make down` deletes the whole cluster; no `canvas-uninstall` target; stale-PVC failure (`password authentication failed for user "bn_keycloak"`) is not detected | Reinstalling into an existing cluster after a partial failure needs manual steps from the README troubleshooting. |
| 2.16 | Config drift | n/a | `KIND_NODE_IMAGE` in `config.mk` is not used by the Makefile; `kind-config.yaml` hardcodes `kindest/node:v1.30.0` and cluster name `oda-canvas` (must match `CLUSTER_NAME`) | Two sources of truth for the node image and cluster name. |
| 2.17 | Reproducibility | Published charts via `helm repo add oda-canvas` | Uses a local checkout (`CANVAS_SRC`, unpinned) | Behaviour depends on whatever commit is checked out (here a feature commit `b37bb155`). `make check` doesn't record or assert the Canvas revision. |

### 2.3 Items outside the comparison, noticed on the way

- The live cluster has `openan-operator` running in `canvas` (from the optional target), and `kubectl wait` in the
  `canvas` target correctly excludes it via a label selector.
- `portal-realm.sh` uses the admin REST API (not partial import) because of a Keycloak 20 NPE with groups. That is a
  sound workaround but adds an extra, non-Canvas change to the `odari` realm, so a Canvas upgrade that re-imports the
  realm could drop the portal clients **[inferred]**.

---

## 3. Suggested fixes (priority order)

1. Set `dependentapi-simple-operator.componentRegistryUrl` to empty (or enable `component-registry`) in
   `canvas-values.yaml.tpl`; check what the operator does with `CANVAS_INFO_ENDPOINT` while the info service is off.
2. Add `helm repo add kong https://charts.konghq.com` and `apiseven https://charts.apiseven.com`, or remove
   `kong-gateway`/`apisix-gateway` from `CANVAS_DEPS`. Reproduce on a clean checkout.
3. Add a `make ingress-forward` target (80/443, plus `--address 0.0.0.0` / `docker0` option for conformance) and
   document it.
4. Soften the "kind node pulls nothing" sentence, or override the hook image to a pinned tag in `global.hookImages`.
5. Align `temp-namespace` with `monitoredNamespaces` or remove it; label namespaces for Istio injection.
6. Make `canvas-verify` stronger (served CRD versions, a `make component-sample` smoke run) and record `CANVAS_SRC`
   git revision.
7. Document disk/RAM including observability; enforce a Helm version in `check`; remove the unused `KIND_NODE_IMAGE`.

## 4. Caveats

- Everything is based on local checkouts, not the published chart repo. The web-research workflow was not used, because
  the question compares local files. No external sources were consulted.
- Items marked [inferred] were not exercised. In particular 2.2 (fresh-clone dependency update), 2.3
  (conformance reachability), 2.5 (operator behaviour) and 2.12 (namespace reconciliation) need a run to confirm.
