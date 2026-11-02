# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
from prometheus_client import Counter, Gauge, start_http_server

RECONCILES = Counter("openan_operator_reconcile_total", "OpenAN reconciliations", ["result"])
CARD_SYNC = Counter("openan_operator_card_sync_total", "Agent card sync operations", ["action", "result"])
HELM_RECOVERIES = Counter("openan_operator_helm_recoveries_total", "Stuck pending Helm releases repaired", ["action"])
CARDS = Gauge("openan_operator_synced_cards", "Agent cards currently synced")


def start(port: int = 8000) -> None:
    start_http_server(port)
