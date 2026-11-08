# Rendered with envsubst into .rendered/portal-values.yaml. AGENTGATEWAY_API_KEY is read from the agentgateway-portal-key
# Secret by `make portal`, and is empty until `make agentgateway` has run (the AI Gateways page then shows it unauthorized).
rbac:
  # Lets the backend add, edit and remove models and MCP servers in agentgateway's ConfigMap and key Secret.
  agentgatewayConfigAccess: {enabled: true, nudgePods: false}
  canvasInstallAccess:
    targetNamespaces: [components, canvas]
  modelGatewayAccess:
    targetNamespaces: [components]
  canvasConformanceAccess:
    targetNamespaces: [components]
frontend:
  image: {repository: ${PORTAL_IMAGE_REPO}, tag: ${PORTAL_FRONTEND_TAG}, pullPolicy: IfNotPresent}
  service: {type: ClusterIP}
  runtime: {keycloakRealm: ${KC_REALM}}
  nginx: {keycloakUpstream: "http://canvas-keycloak.${CANVAS_NS}.svc.cluster.local:${KEYCLOAK_PORT}/auth"}
keycloak:
  init:
    adminUser: ${KC_ADMIN_USER}
    adminPassword: ${KC_ADMIN_PASSWORD}
    realm: ${KC_REALM}
backend:
  image: {repository: ${PORTAL_IMAGE_REPO}, tag: ${PORTAL_BACKEND_TAG}, pullPolicy: IfNotPresent}
  service: {type: ClusterIP}
  env:
    KEYCLOAK_URL: "http://canvas-keycloak.${CANVAS_NS}.svc.cluster.local:${KEYCLOAK_PORT}/auth"
    KEYCLOAK_REALM: ${KC_REALM}
    CORS_ORIGINS: ["http://localhost:${LOCAL_PORT}"]
  # AI Gateways page: agentgateway (`make agentgateway`) in AGENTGATEWAY_NS. The other names keep the chart defaults,
  # which match charts/agentgateway.
  aiGateways:
    configUrl: "http://agentgateway.${AGENTGATEWAY_NS}.svc.cluster.local:8081/config_dump"
    mcpUrl: "http://agentgateway.${AGENTGATEWAY_NS}.svc.cluster.local/mcp"
    llmUrl: "http://agentgateway.${AGENTGATEWAY_NS}.svc.cluster.local/v1/chat/completions"
    configNamespace: ${AGENTGATEWAY_NS}
  # AI Registries > Agents: the OpenAN Registry Center (`make bootstrap-openan`; the OpenAN CR "openan" in OPENAN_NS names
  # its Service). Unreachable until then. Registry credentials added in the portal live in a Secret the chart does not render.
  registryCenter:
    url: "http://openan-registry-center-registry-center.${OPENAN_NS}.svc.cluster.local:5000"
  secretEnv:
    KEYCLOAK_ADMIN_CLIENT_SECRET: ${PORTAL_ADMIN_CLIENT_SECRET}
    AGENTGATEWAY_API_KEY: "${AGENTGATEWAY_API_KEY}"
  conformance:
    canvasBDD:
      env: {KEYCLOAK_USER: ${KC_ADMIN_USER}, KEYCLOAK_PASSWORD: ${KC_ADMIN_PASSWORD}}
