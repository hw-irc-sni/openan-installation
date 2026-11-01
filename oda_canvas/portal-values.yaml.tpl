# Rendered with envsubst into .rendered/portal-values.yaml.
rbac:
  canvasInstallAccess:
    targetNamespaces: [components, canvas]
  modelGatewayAccess:
    targetNamespaces: [components]
  canvasConformanceAccess:
    targetNamespaces: [components, temp-namespace]
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
  secretEnv:
    KEYCLOAK_ADMIN_CLIENT_SECRET: ${PORTAL_ADMIN_CLIENT_SECRET}
  conformance:
    canvasBDD:
      env: {KEYCLOAK_USER: ${KC_ADMIN_USER}, KEYCLOAK_PASSWORD: ${KC_ADMIN_PASSWORD}}
