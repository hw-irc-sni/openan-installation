# kind cluster for ODA Canvas development and quick demos. Rendered with envsubst into
# .rendered/kind-config.yaml (CLUSTER_NAME and KIND_NODE_IMAGE come from config.mk).
# Ports are not mapped to the host: the portal is reached with `make port-forward`
# because canvas-portal-v2's Services have no configurable nodePort.
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: ${CLUSTER_NAME}
nodes:
  - role: control-plane
    image: ${KIND_NODE_IMAGE}
    labels:
      ingress-ready: "true"
