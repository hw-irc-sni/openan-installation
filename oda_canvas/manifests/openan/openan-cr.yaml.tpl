apiVersion: openan.dev/v1alpha1
kind: OpenAN
metadata:
  name: openan
  namespace: ${OPENAN_NS}
spec:
  registry:
    organization: openan-demo
  cardSync:
    enabled: true
    # Cards are ES256-signed; the Registry verifies them with keys from its local key store (a ConfigMap the
    # operator maintains), so no HTTPS jku endpoint is needed.
    signing:
      enabled: true
  registryCenter:
    values:
      image: {repository: ghcr.io/project-openan/registry-center, tag: "${OPENAN_RC_TAG}"}
  orchestrationCenter:
    values:
      image: {repository: ghcr.io/project-openan/orchestration-center, tag: "${OPENAN_OC_TAG}"}
  frontend:
    values:
      image: {repository: ghcr.io/project-openan/openan-portal, tag: "${OPENAN_FE_TAG}"}
