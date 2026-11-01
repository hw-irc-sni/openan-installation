# ODA Canvas deployment automation.
#   1. kind cluster (dev / quick demo, Kubernetes 1.30)
#   2. Istio (gateway prerequisite of the Canvas)
#   3. ODA Canvas (charts/canvas-oda): CRDs, operators and the bundled Keycloak (realm odari)
#   4. canvas-portal-v2 on top of the Canvas Keycloak
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
LANGFUSE_VARS := $${OBS_NS} $${LANGFUSE_IMAGE_TAG} $${LANGFUSE_POSTGRES_IMAGE} $${LANGFUSE_CLICKHOUSE_IMAGE} $${LANGFUSE_REDIS_IMAGE} $${LANGFUSE_MINIO_IMAGE} $${LANGFUSE_INIT_ORG} $${LANGFUSE_INIT_PROJECT} $${LANGFUSE_INIT_USER_EMAIL} $${LANGFUSE_LOCAL_PORT}
IMAGES := $(PORTAL_IMAGE_REPO):$(PORTAL_FRONTEND_TAG) $(PORTAL_IMAGE_REPO):$(PORTAL_BACKEND_TAG)
CANVAS_DEPS := cert-manager-init apisix-gateway kong-gateway canvas-vault pdb-management-operator canvas-oda

.PHONY: up down check cluster images repos canvas-images istio ingress-address gateway-api-crds canvas canvas-verify \
        observability observability-forward portal-realm portal verify component-sample port-forward status

up: check cluster images repos canvas-images istio ingress-address gateway-api-crds canvas observability portal-realm portal verify
	@echo; echo "Portal ready. Run 'make port-forward' then open http://localhost:$(LOCAL_PORT)"
	@echo "Login: $(PORTAL_USER) / (PORTAL_USER_PASSWORD in config.env)"

check:
	@for t in docker kind kubectl helm envsubst python3 curl; do command -v $$t >/dev/null || { echo "missing: $$t"; exit 1; }; done
	@docker info >/dev/null 2>&1 || { echo "docker daemon not reachable"; exit 1; }
	@if [ -n "$$https_proxy$$HTTPS_PROXY" ] && ! docker info 2>/dev/null | grep -q "HTTPS Proxy"; then \
	  echo "WARNING: your shell uses a proxy but the Docker daemon has none; image pulls/builds may"; \
	  echo "         fail with 'no route to host'. See README 'Network / proxy troubleshooting'."; fi
	@test -f $(PORTAL_SRC)/helm/canvas-portal/Chart.yaml || { echo "PORTAL_SRC wrong: $(PORTAL_SRC)"; exit 1; }
	@test -f $(CANVAS_SRC)/charts/canvas-oda/Chart.yaml || { echo "CANVAS_SRC wrong: $(CANVAS_SRC)"; exit 1; }

cluster:
	@kind get clusters | grep -qx $(CLUSTER_NAME) || kind create cluster --config kind-config.yaml --wait 120s
	@kubectl config use-context kind-$(CLUSTER_NAME) >/dev/null

# Portal images (built from source, the published tags do not exist) + the hook job image.
images:
ifeq ($(PORTAL_BUILD),true)
	docker build -t $(PORTAL_IMAGE_REPO):$(PORTAL_FRONTEND_TAG) $(PORTAL_SRC)/frontend
	docker build -t $(PORTAL_IMAGE_REPO):$(PORTAL_BACKEND_TAG) $(PORTAL_SRC)/backend
else
	@for i in $(IMAGES); do docker image inspect $$i >/dev/null 2>&1 || docker pull $$i; done
endif
	@docker image inspect $(INIT_JOB_IMAGE) >/dev/null 2>&1 || docker pull -q $(INIT_JOB_IMAGE)
	@for i in $(IMAGES) $(INIT_JOB_IMAGE); do kind load docker-image $$i --name $(CLUSTER_NAME); done

# Helm repos and local chart dependencies (same order as the Canvas CI). `repo add` already fetches
# the index, and dependencies are only (re)downloaded when a chart reports a non-"ok" status: a
# `dependency update` re-contacts every repo (also unrelated ones in your Helm config) and costs ~3 min.
repos:
	@mkdir -p .rendered
	@envsubst < canvas-values.yaml.tpl > $(CANVAS_VALUES)
	helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
	helm repo add bitnami https://charts.bitnami.com/bitnami --force-update
	helm repo add hashicorp https://helm.releases.hashicorp.com --force-update
	helm repo add jetstack https://charts.jetstack.io --force-update
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
	@envsubst < observability-values.yaml.tpl > $(OBS_VALUES)
	@for c in $(CANVAS_DEPS); do d=$(CANVAS_SRC)/charts/$$c; \
	  if helm dependency list $$d 2>/dev/null | awk 'NR>1 && NF && $$NF!="ok"' | grep -q .; then \
	    helm dependency update $$d; else echo "dependencies ok: $$c"; fi; done

# Render Istio + Canvas, then pull and `kind load` every image.
canvas-images:
	bash scripts/preload-images.sh

istio:
	helm upgrade --install istio-base istio/base --version $(ISTIO_VERSION) -n istio-system --create-namespace --wait
	helm upgrade --install istiod istio/istiod --version $(ISTIO_VERSION) -n istio-system --wait
	@kubectl create namespace istio-ingress --dry-run=client -o yaml | kubectl apply -f -
	@kubectl label namespace istio-ingress istio-injection=enabled --overwrite
	helm upgrade --install istio-ingress istio/gateway --version $(ISTIO_VERSION) -n istio-ingress \
	  --set labels.app=istio-ingress --set labels.istio=ingressgateway --skip-schema-validation --wait

# kind has no LoadBalancer: give istio-ingress an address so ExposedAPI URLs get populated.
ingress-address:
	kubectl patch svc istio-ingress -n istio-ingress --subresource=status --type=merge \
	  -p '{"status":{"loadBalancer":{"ingress":[{"ip":"127.0.0.1"}]}}}'

# Gateway API CRDs: not needed by Istio here, but the portal's gateway inventory lists HTTPRoutes.
gateway-api-crds:
	kubectl apply -f $(GATEWAY_API_CRDS)

# Do NOT use --wait: the post-install hooks (certificates, Vault init) would deadlock.
canvas:
	helm upgrade --install $(CANVAS_RELEASE) $(CANVAS_SRC)/charts/canvas-oda -n $(CANVAS_NS) \
	  --create-namespace -f $(CANVAS_VALUES) --timeout 15m
	@for ns in $$(echo $(CANVAS_NAMESPACES) | tr , ' '); do \
	  kubectl create namespace $$ns --dry-run=client -o yaml | kubectl apply -f -; done
	kubectl -n $(CANVAS_NS) rollout status statefulset/canvas-keycloak --timeout=15m
	kubectl -n $(CANVAS_NS) wait --for=condition=available deploy --all --timeout=15m
	@# The Vault post-install hook can fail once while cert-manager is still issuing canvasvault-tls
	@# (its Job retries and completes); remove the failed attempt so `kubectl get pods` is clean.
	@kubectl -n canvas-vault delete pod --field-selector=status.phase=Failed --ignore-not-found
	$(MAKE) canvas-verify

canvas-verify:
	@kubectl get crd | grep oda.tmforum.org
	@kubectl -n $(CANVAS_NS) get pods

# Observability: Prometheus + Grafana (kube-prometheus-stack, release "monitoring") and Langfuse, all as
# pods in $(OBS_NS), which is where the portal's /observability page looks for them. Runs after the
# Canvas, whose api-operator-istio creates the ServiceMonitors that this Prometheus scrapes.
observability:
	@mkdir -p .rendered
	@test -n "$$GRAFANA_ADMIN_PASSWORD" -a "$$GRAFANA_ADMIN_PASSWORD" != CHANGE_ME || { echo "set GRAFANA_ADMIN_PASSWORD in config.env"; exit 1; }
	@test "$${#LANGFUSE_INIT_USER_PASSWORD}" -ge 8 -a "$$LANGFUSE_INIT_USER_PASSWORD" != CHANGE_ME || { echo "set LANGFUSE_INIT_USER_PASSWORD (>= 8 chars) in config.env"; exit 1; }
	@envsubst < observability-values.yaml.tpl > $(OBS_VALUES)
	@kubectl create namespace $(OBS_NS) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) \
	  -n $(OBS_NS) -f $(OBS_VALUES) --wait --timeout 10m
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

# Local access to the three UIs (Ctrl-C stops all of them).
observability-forward:
	@echo "Grafana    http://localhost:$(GRAFANA_LOCAL_PORT)    (admin / GRAFANA_ADMIN_PASSWORD in config.env)"
	@echo "Prometheus http://localhost:$(PROMETHEUS_LOCAL_PORT)"
	@echo "Langfuse   http://localhost:$(LANGFUSE_LOCAL_PORT)    ($(LANGFUSE_INIT_USER_EMAIL) / LANGFUSE_INIT_USER_PASSWORD in config.env)"
	@trap 'kill 0' INT TERM EXIT; \
	kubectl -n $(OBS_NS) port-forward svc/monitoring-grafana $(GRAFANA_LOCAL_PORT):80 & \
	kubectl -n $(OBS_NS) port-forward svc/monitoring-kube-prometheus-prometheus $(PROMETHEUS_LOCAL_PORT):9090 & \
	kubectl -n $(OBS_NS) port-forward svc/langfuse-web $(LANGFUSE_LOCAL_PORT):3000 & \
	wait

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

# Optional: a reference component, so /components and /exposedapis have data.
component-sample:
	helm repo add oda-components https://tmforum-oda.github.io/reference-example-components/ --force-update
	helm template pc oda-components/productcatalog -n components \
	  | grep -hoE '^\s*-?\s*image:\s*"?[^"[:space:]]+' | sed -E 's/.*image:\s*"?//' | sort -u \
	  | while read i; do docker image inspect $$i >/dev/null 2>&1 || docker pull -q $$i; kind load docker-image $$i --name $(CLUSTER_NAME); done
	helm upgrade --install pc oda-components/productcatalog -n components
	kubectl -n components get components,exposedapis

port-forward:
	kubectl -n $(PORTAL_NS) port-forward svc/$(RELEASE)-frontend $(LOCAL_PORT):80

status:
	@kubectl get pods -A | grep -E "$(PORTAL_NS)|$(CANVAS_NS)|$(COMPONENTS_NS)|$(OBS_NS)|istio|NAMESPACE"

down:
	kind delete cluster --name $(CLUSTER_NAME)
	rm -rf .rendered
