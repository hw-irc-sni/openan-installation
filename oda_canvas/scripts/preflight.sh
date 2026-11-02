#!/usr/bin/env bash
# Pre-flight checks for the ODA Canvas demo deployment (called by the Makefile).
#   preflight.sh tools                  host tools, versions, Docker daemon, Canvas checkout
#   preflight.sh secrets VAR [VAR...]   config.env values must be set (not empty / CHANGE_ME)
# Nothing is installed or downloaded: missing tools are reported with an install hint.
set -uo pipefail

fail=0
err()  { echo "ERROR: $*" >&2; fail=1; }
warn() { echo "WARNING: $*" >&2; }

# version_ge A B: A >= B for dotted versions
version_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]; }

hint() {
  case "$1" in
    docker)   echo "https://docs.docker.com/engine/install/" ;;
    kind)     echo "https://kind.sigs.k8s.io/docs/user/quick-start/#installation  (e.g. go install sigs.k8s.io/kind@latest)" ;;
    kubectl)  echo "https://kubernetes.io/docs/tasks/tools/" ;;
    helm)     echo "https://helm.sh/docs/intro/install/" ;;
    envsubst) echo "package 'gettext' / 'gettext-base' (apt install gettext-base, brew install gettext)" ;;
    python3)  echo "package 'python3'" ;;
    curl)     echo "package 'curl'" ;;
    git)      echo "package 'git'" ;;
    openssl)  echo "package 'openssl'" ;;
  esac
}

check_tools() {
  for t in docker kind kubectl helm envsubst python3 curl git openssl; do
    command -v "$t" >/dev/null || err "missing '$t': $(hint "$t")"
  done
  [ "$fail" = 0 ] || return

  docker info >/dev/null 2>&1 || err "docker daemon not reachable (is it running, and may this user use it?)"
  if [ -n "${https_proxy:-}${HTTPS_PROXY:-}" ] && ! docker info 2>/dev/null | grep -q "HTTPS Proxy"; then
    warn "your shell uses a proxy but the Docker daemon has none; image pulls/builds may fail with"
    warn "'no route to host'. See README 'Network / proxy troubleshooting'."
  fi

  helm_v=$(helm version --template '{{.Version}}' 2>/dev/null | sed 's/^v//;s/[-+].*//')
  if [ -z "$helm_v" ]; then err "cannot determine the Helm version"
  elif ! version_ge "$helm_v" "${HELM_MIN:-3.16}"; then err "helm $helm_v is older than the required ${HELM_MIN:-3.16}"
  elif [ "${helm_v%%.*}" -ge 4 ]; then echo "Note: helm $helm_v (the Canvas documents Helm 3; this deployment is exercised with Helm 4)"
  fi

  kind_v=$(kind version 2>/dev/null | sed -nE 's/^kind v([0-9.]+).*/\1/p')
  [ -n "$kind_v" ] || err "cannot determine the kind version"

  # Canvas checkout: chart present; revision reported (and checked when CANVAS_REF is set)
  if [ -n "${CANVAS_SRC:-}" ]; then
    [ -f "$CANVAS_SRC/charts/canvas-oda/Chart.yaml" ] || err "CANVAS_SRC wrong (no charts/canvas-oda/Chart.yaml): $CANVAS_SRC"
    if rev=$(git -C "$CANVAS_SRC" rev-parse --short=12 HEAD 2>/dev/null); then
      dirty=""; [ -z "$(git -C "$CANVAS_SRC" status --porcelain 2>/dev/null)" ] || dirty=" (+ uncommitted changes)"
      canvas_v=$(sed -nE 's/^version:\s*"?([^" ]+).*/\1/p' "$CANVAS_SRC/charts/canvas-oda/Chart.yaml" | head -n1)
      echo "Canvas source: $CANVAS_SRC @ $rev$dirty (canvas-oda $canvas_v)"
      if [ -n "${CANVAS_REF:-}" ]; then
        case "$(git -C "$CANVAS_SRC" rev-parse HEAD)" in
          "$CANVAS_REF"*) ;;
          *) err "CANVAS_SRC is at $rev but CANVAS_REF=$CANVAS_REF is pinned in config.mk" ;;
        esac
      fi
    else
      warn "CANVAS_SRC is not a git checkout: its revision cannot be recorded"
    fi
  fi
  [ "$fail" != 0 ] || echo "Pre-flight OK: docker, kind $kind_v, kubectl, helm $helm_v"
}

check_secrets() {
  for v in "$@"; do
    val=${!v:-}
    { [ -n "$val" ] && [ "$val" != CHANGE_ME ]; } || err "set $v in config.env (cp config.env.example config.env)"
  done
  if [[ " $* " == *" LANGFUSE_INIT_USER_PASSWORD "* ]] && [ "${#LANGFUSE_INIT_USER_PASSWORD}" -lt 8 ]; then
    err "LANGFUSE_INIT_USER_PASSWORD must have at least 8 characters"
  fi
}

case "${1:-}" in
  tools)   check_tools ;;
  secrets) shift; check_secrets "$@" ;;
  *)       echo "usage: preflight.sh tools | secrets VAR..." >&2; exit 2 ;;
esac
exit "$fail"
