# kube-prometheus-stack (Prometheus + Grafana + operator), release "monitoring" in namespace
# "monitoring": the Services are then named monitoring-kube-prometheus-prometheus and
# monitoring-grafana, which canvas-portal-v2 discovers (its /observability page).
# Rendered with envsubst into .rendered/observability-values.yaml.
alertmanager: {enabled: false}
nodeExporter: {enabled: false}
# kind control-plane components are not scrapeable; leaving them on only shows red targets.
kubeEtcd: {enabled: false}
kubeScheduler: {enabled: false}
kubeControllerManager: {enabled: false}
kubeProxy: {enabled: false}
prometheusOperator:
  admissionWebhooks: {enabled: false}   # no certgen hook job / image
  tls: {enabled: false}                 # its TLS secret would come from that job
prometheus:
  prometheusSpec:
    retention: 7d
    # Scrape ServiceMonitors/PodMonitors of every namespace (the Canvas creates them per component).
    serviceMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelector: {}
    serviceMonitorNamespaceSelector: {}
    podMonitorSelectorNilUsesHelmValues: false
    podMonitorSelector: {}
    podMonitorNamespaceSelector: {}
    ruleSelectorNilUsesHelmValues: false
kubelet:
  serviceMonitor:
    # containerd (kind) cAdvisor metrics lack the image/container labels the stack's rules filter on
    # (same workaround as charts/observability-stack in oda-canvas).
    cAdvisorMetricRelabelings:
      - {sourceLabels: [pod, image], regex: "(.+);", action: replace, replacement: "containerd://$1", targetLabel: image}
      - {sourceLabels: [pod, container], regex: "(.+);", action: replace, replacement: "POD", targetLabel: container}
grafana:
  enabled: true
  adminUser: admin
  adminPassword: ${GRAFANA_ADMIN_PASSWORD}
  defaultDashboardsTimezone: browser
