# Overrides for the Canvas chart charts/observability-stack (kube-prometheus-stack under the alias
# "prometheus", + OpenTelemetry collector + Jaeger), installed as release "monitoring" in namespace
# "monitoring": the Services are then named monitoring-kube-prometheus-prometheus and monitoring-grafana,
# which canvas-portal-v2 discovers (its /observability page).
# Rendered with envsubst into .rendered/observability-values.yaml.
#
# The chart's own defaults already provide: ServiceMonitor discovery in every namespace, the containerd
# cAdvisor relabelings (kind/Docker Desktop), node-exporter off, Grafana on. Only the demo tweaks are here.
prometheus:
  # The alias would name the Services monitoring-prometheus-*; the portal looks for monitoring-kube-prometheus-*.
  fullnameOverride: monitoring-kube-prometheus
  alertmanager: {enabled: false}
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
      # The chart already selects every ServiceMonitor/PodMonitor; also take every PrometheusRule.
      podMonitorSelectorNilUsesHelmValues: false
      podMonitorSelector: {}
      podMonitorNamespaceSelector: {}
      ruleSelectorNilUsesHelmValues: false
  grafana:
    enabled: true
    # SQLite WAL: the first-start migration otherwise races the sidecars ("database is locked", one container restart).
    grafana.ini:
      database: {wal: true}
    adminUser: admin
    adminPassword: ${GRAFANA_ADMIN_PASSWORD}
    defaultDashboardsTimezone: browser
jaeger:
  allInOne:
    tag: "${JAEGER_IMAGE_TAG}"   # the chart default is "latest" (not reproducible)
    # The chart sets SPAN_STORAGE_TYPE=memory both here and in its template; the duplicate env entry is
    # rejected by Helm 4 server-side apply. The template already provides it.
    extraEnv: []
opentelemetry-collector:
  # The collector exports to Jaeger from its first second: start it once Jaeger accepts connections.
  initContainers:
    - name: wait-for-jaeger
      image: busybox:1.35
      command: ["sh", "-c", "until nc -z -w2 ${OBS_RELEASE}-jaeger-collector 4317; do echo waiting for jaeger; sleep 2; done"]
  config:
    # Bind to the pod IP (as the chart does for its other receivers) instead of 0.0.0.0: reachable from other pods,
    # without the collector's "exposes this server to every network interface" warning.
    extensions:
      health_check:
        endpoint: "${env:MY_POD_IP}:13133"
    receivers:
      otlp:
        protocols:
          http:
            endpoint: "${env:MY_POD_IP}:4318"
    exporters:
      # The chart hard-codes the Jaeger collector Service of a release named "observability".
      otlp/jaeger:
        endpoint: ${OBS_RELEASE}-jaeger-collector.${OBS_NS}.svc.cluster.local.:4317
