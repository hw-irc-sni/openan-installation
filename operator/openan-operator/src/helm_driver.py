# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Thin wrapper around the helm CLI (idempotent upgrade --install / uninstall / status)."""
import json
import subprocess
import tempfile
from typing import Any, Dict, List

import yaml


class HelmError(RuntimeError):
    pass


def _run(args: List[str], timeout: int = 300) -> str:
    p = subprocess.run(["helm", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise HelmError(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def upgrade_install(release: str, chart_path: str, namespace: str, values: Dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.safe_dump(values, f)
        f.flush()
        _run(["upgrade", "--install", release, chart_path, "-n", namespace, "-f", f.name])


def uninstall(release: str, namespace: str) -> bool:
    try:
        _run(["uninstall", release, "-n", namespace])
        return True
    except HelmError as e:
        if "not found" in str(e):
            return False
        raise


def status(release: str, namespace: str) -> str | None:
    try:
        return json.loads(_run(["status", release, "-n", namespace, "-o", "json"]))["info"]["status"]
    except HelmError as e:
        if "not found" in str(e):
            return None
        raise
