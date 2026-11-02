# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Thin wrapper around the helm CLI (idempotent upgrade --install / uninstall / status)."""
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List

import yaml

from . import metrics

log = logging.getLogger("openan.helm")

PENDING = frozenset({"pending-install", "pending-upgrade", "pending-rollback"})
GOOD = frozenset({"deployed", "superseded"})
# `upgrade --install` runs without --wait, so a legitimate operation takes seconds and the subprocess is killed after
# UPGRADE_TIMEOUT. A revision still pending after STALE_AFTER (> UPGRADE_TIMEOUT) therefore has no live owner (the process
# that started it was killed) and blocks every later upgrade with "another operation is in progress".
UPGRADE_TIMEOUT = 120
STALE_AFTER = float(os.environ.get("HELM_PENDING_STALE_SECONDS", "150"))


class HelmError(RuntimeError):
    pass


def _run(args: List[str], timeout: int = 300) -> str:
    p = subprocess.run(["helm", *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise HelmError(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def history(release: str, namespace: str) -> List[Dict[str, Any]]:
    """Revisions of a release as `helm history -o json` reports them ([] when the release does not exist)."""
    try:
        return json.loads(_run(["history", release, "-n", namespace, "-o", "json"]) or "[]")
    except HelmError as e:
        if "not found" in str(e):
            return []
        raise


def _parse_time(value: str) -> datetime:
    """Helm prints RFC 3339 with nanoseconds; fromisoformat takes at most microseconds."""
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00"))


def recover_if_stuck(release: str, namespace: str, stale_after: float | None = None,
                     now: datetime | None = None) -> str | None:
    """Repair a release whose latest revision has been pending for longer than any live operation could take.

    Returns "rolled-back" (back to the last good revision), "uninstalled" (nothing to go back to: the next
    `upgrade --install` installs it afresh; data PVCs are kept by their resource policy) or None (healthy, or the
    pending revision is still young enough to belong to a running operation)."""
    revisions = history(release, namespace)
    if not revisions:
        return None
    last = max(revisions, key=lambda r: r["revision"])
    if last["status"] not in PENDING:
        return None
    age = ((now or datetime.now(timezone.utc)) - _parse_time(last["updated"])).total_seconds()
    if age < (STALE_AFTER if stale_after is None else stale_after):
        return None
    good = [r for r in revisions if r["status"] in GOOD and r["revision"] < last["revision"]]
    if last["status"] != "pending-install" and good:
        target = max(good, key=lambda r: r["revision"])["revision"]
        log.warning("helm %s: revision %s stuck in %s for %.0fs; rolling back to revision %s",
                    release, last["revision"], last["status"], age, target)
        _run(["rollback", release, str(target), "-n", namespace], timeout=UPGRADE_TIMEOUT)
        action = "rolled-back"
    else:
        log.warning("helm %s: revision %s stuck in %s for %.0fs and there is no good revision; uninstalling",
                    release, last["revision"], last["status"], age)
        uninstall(release, namespace)
        action = "uninstalled"
    metrics.HELM_RECOVERIES.labels(action).inc()
    return action


def upgrade_install(release: str, chart_path: str, namespace: str, values: Dict[str, Any]) -> str | None:
    """Idempotent install/upgrade. A stuck pending revision is repaired first; the repair (if any) is returned."""
    recovered = recover_if_stuck(release, namespace)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.safe_dump(values, f)
        f.flush()
        _run(["upgrade", "--install", release, chart_path, "-n", namespace, "-f", f.name], timeout=UPGRADE_TIMEOUT)
    return recovered


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
