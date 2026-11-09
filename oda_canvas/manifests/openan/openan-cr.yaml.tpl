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
      registry: {publicBaseUrl: "${OPENAN_RC_PUBLIC_BASE_URL}"}
      tmf639: {enabled: ${OPENAN_TMF639_ENABLED}, specificationUrl: "${OPENAN_TMF639_SPEC_URL}"}
      broadcast: {enabled: ${OPENAN_BROADCAST_ENABLED}, callbackAllowlist: "${OPENAN_BROADCAST_CALLBACK_HOSTS}",
                  callbackCaConfigMap: "${OPENAN_BROADCAST_CALLBACK_CA_CONFIGMAP}", callbackCaKey: "${OPENAN_BROADCAST_CALLBACK_CA_KEY}"}
  orchestrationCenter:
    values:
      image: {repository: ghcr.io/project-openan/orchestration-center, tag: "${OPENAN_OC_TAG}"}
      orchestration:
        modelsYaml: |
          models:
            chat:
              provider: openai_compatible
              model: ${OPENAN_LLM_MODEL}
              url: ${OPENAN_LLM_URL}
              timeout: 300
              verify_ssl: true
  frontend:
    values:
      image: {repository: ghcr.io/project-openan/openan-portal, tag: "${OPENAN_FE_TAG}"}
