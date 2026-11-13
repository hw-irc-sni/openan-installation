# Deployment parameters. Override on the command line: make up PORTAL_SRC=/path ...
CLUSTER_NAME=oda-canvas
PORTAL_NS=canvas-portal
CANVAS_NS=canvas
COMPONENTS_NS=components
RELEASE=canvas-portal

# canvas-portal-v2 chart source (tmforum-oda/oda-canvas checkout). merged-demo combines the ai-gateways and ai-registries
# work: the AI Gateways settings in portal-values.yaml.tpl need chart 0.1.3 or later, and older charts ignore them.
PORTAL_SRC=../../../tmforum-oda/oda-canvas-merged-demo/canvas-portal-v2

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
# Address given to istio-ingress (kind has no LoadBalancer); `make oda-canvas-port-forward` exposes it.
# Conformance tests (CTK containers) need an address the containers reach: INGRESS_ADDRESS=172.17.0.1
# (host.docker.internal on Docker Desktop).
INGRESS_ADDRESS=127.0.0.1
# Interface(s) of the host the forwards listen on: 0.0.0.0 = any IP of the host. Comma-separated list allowed.
FORWARD_ADDRESS=0.0.0.0
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
# Host ports of the observability UIs in `make oda-canvas-port-forward` (8080/8081 may be taken; Grafana and Langfuse both listen on 3000).
JAEGER_LOCAL_PORT=16686
GRAFANA_LOCAL_PORT=3001
PROMETHEUS_LOCAL_PORT=9090
LANGFUSE_LOCAL_PORT=3002
OPENAN_PORTAL_LOCAL_PORT=8082
# Kubernetes Dashboard (`make k8s-dashboard`, part of `make bootstrap-openan`), HTTPS with a self-signed certificate.
K8S_DASHBOARD_LOCAL_PORT=8444
# ClusterRole of the dashboard login account: view (read-only) or cluster-admin (full control of the cluster).
K8S_DASHBOARD_ROLE=view
# Validity of the login token from `make k8s-dashboard-token` (keep it short, above all with cluster-admin).
K8S_DASHBOARD_TOKEN_DURATION=1h

# Kubernetes Dashboard chart. The project is archived (kubernetes-retired/dashboard) and its Helm repository is gone:
# 7.14.0 is the last release, installed from the chart tarball of its GitHub release.
K8S_DASHBOARD_CHART_VERSION=7.14.0
K8S_DASHBOARD_CHART_URL=https://github.com/kubernetes-retired/dashboard/releases/download/kubernetes-dashboard-$(K8S_DASHBOARD_CHART_VERSION)/kubernetes-dashboard-$(K8S_DASHBOARD_CHART_VERSION).tgz

# OpenAN operator (charts/openan-operator, operator/openan-operator) and the stack it manages.
OPENAN_NS=components
OPENAN_OPERATOR_NS=canvas
OPENAN_OPERATOR_IMAGE=openan/openan-operator
OPENAN_OPERATOR_TAG=0.1.0
OPENAN_RC_TAG=v1.1.2
# OPENAN_RC_BUILD=true builds ghcr.io/project-openan/registry-center:$(OPENAN_RC_TAG) from OPENAN_RC_SRC instead of pulling it
# (use a non-release tag, e.g. OPENAN_RC_TAG=src). Needed for TMF639 and the gateway path prefix until a release has them.
OPENAN_RC_BUILD=false
OPENAN_RC_SRC=../../registry-center
# Base of resource/event hrefs: the Canvas gateway URL of the Registry Center Component (as in its ExposedAPI status).
OPENAN_RC_PUBLIC_BASE_URL=https://$(INGRESS_ADDRESS)/openan-registry-center-registrycenter
# TMFC012 alignment (oda_canvas/TMF639-FACADE-PLAN.md). OPENAN_TMF639_SPEC_URL is required when enabled: the
# tmf639-a2a-agent-v5.yaml profile at a commit, tag or branch (README.md has the fork recipe). The operator accepts only URLs under
# https://raw.githubusercontent.com/project-openan/registry-center/ unless OPENAN_ALLOWED_SPEC_URL_PREFIXES
# (comma separated) says otherwise, e.g. https://raw.githubusercontent.com/hw-irc-sni/registry-center/ for a fork.
OPENAN_TMF639_ENABLED=false
OPENAN_TMF639_SPEC_URL=
OPENAN_ALLOWED_SPEC_URL_PREFIXES=
# Change broadcast: native webhook subscriptions and, with OPENAN_TMF639_ENABLED, the TMF639 /hub and its published
# events. OPENAN_BROADCAST_CALLBACK_HOSTS (comma-separated host names) is required when enabled; it becomes both
# broadcast.callbackAllowlist in the CR and the operator's ALLOWED_CALLBACK_HOSTS. Callbacks must be https: a CR
# cannot enable plain-HTTP callbacks.
OPENAN_BROADCAST_ENABLED=false
OPENAN_BROADCAST_CALLBACK_HOSTS=
# ConfigMap in $(OPENAN_NS) with a PEM bundle of extra CAs for callback TLS (key OPENAN_BROADCAST_CALLBACK_CA_KEY), for
# listeners with private certificates. It becomes broadcast.callbackCaConfigMap in the CR and the operator's
# ALLOWED_CALLBACK_CA_CONFIGMAPS. The demo does not create it; the Registry Center pod does not start until it exists.
OPENAN_BROADCAST_CALLBACK_CA_CONFIGMAP=
OPENAN_BROADCAST_CALLBACK_CA_KEY=ca.crt
# No published Orchestration Center image matches the current API: it is built from OPENAN_OC_SRC.
OPENAN_OC_TAG=src
OPENAN_OC_SRC=../../orchestration-center
# The portal is built from its release tarball (containerized/build/portal).
OPENAN_FE_TAG=v1.0.0
# OpenAN Orchestration Center chat model (models.yaml): an OpenAI-compatible endpoint reachable from the kind pods
# (use the host's LAN address, not localhost). No API key is configured.
OPENAN_LLM_BASE_URL=http://10.220.239.55:50343/v1
OPENAN_LLM_URL=$(OPENAN_LLM_BASE_URL)/chat/completions
OPENAN_LLM_MODEL=Qwen/Qwen3.8-27B-FP8
# Sample-A2A-T-Agent image (built from ./sample-agent and loaded into kind)
OPENAN_SAMPLE_AGENT_IMAGE=openan-sample-a2a-t-agent:0.1.0

# AI gateway (`make agentgateway`, part of `make bootstrap-openan`): charts/agentgateway and the test MCP server it routes to.
# Not every Canvas branch has the chart, so this is a separate checkout: merged-demo combines ai-gateways and ai-registries.
AGENTGATEWAY_SRC=../../../tmforum-oda/oda-canvas-merged-demo
# The portal's AI Gateways settings (aiGateways.configNamespace, the gateway URLs) assume the gateway runs in the Canvas namespace.
AGENTGATEWAY_NS=$(CANVAS_NS)
# The OpenAN operator keeps an A2A route per a2a ExposedAPI of OPENAN_NS in this gateway (`make openan-operator`):
# /agents/<namespace>/<ExposedAPI> on the gateway. false: no routes and no RBAC in AGENTGATEWAY_NS.
OPENAN_AGENTGATEWAY_ROUTES=true
# Empty (default): agent cards in the registries keep the agent's own URL. Set, e.g. to
# http://agentgateway.$(AGENTGATEWAY_NS).svc.cluster.local: they advertise the gateway route instead.
OPENAN_AGENTGATEWAY_PUBLISH_URL=
# Must match charts/agentgateway/values.yaml (agentgateway-standalone.image and testMcpServer.image).
AGENTGATEWAY_IMAGE=ghcr.io/agentgateway/agentgateway:v1.7.0
TEST_MCP_SERVER_IMAGE=tmforumodacanvas/test-mcp-server:0.1.1

# CSP knowledge sample (`make csp-knowledge-sample`, not part of the bootstraps): the c-kg knowledge-graph MCP server, built
# from CSP_KNOWLEDGE_SRC and loaded into kind, deployed next to agentgateway and registered there as MCP target csp-knowledge.
CSP_KNOWLEDGE_SRC=../../../knowledge/c-kg
CSP_KNOWLEDGE_IMAGE=c-kg:sample
CSP_KNOWLEDGE_NS=$(AGENTGATEWAY_NS)
# c-kg tools reachable through agentgateway (comma separated; empty: all 29). The gateway's MCP port has no client
# authentication, so by default only the read-only ones: no add/update/delete, load/save (file paths), ontology,
# agent, BPMN or reasoning changes.
CSP_KNOWLEDGE_ALLOWED_TOOLS=sparql_query,get_entity,list_classes,list_instances,get_class_schema,list_ontologies,graph_stats,validate,get_subgraph,get_path,find_workflows,get_workflow_steps,list_agents,get_workflow_executions
# RDF base of the graph's named graphs and instance IRIs (C_KG_BASE_IRI): keep it fixed once data exists.
CSP_KNOWLEDGE_BASE_IRI=http://c-kg.net
