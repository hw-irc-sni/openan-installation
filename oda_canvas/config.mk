# Deployment parameters. Override on the command line: make up PORTAL_SRC=/path ...
CLUSTER_NAME=oda-canvas
PORTAL_NS=canvas-portal
CANVAS_NS=canvas
COMPONENTS_NS=components
RELEASE=canvas-portal

# canvas-portal-v2 chart source (tmforum-oda/oda-canvas checkout).
PORTAL_SRC=../../../tmforum-oda/oda-canvas/canvas-portal-v2

# Images. The tmforumodacanvas/canvas-portal tags are not published, so by default build
# from PORTAL_SRC and `kind load` them. Set PORTAL_BUILD=false to pull instead.
PORTAL_IMAGE_REPO=tmforumodacanvas/canvas-portal
PORTAL_FRONTEND_TAG=frontend-0.1.0
PORTAL_BACKEND_TAG=backend-0.1.0
PORTAL_BUILD=true
INIT_JOB_IMAGE=python:3.12-alpine   # hardcoded in the chart keycloak-init hook

# ODA Canvas (charts/canvas-oda) source and the Istio it requires. The Canvas must live in the
# namespace literally named 'canvas' (canvas-portal-v2 detects the gateway type there).
CANVAS_SRC=../../../tmforum-oda/oda-canvas
CANVAS_RELEASE=canvas
ISTIO_VERSION=1.26.3
# Canvas supports Kubernetes <= 1.30; rendered into the kind config.
KIND_NODE_IMAGE=kindest/node:v1.30.0
# Minimum Helm: the Makefile uses `--skip-schema-validation` (3.16). Helm 4 is accepted with a warning.
HELM_MIN=3.16
# Optional: pin the Canvas checkout (git revision prefix); `make environment` fails if CANVAS_SRC is elsewhere.
CANVAS_REF=
# Hook Jobs of the Canvas chart default to bitnamilegacy/kubectl:latest (imagePullPolicy Always, so kind load
# is not enough). Pin a tag matching the cluster's Kubernetes version.
HOOK_KUBECTL_TAG=1.30.2
# Address given to istio-ingress (kind has no LoadBalancer) and how `make ingress-forward` exposes it.
# Conformance tests (CTK containers) need an address the containers reach: INGRESS_ADDRESS=172.17.0.1
# INGRESS_FORWARD_ADDRESS=0.0.0.0 on Linux, host.docker.internal on Docker Desktop.
INGRESS_ADDRESS=127.0.0.1
INGRESS_FORWARD_ADDRESS=127.0.0.1
INGRESS_HTTP_PORT=80
INGRESS_HTTPS_PORT=443
# canvas-portal-v2 lists Gateway API HTTPRoutes even on Istio and 404s if the CRD is missing.
GATEWAY_API_CRDS=https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml

# Keycloak bundled with the Canvas chart (realm odari). Passwords/secrets live in config.env (untracked).
KEYCLOAK_PORT=8083
KC_ADMIN_USER=admin
KC_REALM=odari
PORTAL_USER=portaladmin

# Component namespaces created (if missing) and labelled istio-injection=enabled after the Canvas install.
# The Canvas operators only reconcile 'components' and 'odacompns-*', so keep this list inside that scope.
COMPONENT_NAMESPACES=components
LOCAL_PORT=8080

# Observability (`make observability`): the Canvas charts/observability-stack (release "monitoring":
# Prometheus + Grafana + OpenTelemetry collector + Jaeger) and Langfuse. The namespace must be one the portal scans (monitoring), see /observability.
OBS_NS=monitoring
OBS_RELEASE=monitoring
# Jaeger all-in-one tag (the chart default is 'latest').
JAEGER_IMAGE_TAG=1.62.0
LANGFUSE_IMAGE_TAG=3
LANGFUSE_POSTGRES_IMAGE=postgres:17-alpine
LANGFUSE_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:24.12
LANGFUSE_REDIS_IMAGE=redis:7-alpine
LANGFUSE_MINIO_IMAGE=bitnamilegacy/minio:2025.7.23-debian-12-r3   # minio/minio is no longer published
LANGFUSE_INIT_ORG=openan
LANGFUSE_INIT_PROJECT=openan
LANGFUSE_INIT_USER_EMAIL=admin@example.com
# Host ports of `make observability-forward` (8080/8081 may be taken; Grafana and Langfuse both listen on 3000).
JAEGER_LOCAL_PORT=16686
GRAFANA_LOCAL_PORT=3001
PROMETHEUS_LOCAL_PORT=9090
LANGFUSE_LOCAL_PORT=3002

# OpenAN operator (charts/openan-operator, operator/openan-operator) and the stack it manages.
OPENAN_NS=components
OPENAN_OPERATOR_NS=canvas
OPENAN_OPERATOR_IMAGE=openan/openan-operator
OPENAN_OPERATOR_TAG=0.1.0
OPENAN_RC_TAG=v1.1.2
# No published Orchestration Center image matches the current API: it is built from OPENAN_OC_SRC.
OPENAN_OC_TAG=src
OPENAN_OC_SRC=../../orchestration-center
# The portal is built from its release tarball (containerized/build/portal).
OPENAN_FE_TAG=v1.0.0
