# Login account of the Kubernetes Dashboard (`make k8s-dashboard`), bound to the ClusterRole K8S_DASHBOARD_ROLE (config.mk):
# `view` (read-only, the default) or `cluster-admin` (full control; this local kind demo only).
# No long-lived token Secret: `make k8s-dashboard-token` issues a short-lived one (K8S_DASHBOARD_TOKEN_DURATION).
apiVersion: v1
kind: ServiceAccount
metadata:
  name: dashboard-user
  namespace: kubernetes-dashboard
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: dashboard-user
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${K8S_DASHBOARD_ROLE}
subjects:
  - kind: ServiceAccount
    name: dashboard-user
    namespace: kubernetes-dashboard
