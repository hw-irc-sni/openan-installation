#!/usr/bin/env bash
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Build the OpenAN Operator image (operator/openan-operator + the three ODA Component charts).
# Usage: build-operator.sh [--tag <tag>] [--image <repo>] [--push]
# The build context is the repository root because the image bundles charts/openan-*.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="openan/openan-operator"
TAG="0.1.0"
PUSH=false

while [ $# -gt 0 ]; do
    case "$1" in
        --tag)   TAG="${2:?--tag needs a value}"; shift 2 ;;
        --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
        --push)  PUSH=true; shift ;;
        -h|--help) sed -n 7,9p "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

docker build -f "$ROOT/operator/openan-operator/Dockerfile" -t "$IMAGE:$TAG" "$ROOT"
if [ "$PUSH" = true ]; then docker push "$IMAGE:$TAG"; fi
echo "built $IMAGE:$TAG"
