# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
FROM python:3.12-slim
ARG HELM_VERSION=v3.19.0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tar \
 && ARCH="$(dpkg --print-architecture)" \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" -o /tmp/helm.tgz \
 && tar -xzf /tmp/helm.tgz -C /tmp && mv "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm \
 && rm -rf /var/lib/apt/lists/* /tmp/helm.tgz "/tmp/linux-${ARCH}"
WORKDIR /app
COPY operator/openan-operator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY operator/openan-operator/src ./src
COPY charts/openan-registry-center /charts/openan-registry-center
COPY charts/openan-orchestration-center /charts/openan-orchestration-center
COPY charts/openan-frontend /charts/openan-frontend
RUN useradd -u 10001 -m openan && mkdir -p /home/openan/.cache /home/openan/.config && chown -R openan /home/openan
USER 10001
ENV PYTHONPATH=/app CHARTS_DIR=/charts HELM_CACHE_HOME=/tmp/helm HELM_CONFIG_HOME=/tmp/helm HELM_DATA_HOME=/tmp/helm
CMD ["kopf", "run", "--standalone", "-A", "-m", "src.openan_operator"]
