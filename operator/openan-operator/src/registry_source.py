# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read access to an agent registry, whatever API it exposes: a complete list of (card, review status) and, where
the registry has one (supports_feed), a change feed used as a wake-up hint: every pass still diffs the complete list.
Any read that is not complete raises SourceUnavailable, so callers never act on a partial view."""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx

from .agent_sync import PUBLISHED, Key, card_key
from .registry_client import RegistryClient, RegistryError

TMF639_BASE = "/tmf-api/resourceInventoryManagement/v5"
TMF639_PAGE = 100
FEED_PAGES = 20  # per tick; the rest is read on the next one

Agents = List[Tuple[Dict[str, Any], str]]


class SourceUnavailable(RuntimeError):
    """The registry could not be read completely."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@contextmanager
def unavailable_on_error() -> Iterator[None]:
    try:
        yield
    except RegistryError as e:
        raise SourceUnavailable(str(e), e.status)
    except (httpx.HTTPError, ValueError) as e:
        raise SourceUnavailable(f"{type(e).__name__}: {e}")


@dataclass(frozen=True)
class Feed:
    events: List[Dict[str, Any]]
    next_since: int


class NativeSource:
    supports_feed = True

    def __init__(self, client: RegistryClient):
        self._rc = client

    def list_agents(self) -> Agents:
        with unavailable_on_error():
            cards = self._rc.list_manage()
        out = []
        for c in cards:
            c = dict(c)
            status = c.pop("publishStatus", PUBLISHED)
            c.pop("registration", None)
            out.append((c, status))
        return out

    def changes(self, since: int) -> Feed:
        events: List[Dict[str, Any]] = []
        cursor = since
        with unavailable_on_error():
            for _ in range(FEED_PAGES):
                page = self._rc.changes(cursor)
                events += page["changes"]
                cursor = int(page.get("next_since", cursor))
                if not page.get("has_more"):
                    break
        return Feed(events, cursor)

    def head(self) -> int:
        """Current registry_version, for a cursor that has nothing stored to start from."""
        cursor = 0
        while True:
            feed = self.changes(cursor)
            if feed.next_since == cursor:
                return cursor
            cursor = feed.next_since


class Tmf639Source:
    """TMF639 A2A-agent profile: LogicalResources of category A2AAgent, signed card in characteristic agentCard.
    No change cursor: callers resync on a timer."""
    supports_feed = False

    def __init__(self, base_url: str, token: str, client: Optional[httpx.Client] = None, timeout: float = 15.0):
        self._c = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._h = {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _agent(resource: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], str]]:
        chars = {c.get("name"): c.get("value") for c in resource.get("resourceCharacteristic") or [] if isinstance(c, dict)}
        card = chars.get("agentCard")
        return (card, str(chars.get("reviewStatus") or PUBLISHED)) if isinstance(card, dict) else None

    def list_agents(self) -> Agents:
        out: Agents = []
        offset = 0
        with unavailable_on_error():
            while True:
                r = self._c.get(f"{TMF639_BASE}/resource", headers=self._h,
                                params={"category": "A2AAgent", "offset": offset, "limit": TMF639_PAGE})
                if r.status_code not in (200, 206):
                    raise SourceUnavailable(f"tmf639 list returned {r.status_code}: {r.text[:200]}", r.status_code)
                page = r.json()
                if not isinstance(page, list):
                    raise SourceUnavailable("tmf639 list is not an array")
                out += [a for a in (self._agent(res) for res in page) if a is not None]
                total = r.headers.get("X-Total-Count")
                offset += len(page)
                if not page or (total is not None and offset >= int(total)) or (total is None and len(page) < TMF639_PAGE):
                    return out


def find_agent(source, key: Key) -> Optional[Tuple[Dict[str, Any], str]]:
    """(card, status) of `key` in the complete listing: a plain GET only sees published cards."""
    return next(((c, s) for c, s in source.list_agents() if card_key(c) == key), None)
