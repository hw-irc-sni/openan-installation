#!/usr/bin/env bash
# Render the Istio and Canvas charts, collect every image and `kind load` it (kind nodes cannot
# pull; the Docker daemon does, so it honours its own proxy settings).
set -euo pipefail
: "${CLUSTER_NAME:?}" "${CANVAS_SRC:?}" "${ISTIO_VERSION:?}" "${CANVAS_NS:?}" "${KPS_VERSION:?}" "${OBS_NS:?}"
out=$(mktemp -d); trap 'rm -rf "$out"' EXIT
helm template istio-base istio/base --version "$ISTIO_VERSION" -n istio-system >"$out/a.yaml"
helm template istiod istio/istiod --version "$ISTIO_VERSION" -n istio-system >>"$out/a.yaml"
helm template istio-ingress istio/gateway --version "$ISTIO_VERSION" -n istio-ingress --skip-schema-validation >>"$out/a.yaml"
helm template "$CANVAS_RELEASE" "$CANVAS_SRC/charts/canvas-oda" -n "$CANVAS_NS" -f .rendered/canvas-values.yaml >>"$out/a.yaml"
# Observability: kube-prometheus-stack (Prometheus, Grafana, operator, kube-state-metrics) ...
helm template monitoring prometheus-community/kube-prometheus-stack --version "$KPS_VERSION" -n "$OBS_NS" \
  -f .rendered/observability-values.yaml >"$out/o.yaml"
# ... the config-reloader is only passed to the operator as an argument, and the bats image is for `helm test`.
obs_extra=$(grep -hoE -- '--prometheus-config-reloader=[^ "]+' "$out/o.yaml" | cut -d= -f2)
sed -i '/bats\/bats/d' "$out/o.yaml"
cat "$out/o.yaml" >>"$out/a.yaml"
images=$( { grep -hoE '^\s*-?\s*image:\s*"?[^"[:space:]]+' "$out/a.yaml" | sed -E 's/.*image:\s*"?//'
            echo "busybox:1.35"; echo "python:3.12-alpine"; echo "docker.io/istio/proxyv2:$ISTIO_VERSION"
            echo "$obs_extra"
            # Langfuse (manifests/langfuse.yaml): web, worker and its Postgres/ClickHouse/Redis/MinIO
            echo "langfuse/langfuse:$LANGFUSE_IMAGE_TAG"; echo "langfuse/langfuse-worker:$LANGFUSE_IMAGE_TAG"
            echo "$LANGFUSE_POSTGRES_IMAGE"; echo "$LANGFUSE_CLICKHOUSE_IMAGE"
            echo "$LANGFUSE_REDIS_IMAGE"; echo "$LANGFUSE_MINIO_IMAGE"; } | sort -u | grep -vxE '|auto|\{\{')
for i in $images; do
  docker image inspect "$i" >/dev/null 2>&1 || docker pull -q "$i" || echo "WARN: could not pull $i"
done
# kind load (docker save + import) is disk bound; 4 in parallel is ~1.7x faster than serial.
for i in $images; do docker image inspect "$i" >/dev/null 2>&1 && echo "$i"; done \
  | xargs -P4 -I{} kind load docker-image {} --name "$CLUSTER_NAME"
