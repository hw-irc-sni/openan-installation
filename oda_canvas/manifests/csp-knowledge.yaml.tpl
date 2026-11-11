# CSP knowledge sample (`make csp-knowledge-sample`): the c-kg knowledge-graph MCP server (CSP_KNOWLEDGE_SRC, see its
# docs/deployment.md) over Streamable HTTP at http://csp-knowledge.<namespace>.svc.cluster.local:8000/mcp, registered in
# agentgateway as MCP target csp-knowledge. Adapted from c-kg's deploy/k8s/c-kg.yaml: no Namespace (it runs next to agentgateway) and no placeholder
# Secret (the Makefile creates csp-knowledge-api-key once, so `kubectl apply` never resets the key).
# Exactly 1 replica: the graph lives in process memory and is written to the volume only on a graceful shutdown.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: csp-knowledge-data
  namespace: ${CSP_KNOWLEDGE_NS}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: csp-knowledge
  namespace: ${CSP_KNOWLEDGE_NS}
  labels: { app.kubernetes.io/name: csp-knowledge }
spec:
  replicas: 1
  # Recreate: a second pod on the same RWO volume would race on kg_data.ttl.
  strategy:
    type: Recreate
  selector:
    matchLabels: { app.kubernetes.io/name: csp-knowledge }
  template:
    metadata:
      labels: { app.kubernetes.io/name: csp-knowledge }
      annotations:
        # The image is rebuilt under the same tag: its ID rolls the pod when the build changes.
        csp-knowledge/image-id: "${CSP_KNOWLEDGE_IMAGE_ID}"
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: { type: RuntimeDefault }
      # SIGTERM reaches PID 1 (exec-form ENTRYPOINT), which writes kg_data.ttl on exit.
      terminationGracePeriodSeconds: 30
      containers:
        - name: c-kg
          image: ${CSP_KNOWLEDGE_IMAGE}
          imagePullPolicy: IfNotPresent
          ports:
            - { name: http, containerPort: 8000 }
          # Settings inline (not a ConfigMap read at start): a change here rolls the pod.
          env:
            # Host headers are compared literally, port included (421 otherwise), and agentgateway sends them without
            # the port: list both forms. The C_KG_BASE_URL netloc and 127.0.0.1:*/localhost:* (port-forward) are built in.
            - { name: C_KG_BASE_URL, value: "http://csp-knowledge.${CSP_KNOWLEDGE_NS}.svc.cluster.local:8000" }
            - name: C_KG_ALLOWED_HOSTS
              value: "csp-knowledge,csp-knowledge:8000,csp-knowledge.${CSP_KNOWLEDGE_NS},csp-knowledge.${CSP_KNOWLEDGE_NS}:8000,csp-knowledge.${CSP_KNOWLEDGE_NS}.svc,csp-knowledge.${CSP_KNOWLEDGE_NS}.svc:8000,csp-knowledge.${CSP_KNOWLEDGE_NS}.svc.cluster.local"
            # Pinned: the base of every named graph and instance IRI; changing it orphans the data in kg_data.ttl.
            - { name: C_KG_BASE_IRI, value: "${CSP_KNOWLEDGE_BASE_IRI}" }
            - { name: C_KG_TRANSPORT, value: streamable-http }
            - { name: C_KG_HOST, value: 0.0.0.0 }
            - { name: C_KG_PORT, value: "8000" }
            - { name: C_KG_DATA_FILE, value: /app/data/kg_data.ttl }
            - name: C_KG_API_KEY
              valueFrom:
                secretKeyRef: { name: csp-knowledge-api-key, key: api-key }
          # TCP probes: every request without the key gets 401, which an httpGet probe would count as a failure.
          startupProbe:
            tcpSocket: { port: http }
            periodSeconds: 2
            failureThreshold: 30
          readinessProbe:
            tcpSocket: { port: http }
            periodSeconds: 10
          livenessProbe:
            tcpSocket: { port: http }
            periodSeconds: 20
            failureThreshold: 3
          resources:
            requests: { cpu: 100m, memory: 512Mi }
            limits: { memory: 2Gi }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false  # editable install under /app/src
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - { name: data, mountPath: /app/data }
      volumes:
        - name: data
          persistentVolumeClaim: { claimName: csp-knowledge-data }
---
apiVersion: v1
kind: Service
metadata:
  name: csp-knowledge
  namespace: ${CSP_KNOWLEDGE_NS}
  labels: { app.kubernetes.io/name: csp-knowledge }
spec:
  type: ClusterIP
  selector: { app.kubernetes.io/name: csp-knowledge }
  ports:
    - { name: http, port: 8000, targetPort: http }
