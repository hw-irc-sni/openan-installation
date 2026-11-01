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
KIND_NODE_IMAGE=kindest/node:v1.30.0   # Canvas supports Kubernetes <= 1.30
# canvas-portal-v2 lists Gateway API HTTPRoutes even on Istio and 404s if the CRD is missing.
GATEWAY_API_CRDS=https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml

# Keycloak bundled with the Canvas chart (realm odari). Passwords/secrets live in config.env (untracked).
KEYCLOAK_PORT=8083
KC_ADMIN_USER=admin
KC_REALM=odari
PORTAL_USER=portaladmin

# Namespaces the portal may install Helm releases into / run conformance in.
CANVAS_NAMESPACES=components,canvas,temp-namespace
LOCAL_PORT=8080
