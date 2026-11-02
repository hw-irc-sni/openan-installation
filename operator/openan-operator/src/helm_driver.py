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


def _stale_after(raw: str | None) -> float:
    """The staleness window; never below UPGRADE_TIMEOUT + 30 s, or a live operation could be mistaken for a dead one."""
    try:
        wanted = float(raw) if raw else 150.0
    except ValueError:
        wanted = 150.0
    return max(wanted, UPGRADE_TIMEOUT + 30.0)


STALE_AFTER = _stale_after(os.environ.get("HELM_PENDING_STALE_SECONDS"))


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

    Returns "rolled-back" (back to the last good revision), "uninstalled" (a first install that never completed: there
    is nothing to lose, the next `upgrade --install` installs it afresh) or None (healthy, or the pending revision is
    still young enough to belong to a running operation).

    It never uninstalls a release that has been installed before: the Canvas adopts its data PVCs (ownerReference to
    the Component), so removing the Component would delete the data. Such a release with no good revision to roll back
    to raises HelmError instead (the reconcile keeps retrying and the error stays visible)."""
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
    elif last["status"] == "pending-install" and last["revision"] == 1:
        log.warning("helm %s: first install stuck in pending-install for %.0fs; uninstalling it to install afresh",
                    release, age)
        uninstall(release, namespace)
        action = "uninstalled"
    else:
        raise HelmError(
            f"{release}: revision {last['revision']} stuck in {last['status']} for {age:.0f}s and there is no good "
            "revision to roll back to; not uninstalling a release that may hold data (fix or delete it manually)")
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
