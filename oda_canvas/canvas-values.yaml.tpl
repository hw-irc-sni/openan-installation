# Rendered with envsubst into .rendered/canvas-values.yaml. Demo profile: Istio gateway only,
# optional services off to keep the image set and memory footprint small.
global:
  hookImages:
    kubectl: bitnamilegacy/kubectl:${HOOK_KUBECTL_TAG}   # chart default is :latest = imagePullPolicy Always
preqrequisitechecks:
  istio: false          # kind has no LoadBalancer; the ingress address is patched by `make ingress-address`
api-operator-istio:
  enabled: true
  deployment:
    openMetricsImplementation: ServiceMonitor   # component metrics are scraped by the Prometheus of `make observability`
kong-gateway-install:
  enabled: false
apisix-gateway-install:
  enabled: false
canvas-info-service:
  enabled: false
resource-inventory:
  enabled: false
pdb-management-operator:
  enabled: false
component-registry:
  enabled: false
dependentapi-simple-operator:   # schema requires this or dependency-management-keycloak-authz
  enabled: true
  componentRegistryUrl: ""      # component-registry is off: do not point the operator at a Service that does not exist
