# Sample-A2A-T-Agent: an A2A (a2a-sdk) + A2A-T (a2a-t-sdk, Task-T extension) agent that passes every message it
# receives to the LLM and replies with the answer. Source and image: ../../sample-agent (`make openan-sample-image`).
# Rendered with envsubst (`make openan-sample`). The OpenAN operator discovers the a2a ExposedAPI and publishes the
# Agent Card in the Registry Center.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: sample-agent
  namespace: components
  labels: {oda.tmforum.org/componentName: sample-agent-sampleagent}
spec:
  replicas: 1
  selector: {matchLabels: {app: sample-agent}}
  template:
    metadata:
      labels: {app: sample-agent, oda.tmforum.org/componentName: sample-agent-sampleagent}
    spec:
      containers:
      - name: agent
        image: ${OPENAN_SAMPLE_AGENT_IMAGE}
        imagePullPolicy: IfNotPresent
        env:
        - {name: AGENT_PUBLIC_URL, value: "http://sample-agent.components.svc.cluster.local:8080/a2a"}
        - {name: A2A_RPC_PATHS, value: "/a2a,/sample-agent-sampleagent/a2a"}
        - {name: LLM_BASE_URL, value: "${OPENAN_LLM_BASE_URL}"}
        - {name: LLM_MODEL, value: "${OPENAN_LLM_MODEL}"}
        ports: [{containerPort: 8080}]
        readinessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 5}
        securityContext: {runAsNonRoot: true, allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
---
apiVersion: v1
kind: Service
metadata:
  name: sample-agent
  namespace: components
  labels: {oda.tmforum.org/componentName: sample-agent-sampleagent}
spec:
  selector: {app: sample-agent}
  ports: [{name: http, port: 8080, targetPort: 8080}]
---
apiVersion: oda.tmforum.org/v1
kind: Component
metadata:
  name: sample-agent-sampleagent
  namespace: components
  labels: {oda.tmforum.org/componentName: sample-agent-sampleagent}
spec:
  componentMetadata:
    id: SAMPLE-AGENT
    name: sampleagent
    functionalBlock: AIAgent
    publicationDate: "2026-11-01T00:00:00.000Z"
    status: specified
    version: 1.0.0
    description: "Sample-A2A-T-Agent: A2A + A2A-T (Task-T) agent that answers every message with an LLM."
    maintainers: [{name: OpenAN, email: openan@example.invalid}]
    owners: [{name: OpenAN, email: openan@example.invalid}]
  coreFunction:
    exposedAPIs:
    - name: a2tagent
      apiType: a2a
      specification:
      - url: http://sample-agent.components.svc.cluster.local:8080/.well-known/agent-card.json
      implementation: sample-agent
      path: /sample-agent-sampleagent/a2a
      port: 8080
    dependentAPIs: []
  eventNotification: {publishedEvents: [], subscribedEvents: []}
  managementFunction: {exposedAPIs: []}
  securityFunction: {canvasSystemRole: canvasRole, exposedAPIs: []}
