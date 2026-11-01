#!/usr/bin/env bash
# Render the Istio and Canvas charts, collect every image and `kind load` it (kind nodes cannot
# pull; the Docker daemon does, so it honours its own proxy settings).
set -euo pipefail
: "${CLUSTER_NAME:?}" "${CANVAS_SRC:?}" "${ISTIO_VERSION:?}" "${CANVAS_NS:?}"
out=$(mktemp -d); trap 'rm -rf "$out"' EXIT
helm template istio-base istio/base --version "$ISTIO_VERSION" -n istio-system >"$out/a.yaml"
helm template istiod istio/istiod --version "$ISTIO_VERSION" -n istio-system >>"$out/a.yaml"
helm template istio-ingress istio/gateway --version "$ISTIO_VERSION" -n istio-ingress --skip-schema-validation >>"$out/a.yaml"
helm template "$CANVAS_RELEASE" "$CANVAS_SRC/charts/canvas-oda" -n "$CANVAS_NS" -f .rendered/canvas-values.yaml >>"$out/a.yaml"
images=$( { grep -hoE '^\s*-?\s*image:\s*"?[^"[:space:]]+' "$out/a.yaml" | sed -E 's/.*image:\s*"?//'
            echo "busybox:1.35"; echo "python:3.12-alpine"; echo "docker.io/istio/proxyv2:$ISTIO_VERSION"; } | sort -u | grep -vxE '|auto')
for i in $images; do
  docker image inspect "$i" >/dev/null 2>&1 || docker pull -q "$i" || echo "WARN: could not pull $i"
done
# kind load (docker save + import) is disk bound; 4 in parallel is ~1.7x faster than serial.
for i in $images; do docker image inspect "$i" >/dev/null 2>&1 && echo "$i"; done \
  | xargs -P4 -I{} kind load docker-image {} --name "$CLUSTER_NAME"
