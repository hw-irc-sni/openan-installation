#!/usr/bin/env bash
# Collect the images of one deployment stage and `kind load` them (kind nodes cannot pull; the Docker
# daemon does, so it honours its own proxy settings).
#   preload-images.sh environment     Istio (base, istiod, gateway, proxyv2)
#   preload-images.sh canvas          charts/canvas-oda with .rendered/canvas-values.yaml (incl. Vault, hooks)
#   preload-images.sh observability   charts/observability-stack + Langfuse
set -euo pipefail
stage=${1:?usage: preload-images.sh environment|canvas|observability}
: "${CLUSTER_NAME:?}"
out=$(mktemp -d); trap 'rm -rf "$out"' EXIT

# image: references of a rendered manifest, one per line
extract() { grep -hoE '^\s*-?\s*image:\s*"?[^"[:space:]]+' "$1" | sed -E 's/.*image:\s*"?//'; }

case "$stage" in
environment)
  : "${ISTIO_VERSION:?}"
  helm template istio-base istio/base --version "$ISTIO_VERSION" -n istio-system >"$out/m.yaml"
  helm template istiod istio/istiod --version "$ISTIO_VERSION" -n istio-system >>"$out/m.yaml"
  helm template istio-ingress istio/gateway --version "$ISTIO_VERSION" -n istio-ingress --skip-schema-validation >>"$out/m.yaml"
  extra=$(echo "docker.io/istio/proxyv2:$ISTIO_VERSION") ;;
canvas)
  : "${CANVAS_SRC:?}" "${CANVAS_NS:?}" "${CANVAS_RELEASE:?}"
  helm template "$CANVAS_RELEASE" "$CANVAS_SRC/charts/canvas-oda" -n "$CANVAS_NS" -f .rendered/canvas-values.yaml >"$out/m.yaml"
  extra=$(echo "busybox:1.35"; echo "python:3.12-alpine") ;;
observability)
  : "${OBS_CHART:?}" "${OBS_NS:?}" "${OBS_RELEASE:?}" "${LANGFUSE_IMAGE_TAG:?}"
  helm template "$OBS_RELEASE" "$OBS_CHART" -n "$OBS_NS" -f .rendered/observability-values.yaml >"$out/m.yaml"
  # the config-reloader is only passed to the operator as an argument, and the bats image is for `helm test`
  reloader=$(grep -hoE -- '--prometheus-config-reloader=[^ "]+' "$out/m.yaml" | cut -d= -f2 || true)
  sed -i '/bats\/bats/d' "$out/m.yaml"
  # Langfuse (manifests/langfuse.yaml): web, worker and its Postgres/ClickHouse/Redis/MinIO
  extra=$(echo "$reloader"; echo "busybox:1.35"
          echo "langfuse/langfuse:$LANGFUSE_IMAGE_TAG"; echo "langfuse/langfuse-worker:$LANGFUSE_IMAGE_TAG"
          echo "$LANGFUSE_POSTGRES_IMAGE"; echo "$LANGFUSE_CLICKHOUSE_IMAGE"
          echo "$LANGFUSE_REDIS_IMAGE"; echo "$LANGFUSE_MINIO_IMAGE") ;;
*) echo "unknown stage: $stage" >&2; exit 2 ;;
esac

images=$( { extract "$out/m.yaml"; echo "$extra"; } | sort -u | grep -vxE '|auto|\{\{')
for i in $images; do
  docker image inspect "$i" >/dev/null 2>&1 || docker pull -q "$i" || echo "WARN: could not pull $i"
done
# kind load (docker save + import) is disk bound; 4 in parallel is ~1.7x faster than serial.
for i in $images; do docker image inspect "$i" >/dev/null 2>&1 && echo "$i"; done \
  | xargs -P4 -I{} kind load docker-image {} --name "$CLUSTER_NAME"
