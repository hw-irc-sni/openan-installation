# ODA Canvas deployment automation (local kind demo). Stages, in order (`make bootstrap-oda-canvas` runs all of them):
#   environment  pre-flight, kind cluster, Helm repos/dependencies, Istio, ingress address, Vault prerequisites
#                (installation README, "Environment installation" 1.-4.)
#   oda-canvas   the ODA Reference Implementation (charts/canvas-oda), Keycloak and Vault included (README 5.)
#   observability  charts/observability-stack (Prometheus, Grafana, OpenTelemetry, Jaeger) + Langfuse
#   canvas-portal  canvas-portal-v2 on top of the Canvas Keycloak
#   component-sample  a reference component that must reach "Complete"
include config.mk
ifeq ($(wildcard config.env),)
$(error config.env not found: run `cp config.env.example config.env` and set the passwords/secrets)
endif
include config.env
export

SHELL := bash
VALUES := .rendered/portal-values.yaml
CANVAS_VALUES := .rendered/canvas-values.yaml
OBS_VALUES := .rendered/observability-values.yaml
OBS_CHART := $(CANVAS_SRC)/charts/observability-stack
LANGFUSE_VARS := $${OBS_NS} $${LANGFUSE_IMAGE_TAG} $${LANGFUSE_POSTGRES_IMAGE} $${LANGFUSE_CLICKHOUSE_IMAGE} $${LANGFUSE_REDIS_IMAGE} $${LANGFUSE_MINIO_IMAGE} $${LANGFUSE_INIT_ORG} $${LANGFUSE_INIT_PROJECT} $${LANGFUSE_INIT_USER_EMAIL} $${LANGFUSE_LOCAL_PORT}
IMAGES := $(PORTAL_IMAGE_REPO):$(PORTAL_FRONTEND_TAG) $(PORTAL_IMAGE_REPO):$(PORTAL_BACKEND_TAG)
# Local charts whose dependencies must be built, deepest first (the umbrella needs its file:// sub-charts resolved).
# kong-gateway and apisix-gateway are not installed by the demo, but the umbrella cannot be packaged without them.
CANVAS_DEPS := cert-manager-init apisix-gateway kong-gateway canvas-vault pdb-management-operator canvas-oda
HELM_REPOS := istio=https://istio-release.storage.googleapis.com/charts \
              bitnami=https://charts.bitnami.com/bitnami \
              hashicorp=https://helm.releases.hashicorp.com \
              jetstack=https://charts.jetstack.io \
              kong=https://charts.konghq.com \
              apiseven=https://charts.apiseven.com \
              prometheus-community=https://prometheus-community.github.io/helm-charts \
              open-telemetry=https://open-telemetry.github.io/opentelemetry-helm-charts \
              jaegertracing=https://jaegertracing.github.io/helm-charts
# `--force-conflicts` (server-side apply) exists only in Helm 4.
HELM_MAJOR := $(shell helm version --template '{{.Version}}' 2>/dev/null | sed 's/^v//;s/\..*//')
HELM_FORCE := $(if $(filter 4,$(HELM_MAJOR)),--force-conflicts,)
INGRESS_KEY := $(shell echo '$(INGRESS_ADDRESS)' | grep -Eq '^[0-9.]+$$' && echo ip || echo hostname)

.PHONY: bootstrap-oda-canvas canvas-portal environment oda-canvas observability preflight check check-secrets check-portal \
        cluster repos deps istio ingress-address gateway-api-crds vault-prepare permissions \
        canvas oda-canvas canvas-verify images portal-realm portal verify component-sample component-verify \
        oda-canvas-port-forward oda-canvas-port-forward-stop openan-port-forward openan-port-forward-stop status down openan-operator-image openan-operator openan-sample-image openan-portal-route openan-sample openan-verify openan-clean bootstrap-openan

# The five stages one after the other (recursive make: the order must hold also under `make -j`).
bootstrap-oda-canvas: check-secrets check-portal
	$(MAKE) environment
	$(MAKE) oda-canvas
	$(MAKE) observability
	$(MAKE) canvas-portal
	$(MAKE) component-sample
	@echo; echo "ODA Canvas, observability stack, portal and sample component ready."
	@echo "Also: 'make status'. Forwarding all the UIs now (detached; stop with 'make oda-canvas-port-forward-stop')."
	$(MAKE) oda-canvas-port-forward

# ---- 1. environment: README "Environment installation" 1.-4. ------------------------------------------
environment:
	$(MAKE) preflight
	$(MAKE) cluster
	$(MAKE) deps
	$(MAKE) istio
	$(MAKE) ingress-address
	$(MAKE) gateway-api-crds
	$(MAKE) vault-prepare
	$(MAKE) permissions
	@echo "Environment ready: kind cluster $(CLUSTER_NAME), Istio $(ISTIO_VERSION), Helm repos/dependencies, Vault prerequisites."

# Host tools, versions, Docker and the Canvas checkout. Nothing is installed: missing tools are reported with a hint.
preflight check:
	@bash scripts/preflight.sh tools

check-secrets:
	@bash scripts/preflight.sh secrets KC_ADMIN_PASSWORD PORTAL_USER_PASSWORD PORTAL_ADMIN_CLIENT_SECRET GRAFANA_ADMIN_PASSWORD LANGFUSE_INIT_USER_PASSWORD

check-portal:
	@test -f $(PORTAL_SRC)/helm/canvas-portal/Chart.yaml || { echo "PORTAL_SRC wrong: $(PORTAL_SRC)"; exit 1; }

# README 1.: a Kubernetes distribution. kind, with the node image and cluster name from config.mk.
cluster:
	@mkdir -p .rendered
	@envsubst '$${CLUSTER_NAME} $${KIND_NODE_IMAGE}' < kind-config.yaml.tpl > .rendered/kind-config.yaml
	@kind get clusters | grep -qx $(CLUSTER_NAME) || kind create cluster --config .rendered/kind-config.yaml --wait 120s
	@kubectl config use-context kind-$(CLUSTER_NAME) >/dev/null

# README 2.: Helm repositories. `repo add` already fetches the index.
repos:
	@for r in $(HELM_REPOS); do helm repo add $${r%%=*} $${r#*=} --force-update >/dev/null || echo "WARN: cannot add Helm repo $${r%%=*}"; done

# README 2.: local chart dependencies (same order as the Canvas CI), for the Canvas and the observability stack.
# Only (re)built when a chart reports a non-"ok" status: a `dependency update` re-contacts every repo and costs ~3 min.
# `build` honours Chart.lock (keeps the Canvas checkout clean); `update` is the fallback when there is no/stale lock.
deps: repos
	@for d in $(addprefix $(CANVAS_SRC)/charts/,$(CANVAS_DEPS)) $(OBS_CHART); do \
	  if helm dependency list $$d 2>/dev/null | awk 'NR>1 && NF && $$NF!="ok"' | grep -q .; then \
	    helm dependency build $$d || helm dependency update $$d || exit 1; else echo "dependencies ok: $$(basename $$d)"; fi; done

# README 3.: Istio (base, istiod, ingress gateway in the injected namespace istio-ingress).
istio: repos
	bash scripts/preload-images.sh environment
	helm upgrade --install istio-base istio/base --version $(ISTIO_VERSION) -n istio-system --create-namespace $(HELM_FORCE) --wait
	helm upgrade --install istiod istio/istiod --version $(ISTIO_VERSION) -n istio-system $(HELM_FORCE) --wait
	@kubectl create namespace istio-ingress --dry-run=client -o yaml | kubectl apply -f -
	@kubectl label namespace istio-ingress istio-injection=enabled --overwrite
	helm upgrade --install istio-ingress istio/gateway --version $(ISTIO_VERSION) -n istio-ingress \
	  --set labels.app=istio-ingress --set labels.istio=ingressgateway --skip-schema-validation $(HELM_FORCE) --wait

# kind has no LoadBalancer: give istio-ingress an address so ExposedAPI URLs get populated (installation README,
# "Local clusters (kind)"). Idempotent; rerun after the Service is recreated. Needed BEFORE components are installed.
ingress-address:
	kubectl patch svc istio-ingress -n istio-ingress --subresource=status --type=merge \
	  -p '{"status":{"loadBalancer":{"ingress":[{"$(INGRESS_KEY)":"$(INGRESS_ADDRESS)"}]}}}'

# Gateway API CRDs: not needed by Istio here, but the portal's gateway inventory lists HTTPRoutes.
gateway-api-crds:
	kubectl apply -f $(GATEWAY_API_CRDS)

# README 4.: HashiCorp Vault is a sub-chart of canvas-oda (namespace canvas-vault, installed by `make oda-canvas`).
# This only makes sure it can be installed: hashicorp repo, canvas-vault dependencies, not switched off in the values.
vault-prepare:
	@helm repo list 2>/dev/null | grep -q helm.releases.hashicorp.com || { echo "Helm repo hashicorp missing (run make repos)"; exit 1; }
	@helm dependency list $(CANVAS_SRC)/charts/canvas-vault | awk 'NR>1 && NF && $$NF!="ok"' | grep -q . \
	  && { echo "canvas-vault dependencies not built (run make deps)"; exit 1; } || true
	@if awk '/^canvas-vault:/{f=1;next} /^[^ #]/{f=0} f && /enabled: *false/{b=1} END{exit !b}' canvas-values.yaml.tpl; then \
	  echo "WARNING: canvas-vault is disabled in canvas-values.yaml.tpl: components requesting Secrets-Management stay InProgress-SecretsConfig"; \
	else echo "Vault prerequisites ok (canvas-vault enabled, installed by 'make oda-canvas')"; fi

# README 1.: permissions needed by the install (installation/precheck.sh, but failing on "no"). Run last, so the
# CRD-backed resources (gateways, certificates, issuers) are known to the API server.
permissions:
	@fail=0; \
	for r in namespaces customresourcedefinitions clusterroles clusterrolebindings mutatingwebhookconfigurations validatingwebhookconfigurations clusterissuers; do \
	  [ "$$(kubectl auth can-i create $$r --all-namespaces 2>/dev/null)" = yes ] || { echo "cannot create $$r (cluster-wide)"; fail=1; }; done; \
	for r in serviceaccounts secrets configmaps roles rolebindings services deployments statefulsets gateways jobs certificates issuers; do \
	  [ "$$(kubectl auth can-i create $$r 2>/dev/null)" = yes ] || { echo "cannot create $$r"; fail=1; }; done; \
	[ $$fail = 0 ] && echo "Kubernetes permissions ok" || exit 1

# ---- 2. oda-canvas: README 5. ---------------------------------------------------------------------------
# Do NOT use --wait: the post-install hooks (certificates, Vault init) would deadlock.
canvas: oda-canvas
oda-canvas:
	@kubectl -n istio-system get deploy istiod >/dev/null 2>&1 || { echo "Istio not found: run 'make environment' first"; exit 1; }
	@mkdir -p .rendered
	@envsubst < canvas-values.yaml.tpl > $(CANVAS_VALUES)
	$(MAKE) deps
	bash scripts/preload-images.sh canvas
	helm upgrade --install $(CANVAS_RELEASE) $(CANVAS_SRC)/charts/canvas-oda -n $(CANVAS_NS) \
	  --create-namespace -f $(CANVAS_VALUES) --timeout 15m
	@# Component namespaces: the Canvas chart creates 'components' (injected); make sure all of them exist and are injected.
	@for ns in $$(echo $(COMPONENT_NAMESPACES) | tr , ' '); do \
	  kubectl get namespace $$ns >/dev/null 2>&1 || kubectl create namespace $$ns; \
	  	  kubectl label namespace $$ns istio-injection=enabled --overwrite; done
	kubectl -n $(CANVAS_NS) rollout status statefulset/canvas-keycloak --timeout=15m
	@# Canvas-owned deployments only: the OpenAN operator (installed separately, possibly into this
	@# namespace) must not gate the Canvas readiness.
	kubectl -n $(CANVAS_NS) wait --for=condition=available deploy -l 'app.kubernetes.io/name!=openan-operator' --timeout=15m
	@# The Vault post-install hook can fail once while cert-manager is still issuing canvasvault-tls
	@# (its Job retries and completes); remove the failed attempt so `kubectl get pods` is clean.
	@kubectl -n canvas-vault delete pod --field-selector=status.phase=Failed --ignore-not-found
	$(MAKE) canvas-verify

canvas-verify:
	@rev=$$(git -C $(CANVAS_SRC) rev-parse --short=12 HEAD 2>/dev/null || echo unknown); \
	  echo "Canvas chart: $$(helm list -n $(CANVAS_NS) -f '^$(CANVAS_RELEASE)$$' -o json | python3 -c 'import sys,json;print((json.load(sys.stdin) or [{}])[0].get("chart","not installed"))') from $(CANVAS_SRC) @ $$rev"
	@kubectl get crd | grep oda.tmforum.org
	@n=$$(kubectl get crd -o name | grep -c oda.tmforum.org); [ $$n -ge 8 ] || { echo "ERROR: $$n oda.tmforum.org CRDs, expected 8"; exit 1; }
	@served=$$(kubectl get crd components.oda.tmforum.org -o jsonpath='{.spec.versions[?(@.served==true)].name}'); \
	  echo "Component API versions served: $$served"; \
	  case " $$served " in *" v1 "*) ;; *) echo "ERROR: Component v1 is not served"; exit 1;; esac
	@kubectl get svc istio-ingress -n istio-ingress -o jsonpath='{.status.loadBalancer.ingress[0]}' | grep -q . \
	  || { echo "ERROR: istio-ingress has no address (run make ingress-address): ExposedAPI URLs would stay empty"; exit 1; }
	@kubectl -n $(CANVAS_NS) get pods

# ---- 3. observability ---------------------------------------------------------------------------------------
# The Canvas charts/observability-stack (kube-prometheus-stack as "prometheus", OpenTelemetry collector, Jaeger) as
# release $(OBS_RELEASE) in $(OBS_NS), which is where the portal's /observability page looks for Prometheus and
# Grafana, plus Langfuse (not part of the Canvas stack). Runs after the Canvas, whose api-operator-istio creates the
# ServiceMonitors that this Prometheus scrapes.
observability:
	@bash scripts/preflight.sh secrets GRAFANA_ADMIN_PASSWORD LANGFUSE_INIT_USER_PASSWORD
	@kubectl cluster-info >/dev/null 2>&1 || { echo "cluster not reachable: run 'make environment' first"; exit 1; }
	@if helm list -n $(OBS_NS) -f '^$(OBS_RELEASE)$$' -o json | grep -q '"chart":"kube-prometheus-stack-'; then \
	  echo "ERROR: release $(OBS_RELEASE) in $(OBS_NS) is a plain kube-prometheus-stack from an earlier version of this Makefile."; \
	  echo "       Replace it: helm uninstall $(OBS_RELEASE) -n $(OBS_NS)   (then rerun make observability)"; exit 1; fi
	@mkdir -p .rendered
	@envsubst '$${GRAFANA_ADMIN_PASSWORD} $${JAEGER_IMAGE_TAG} $${OBS_RELEASE} $${OBS_NS}' < observability-values.yaml.tpl > $(OBS_VALUES)
	$(MAKE) deps
	bash scripts/preload-images.sh observability
	@kubectl create namespace $(OBS_NS) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install $(OBS_RELEASE) $(OBS_CHART) -n $(OBS_NS) -f $(OBS_VALUES) --wait --timeout 10m
	@# Langfuse secrets: random, created once (never in git); the init user password comes from config.env.
	@kubectl -n $(OBS_NS) get secret langfuse-secrets >/dev/null 2>&1 || kubectl -n $(OBS_NS) create secret generic langfuse-secrets \
	  --from-literal=salt="$$(openssl rand -base64 32)" --from-literal=encryption-key="$$(openssl rand -hex 32)" \
	  --from-literal=nextauth-secret="$$(openssl rand -base64 32)" --from-literal=postgres-password="$$(openssl rand -hex 16)" \
	  --from-literal=clickhouse-password="$$(openssl rand -hex 16)" --from-literal=redis-password="$$(openssl rand -hex 16)" \
	  --from-literal=minio-password="$$(openssl rand -hex 16)"
	@kubectl -n $(OBS_NS) create secret generic langfuse-init --from-literal=user-password="$$LANGFUSE_INIT_USER_PASSWORD" \
	  --dry-run=client -o yaml | kubectl apply -f -
	@envsubst '$(LANGFUSE_VARS)' < manifests/langfuse.yaml | kubectl apply -f -
	kubectl -n $(OBS_NS) rollout status statefulset/langfuse-postgres statefulset/langfuse-clickhouse statefulset/langfuse-minio deploy/langfuse-redis --timeout=10m
	kubectl -n $(OBS_NS) rollout status deploy/langfuse-web deploy/langfuse-worker --timeout=10m
	@kubectl -n $(OBS_NS) get pods
	@echo "Traces: components send OTLP/HTTP to http://$(OBS_RELEASE)-opentelemetry-collector.$(OBS_NS).svc.cluster.local:4318/v1/traces (Jaeger UI: make oda-canvas-port-forward)"

# ---- 4. canvas-portal: canvas-portal-v2 on top of the Canvas Keycloak ---------------------------------------------------------------------
# Portal images, realm setup, Helm release, health checks (needs the Canvas and the observability stack).
canvas-portal: check-portal
	@kubectl -n $(CANVAS_NS) get statefulset canvas-keycloak >/dev/null 2>&1 || { echo "Canvas not found: run 'make oda-canvas' first"; exit 1; }
	$(MAKE) images
	$(MAKE) portal-realm
	$(MAKE) portal
	$(MAKE) verify

# Portal images (built from source, the published tags do not exist) + the hook job image.
images: check-portal
ifeq ($(PORTAL_BUILD),true)
	docker build -t $(PORTAL_IMAGE_REPO):$(PORTAL_FRONTEND_TAG) $(PORTAL_SRC)/frontend
	docker build -t $(PORTAL_IMAGE_REPO):$(PORTAL_BACKEND_TAG) $(PORTAL_SRC)/backend
else
	@for i in $(IMAGES); do docker image inspect $$i >/dev/null 2>&1 || docker pull $$i; done
endif
	@docker image inspect $(INIT_JOB_IMAGE) >/dev/null 2>&1 || docker pull -q $(INIT_JOB_IMAGE)
	@for i in $(IMAGES) $(INIT_JOB_IMAGE); do kind load docker-image $$i --name $(CLUSTER_NAME); done

# Portal clients, canvas-admin group and demo user in the Canvas Keycloak realm.
portal-realm:
	bash scripts/portal-realm.sh

portal:
	@mkdir -p .rendered
	@envsubst < portal-values.yaml.tpl > $(VALUES)
	helm upgrade --install $(RELEASE) $(PORTAL_SRC)/helm/canvas-portal \
	  -n $(PORTAL_NS) --create-namespace -f $(VALUES) --wait --timeout 5m

verify:
	@kubectl -n $(PORTAL_NS) rollout status deploy --timeout=120s
	@kubectl -n $(PORTAL_NS) exec deploy/$(RELEASE)-backend -- \
	  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/health').read().decode())"
	@# /observability: the portal must see Langfuse, Grafana and Prometheus as healthy.
	@kubectl -n $(PORTAL_NS) exec deploy/$(RELEASE)-backend -- env PU="$(PORTAL_USER)" PP="$$PORTAL_USER_PASSWORD" \
	  python -c "$$(cat scripts/verify-observability.py)"

# Optional smoke test: a reference component must reach "Complete", so /components and /exposedapis have data.
component-sample:
	helm repo add oda-components https://tmforum-oda.github.io/reference-example-components/ --force-update
	helm template pc oda-components/productcatalog -n components \
	  | grep -hoE '^\s*-?\s*image:\s*"?[^"[:space:]]+' | sed -E 's/.*image:\s*"?//' | sort -u \
	  | while read i; do docker image inspect $$i >/dev/null 2>&1 || docker pull -q $$i; kind load docker-image $$i --name $(CLUSTER_NAME); done
	helm upgrade --install pc oda-components/productcatalog -n components
	$(MAKE) component-verify

component-verify:
	@for i in $$(seq 1 60); do \
	  st=$$(kubectl -n components get components -o jsonpath="{range .items[*]}{.status['summary/status'].deployment_status}{'\n'}{end}" 2>/dev/null | sort -u | tr '\n' ' '); \
	  [ "$$st" = "Complete " ] && { echo "components: Complete"; kubectl -n components get components,exposedapis; exit 0; }; \
	  echo "component status: $${st:-none yet} ($$i/60)"; sleep 10; done; \
	  kubectl -n components get components,exposedapis; echo "components did not all reach Complete"; exit 1

# Every UI of the demo on FORWARD_ADDRESS (default 0.0.0.0: any IP of this host). Ctrl-C stops all the forwards.
# Ingress ports < 1024 may need privileges: INGRESS_HTTP_PORT=8081 INGRESS_HTTPS_PORT=8443. For the conformance CTK
# containers use an address they can reach: INGRESS_ADDRESS=172.17.0.1 (Docker Desktop: host.docker.internal).
# Detached: each forward is a systemd user service (scripts/port-forward.sh), so they outlive the launching terminal and are
# restarted if kubectl exits. Stop with `make oda-canvas-port-forward-stop`. The OpenAN portal has its own target (openan-port-forward).
PF := bash scripts/port-forward.sh
PF_CTX := kind-$(CLUSTER_NAME)
oda-canvas-port-forward: ingress-address
	@h=$$(hostname -I 2>/dev/null | awk '{print $$1}'); h=$${h:-<host-ip>}; \
	echo "Listening on $(FORWARD_ADDRESS); replace localhost with any host address (e.g. $$h)"; \
	echo "Portal     http://localhost:$(LOCAL_PORT)    (login: $(PORTAL_USER) / PORTAL_USER_PASSWORD in config.env)"; \
	echo "Grafana    http://localhost:$(GRAFANA_LOCAL_PORT)    (admin / GRAFANA_ADMIN_PASSWORD in config.env)"; \
	echo "Prometheus http://localhost:$(PROMETHEUS_LOCAL_PORT)"; \
	echo "Jaeger     http://localhost:$(JAEGER_LOCAL_PORT)"; \
	echo "Langfuse   http://localhost:$(LANGFUSE_LOCAL_PORT)    ($(LANGFUSE_INIT_USER_EMAIL) / LANGFUSE_INIT_USER_PASSWORD in config.env)"; \
	echo "ExposedAPIs http://$(INGRESS_ADDRESS):$(INGRESS_HTTP_PORT) (https :$(INGRESS_HTTPS_PORT))"; \
	$(PF) start canvas $(PF_CTX) $(FORWARD_ADDRESS) \
	  $(PORTAL_NS)/$(RELEASE)-frontend=$(LOCAL_PORT):80 \
	  $(OBS_NS)/$(OBS_RELEASE)-grafana=$(GRAFANA_LOCAL_PORT):80 \
	  $(OBS_NS)/$(OBS_RELEASE)-kube-prometheus-prometheus=$(PROMETHEUS_LOCAL_PORT):9090 \
	  $(OBS_NS)/$(OBS_RELEASE)-jaeger-query=$(JAEGER_LOCAL_PORT):16686 \
	  $(OBS_NS)/langfuse-web=$(LANGFUSE_LOCAL_PORT):3000 \
	  istio-ingress/istio-ingress=$(INGRESS_HTTP_PORT):80,$(INGRESS_HTTPS_PORT):443

oda-canvas-port-forward-stop:
	@$(PF) stop canvas

# OpenAN portal (namespace components), independent of the Canvas forwards: it may be deployed at a different stage.
openan-port-forward:
	@kubectl -n $(COMPONENTS_NS) get svc openan-frontend-frontend >/dev/null 2>&1 || { echo "OpenAN portal not deployed: run 'make openan-operator' first"; exit 1; }
	@echo "OpenAN portal http://localhost:$(OPENAN_PORTAL_LOCAL_PORT)"
	@$(PF) start openan $(PF_CTX) $(FORWARD_ADDRESS) $(COMPONENTS_NS)/openan-frontend-frontend=$(OPENAN_PORTAL_LOCAL_PORT):8080

openan-port-forward-stop:
	@$(PF) stop openan

status:
	@kubectl get pods -A | grep -E "$(PORTAL_NS)|$(CANVAS_NS)|$(COMPONENTS_NS)|$(OBS_NS)|istio|NAMESPACE"

down:
	kind delete cluster --name $(CLUSTER_NAME)
	rm -rf .rendered

# ---- OpenAN operator -------------------------------------------------------------------------------
# Not part of `make bootstrap-oda-canvas`: build/load images first (`make openan-operator-image`), then `make openan-operator`.
OPENAN_ROOT := $(abspath ..)
OPENAN_OC_IMAGE := ghcr.io/project-openan/orchestration-center:$(OPENAN_OC_TAG)
OPENAN_FE_IMAGE := ghcr.io/project-openan/openan-portal:$(OPENAN_FE_TAG)
OPENAN_IMAGES := ghcr.io/project-openan/registry-center:$(OPENAN_RC_TAG) $(OPENAN_OC_IMAGE) $(OPENAN_FE_IMAGE)

openan-operator-image:
	bash $(OPENAN_ROOT)/containerized/build/build-operator.sh --image $(OPENAN_OPERATOR_IMAGE) --tag $(OPENAN_OPERATOR_TAG)
	docker image inspect $(OPENAN_OC_IMAGE) >/dev/null 2>&1 || docker build -t $(OPENAN_OC_IMAGE) $(OPENAN_OC_SRC)
	@# default buildx driver: the docker-container driver does not use the Docker daemon proxy
	docker image inspect $(OPENAN_FE_IMAGE) >/dev/null 2>&1 || \
	  (cd $(OPENAN_ROOT)/containerized/build/portal && BUILDER=default PUSH=0 PLATFORMS=linux/amd64 TAG=$(OPENAN_FE_TAG) bash build-portal-image.sh)
	@docker image inspect ghcr.io/project-openan/registry-center:$(OPENAN_RC_TAG) >/dev/null 2>&1 || docker pull ghcr.io/project-openan/registry-center:$(OPENAN_RC_TAG)
	@for i in $(OPENAN_OPERATOR_IMAGE):$(OPENAN_OPERATOR_TAG) $(OPENAN_IMAGES); do \
	  kind load docker-image $$i --name $(CLUSTER_NAME); done

openan-operator:
	@# Helm only installs crds/ on first install, so apply the CRD explicitly (idempotent).
	kubectl apply -f $(OPENAN_ROOT)/operator/openan-operator/crd/openan-crd.yaml
	helm upgrade --install openan-operator $(OPENAN_ROOT)/charts/openan-operator -n $(OPENAN_OPERATOR_NS) --create-namespace \
	  --set deployment.image=$(OPENAN_OPERATOR_IMAGE) --set deployment.version=$(OPENAN_OPERATOR_TAG) \
	  --set deployment.monitoredNamespaces='$(OPENAN_NS)' --wait --timeout 3m
	@# `kind load` replaces the image under an unchanged tag: restart so the new build is the one running.
	kubectl -n $(OPENAN_OPERATOR_NS) rollout restart deploy/openan-operator
	kubectl -n $(OPENAN_OPERATOR_NS) rollout status deploy/openan-operator --timeout=3m
	@mkdir -p .rendered
	@envsubst < manifests/openan/openan-cr.yaml.tpl > .rendered/openan-cr.yaml
	kubectl apply -f .rendered/openan-cr.yaml
	kubectl -n $(OPENAN_NS) wait --for=condition=Ready openan/openan --timeout=10m

# Portal on the Istio gateway at "/". Istio orders the VirtualServices of host "*" by creation time, so this catch-all must
# be the newest: delete and re-create it (a plain apply keeps the old timestamp) whenever a Component's route is added.
openan-portal-route:
	kubectl delete -f manifests/openan/portal-virtualservice.yaml --ignore-not-found
	kubectl apply -f manifests/openan/portal-virtualservice.yaml

# Sample-A2A-T-Agent image (a2a-sdk + a2a-t-sdk, answers with the LLM): built by the Docker daemon (which honours its proxy
# settings) and loaded into kind, whose nodes cannot pull.
openan-sample-image:
	docker build -t $(OPENAN_SAMPLE_AGENT_IMAGE) sample-agent
	kind load docker-image $(OPENAN_SAMPLE_AGENT_IMAGE) --name $(CLUSTER_NAME)

# A sample a2a Component: its Agent Card must show up in the Registry Center.
openan-sample: openan-sample-image
	@mkdir -p .rendered
	@envsubst '$${OPENAN_SAMPLE_AGENT_IMAGE} $${OPENAN_LLM_BASE_URL} $${OPENAN_LLM_MODEL}' < manifests/openan/sample-a2a-agent.yaml.tpl > .rendered/sample-a2a-agent.yaml
	kubectl apply -f .rendered/sample-a2a-agent.yaml
	$(MAKE) openan-verify

openan-verify:
	@# exec into the Registry pod (python + network access to itself); a throwaway `kubectl run` pod would get an Istio sidecar and never exit.
	@TOKEN=$$(kubectl -n $(OPENAN_NS) get secret openan-operator-token -o jsonpath='{.data.token}' | base64 -d); \
	for i in $$(seq 1 40); do \
	  kubectl -n $(OPENAN_NS) exec deploy/openan-registry-center-registry-center -c registry-center -- python -c "import urllib.request as u;r=u.Request('http://127.0.0.1:5000/rest/v1/registry-center/agent-cards',headers={'Authorization':'Bearer $$TOKEN'});print(u.urlopen(r).read().decode())" 2>/dev/null | grep -q "Sample-A2A-T-Agent" && { echo "agent card registered"; exit 0; }; \
	  sleep 15; done; echo "agent card NOT found in the Registry Center"; exit 1

# Remove everything the openan-* targets created in the cluster, so a new OpenAN deployment can start from scratch. Order
# matters: the operator must still run while the sample card and the OpenAN CR are deleted (its finalizers remove the
# Registry card, uninstall the Registry/Orchestration/portal releases and delete their data PVCs).
#   1. the detached port-forward           4. the OpenAN CR (-> the three component releases, PVCs, Secrets, trust ConfigMap)
#   2. the portal route                    5. the operator release and the OpenAN CRD
#   3. the sample agent Component          6. leftovers by label, then the `openan` Helm repo (client side, if present)
# Not touched: the Canvas, the namespaces (components and the operator's namespace are shared with the Canvas), the
# `pc` reference component, and local Docker images / images already loaded into kind (not Kubernetes resources).
OPENAN_HELM_REPO := openan
openan-clean:
	-@$(PF) stop openan
	kubectl delete -f manifests/openan/portal-virtualservice.yaml --ignore-not-found
	kubectl delete -f manifests/openan/sample-a2a-agent.yaml.tpl --ignore-not-found --timeout=3m
	@# The operator's finalizers do the teardown: without a running operator this wait times out instead of leaking releases.
	kubectl -n $(OPENAN_NS) delete openan --all --ignore-not-found --timeout=5m \
	  || { echo "ERROR: the OpenAN CR was not removed (is the operator running in $(OPENAN_OPERATOR_NS)?)"; exit 1; }
	helm uninstall openan-operator -n $(OPENAN_OPERATOR_NS) --ignore-not-found
	kubectl delete crd openans.openan.dev --ignore-not-found
	@# Leftovers (e.g. data kept by retainData): generated Secrets/ConfigMaps and the component data PVCs.
	kubectl -n $(OPENAN_NS) delete secret,configmap -l app.kubernetes.io/managed-by=openan-operator --ignore-not-found
	kubectl -n $(OPENAN_NS) delete pvc -l 'app.kubernetes.io/instance in (openan-registry-center,openan-orchestration-center,openan-frontend)' --ignore-not-found
	@if helm repo list 2>/dev/null | awk 'NR>1{print $$1}' | grep -qx '$(OPENAN_HELM_REPO)'; then \
	  helm repo remove $(OPENAN_HELM_REPO); else echo "Helm repo $(OPENAN_HELM_REPO) not added"; fi
	@echo "OpenAN removed. Start again with: make bootstrap-openan"

# The whole OpenAN start sequence, on top of a running Canvas (`make bootstrap-oda-canvas`). Order matters:
#   images -> operator + OpenAN CR (Registry Center, Orchestration Center, portal) -> sample agent (waits for its card) ->
#   portal route LAST (a catch-all "/" on the Istio gateway must be the newest route) -> detached portal port-forward.
# Undo with `make openan-clean`. The gateway route is reachable through `make oda-canvas-port-forward` (ingress ports).
bootstrap-openan:
	@kubectl get crd components.oda.tmforum.org >/dev/null 2>&1 && kubectl -n $(CANVAS_NS) get statefulset canvas-keycloak >/dev/null 2>&1 \
	  || { echo "Canvas not found: run 'make bootstrap-oda-canvas' (or 'make oda-canvas') first"; exit 1; }
	$(MAKE) openan-operator-image
	$(MAKE) openan-operator
	$(MAKE) openan-sample
	$(MAKE) openan-portal-route
	@echo; echo "OpenAN ready: operator, Registry Center, Orchestration Center, portal and Sample-A2A-T-Agent."
	@echo "Portal through the gateway: https://<host>:$(INGRESS_HTTPS_PORT)/ (needs 'make oda-canvas-port-forward'); direct forward below."
	$(MAKE) openan-port-forward
