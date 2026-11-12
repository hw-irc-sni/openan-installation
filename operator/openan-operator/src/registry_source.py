# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read access to an agent registry, whatever API it exposes: a complete list of (card, review status) and, where
the registry has one, a change feed used as a wake-up hint (every pass still diffs the complete list)."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .registry_client import RegistryClient, RegistryError

TMF639_BASE = "/tmf-api/resourceInventoryManagement/v5"
TMF639_PAGE = 100
FEED_PAGES = 20  # per tick; the rest is read on the next one


class SourceUnavailable(RuntimeError):
    """The registry could not be read completely: callers must not act on a partial view."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Feed:
    events: List[Dict[str, Any]]
    next_since: int


class NativeSource:
    supports_feed = True

    def __init__(self, client: RegistryClient):
        self._rc = client

    def list_agents(self) -> List[Tuple[Dict[str, Any], str]]:
        try:
            cards = self._rc.list_manage()
        except RegistryError as e:
            raise SourceUnavailable(str(e), e.status)
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}")
        out = []
        for c in cards:
            c = dict(c)
            status = c.pop("publishStatus", "published")
            c.pop("registration", None)
            out.append((c, status))
        return out

    def changes(self, since: int) -> Feed:
        events: List[Dict[str, Any]] = []
        cursor = since
        try:
            for _ in range(FEED_PAGES):
                page = self._rc.changes(cursor)
                events += page["changes"]
                cursor = int(page.get("next_since", cursor))
                if not page.get("has_more"):
                    break
        except RegistryError as e:
            raise SourceUnavailable(str(e), e.status)
        except (httpx.HTTPError, ValueError) as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}")
        return Feed(events, cursor)

    def head(self) -> int:
        """Current registry_version: the cursor starts here (events from before the operator started are not
        replayed; the first pass diffs the complete list instead)."""
        cursor = 0
        while True:
            feed = self.changes(cursor)
            if feed.next_since == cursor:
                return cursor
            cursor = feed.next_since


class Tmf639Source:
    """TMF639 A2A-agent profile: LogicalResources of category A2AAgent, signed card in characteristic agentCard."""
    supports_feed = False

    def __init__(self, base_url: str, token: str, client: Optional[httpx.Client] = None, timeout: float = 15.0):
        self._c = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._h = {"Authorization": f"Bearer {token}"}

    def list_agents(self) -> List[Tuple[Dict[str, Any], str]]:
        out: List[Tuple[Dict[str, Any], str]] = []
        offset = 0
        try:
            while True:
                r = self._c.get(f"{TMF639_BASE}/resource", headers=self._h,
                                params={"category": "A2AAgent", "offset": offset, "limit": TMF639_PAGE})
                if r.status_code not in (200, 206):
                    raise SourceUnavailable(f"tmf639 list returned {r.status_code}: {r.text[:200]}", r.status_code)
                page = r.json()
                if not isinstance(page, list):
                    raise SourceUnavailable("tmf639 list is not an array")
                for res in page:
                    chars = {c.get("name"): c.get("value") for c in res.get("resourceCharacteristic") or []
                             if isinstance(c, dict)}
                    card = chars.get("agentCard")
                    if isinstance(card, dict):
                        out.append((card, str(chars.get("reviewStatus") or "published")))
                total = r.headers.get("X-Total-Count")
                offset += len(page)
                if not page or (total is not None and offset >= int(total)) or (total is None and len(page) < TMF639_PAGE):
                    return out
        except httpx.HTTPError as e:
            raise SourceUnavailable(f"{type(e).__name__}: {e}")
        except ValueError as e:
            raise SourceUnavailable(f"tmf639 list is not valid JSON: {e}")

    def changes(self, since: int) -> Feed:
        raise NotImplementedError("the TMF639 profile has no change cursor")
