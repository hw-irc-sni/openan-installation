# Connected agent registry: a Registry Center (native API) that runs outside the cluster, represented as an ODA
# Resource Inventory Component so the OpenAN operator's agent sync discovers it like the in-cluster one
# (spec.agentSync.enabled on the OpenAN CR). The Canvas only marks an API ready from Service endpoints, so the
# Component is backed by a small nginx reverse proxy to the external registry, not by an ExternalName Service.
#
# Render with an explicit variable list (nginx's own ${...} variables must stay):
#   export CONNECTED_REGISTRY_NAME=partner-registry CONNECTED_REGISTRY_ORIGIN=https://registry.partner.example \
#          CONNECTED_REGISTRY_HOST=registry.partner.example \
#          OPENAN_AGENT_PROXY_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine
#   envsubst '$${CONNECTED_REGISTRY_NAME} $${CONNECTED_REGISTRY_ORIGIN} $${CONNECTED_REGISTRY_HOST} $${OPENAN_AGENT_PROXY_IMAGE}' \
#     < connected-registry.yaml.tpl | kubectl apply -f -
#
# Credentials (Bearer tokens issued by that registry): `token` is used for writes (deleting cards when a mirrored
# Component is deleted, publishing when openan.dev/agent-publish is "true"), `readToken` (nms_oss: change feed and
# full list) for reads; with only `token` it is used for both:
#   kubectl -n components create secret generic ${CONNECTED_REGISTRY_NAME}-credentials \
#     --from-literal=token=... --from-literal=readToken=...
#   kubectl -n components label secret ${CONNECTED_REGISTRY_NAME}-credentials openan.dev/registry-credentials=true
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${CONNECTED_REGISTRY_NAME}
  namespace: components
  labels: {oda.tmforum.org/componentName: ${CONNECTED_REGISTRY_NAME}}
data:
  default.conf.template: |
    server {
      listen 8080;
      location = /healthz { access_log off; default_type text/plain; return 200 "ok"; }
      location / {
        resolver ${NGINX_LOCAL_RESOLVERS} valid=30s;
        set $registry_upstream ${CONNECTED_REGISTRY_ORIGIN};
        # Canvas gateway traffic arrives as /<component>/...; in-cluster callers (the operator) use the plain path.
        rewrite ^/${CONNECTED_REGISTRY_NAME}(/.*)$ $1 break;
        proxy_pass $registry_upstream;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host ${CONNECTED_REGISTRY_HOST};
        proxy_ssl_server_name on;
        proxy_ssl_name ${CONNECTED_REGISTRY_HOST};
        proxy_buffering off;
        proxy_read_timeout 300s;
      }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${CONNECTED_REGISTRY_NAME}
  namespace: components
  labels: {oda.tmforum.org/componentName: ${CONNECTED_REGISTRY_NAME}}
spec:
  replicas: 1
  selector: {matchLabels: {app: ${CONNECTED_REGISTRY_NAME}}}
  template:
    metadata:
      labels: {app: ${CONNECTED_REGISTRY_NAME}, oda.tmforum.org/componentName: ${CONNECTED_REGISTRY_NAME}}
    spec:
      automountServiceAccountToken: false
      containers:
      - name: proxy
        image: ${OPENAN_AGENT_PROXY_IMAGE}
        imagePullPolicy: IfNotPresent
        env: [{name: NGINX_ENTRYPOINT_LOCAL_RESOLVERS, value: "1"}]
        ports: [{containerPort: 8080}]
        readinessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 10}
        volumeMounts: [{name: conf, mountPath: /etc/nginx/templates}]
        securityContext: {runAsNonRoot: true, allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
      volumes: [{name: conf, configMap: {name: ${CONNECTED_REGISTRY_NAME}}}]
---
apiVersion: v1
kind: Service
metadata:
  name: ${CONNECTED_REGISTRY_NAME}
  namespace: components
  labels: {oda.tmforum.org/componentName: ${CONNECTED_REGISTRY_NAME}}
spec:
  selector: {app: ${CONNECTED_REGISTRY_NAME}}
  ports: [{name: http, port: 8080, targetPort: 8080}]
---
apiVersion: oda.tmforum.org/v1
kind: Component
metadata:
  name: ${CONNECTED_REGISTRY_NAME}
  namespace: components
  labels: {oda.tmforum.org/componentName: ${CONNECTED_REGISTRY_NAME}}
  annotations:
    openan.dev/credentials-secret: ${CONNECTED_REGISTRY_NAME}-credentials
    openan.dev/agent-publish: "false"   # "true": also publish cards of agents made in this cluster
    openan.dev/agent-deploy: "false"    # "true": its cards' deployment extension may run (allow-listed) images here
spec:
  componentMetadata:
    id: OPENAN-RC
    name: ${CONNECTED_REGISTRY_NAME}
    functionalBlock: AIAgentRegistry
    publicationDate: "2026-11-01T00:00:00.000Z"
    status: specified
    version: 1.0.0
    description: "Connected agent registry ${CONNECTED_REGISTRY_ORIGIN} (proxied)"
    maintainers: [{name: OpenAN, email: openan@example.invalid}]
    owners: [{name: OpenAN, email: openan@example.invalid}]
  coreFunction:
    exposedAPIs:
    - name: agentregistry
      apiType: openapi
      specification:
      - url: ${CONNECTED_REGISTRY_ORIGIN}/registry-center.yaml
      implementation: ${CONNECTED_REGISTRY_NAME}
      path: /${CONNECTED_REGISTRY_NAME}/rest/v1/registry-center
      port: 8080
    dependentAPIs: []
  eventNotification: {publishedEvents: [], subscribedEvents: []}
  managementFunction: {exposedAPIs: []}
  securityFunction: {canvasSystemRole: canvasRole, exposedAPIs: []}
