# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Registry Center client: idempotent upsert/delete of agent cards (identity = organization + name)."""
from typing import Any, Dict
from urllib.parse import quote

import httpx

from .cards import OwnershipError, card_owner

BASE = "/rest/v1/registry-center"


class RegistryError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"registry returned {status}: {body[:300]}")
        self.status = status


class RegistryClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None, timeout: float = 15.0):
        self._c = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._h = {"Authorization": f"Bearer {token}"}

    def _path(self, org: str, name: str) -> str:
        return f"{BASE}/agent-cards/{quote(org, safe='')}/{quote(name, safe='')}"

    def health(self) -> bool:
        try:
            return self._c.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def get(self, org: str, name: str) -> Dict[str, Any] | None:
        r = self._c.get(self._path(org, name), headers=self._h)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RegistryError(r.status_code, r.text)
        body = r.json()
        # The registry answers 200 {"agentCards": []} for an unknown card.
        cards = body.get("agentCards") if isinstance(body, dict) else None
        if isinstance(body, dict) and "agentCards" in body:
            return cards[0] if cards else None
        return body

    def _check_owner(self, existing: Dict[str, Any] | None, owner: str | None, org: str, name: str) -> None:
        if owner is not None and existing is not None and card_owner(existing) != owner:
            raise OwnershipError(f"card {org}/{name} is owned by another source ({card_owner(existing) or 'unknown'})")

    def upsert(self, card: Dict[str, Any], owner: str | None = None) -> str:
        """Create or replace; returns 'created' or 'updated'.

        With `owner`, an existing card of another owner is never replaced: the check is repeated here, right before
        the write, and again after a 409 (another writer created the card between our read and our POST).
        """
        org, name = card["provider"]["organization"], card["name"]
        body = {"agentCards": [card]}
        existing = self.get(org, name)
        self._check_owner(existing, owner, org, name)
        if existing is None:
            r = self._c.post(f"{BASE}/agent-cards", json=body, headers=self._h)
            if r.status_code in (200, 201):
                return "created"
            if r.status_code != 409:
                raise RegistryError(r.status_code, r.text)
            # raced with another writer: re-read and re-check ownership before replacing what it wrote
            self._check_owner(self.get(org, name), owner, org, name)
        r = self._c.put(self._path(org, name), json=body, headers=self._h)
        if r.status_code not in (200, 201):
            raise RegistryError(r.status_code, r.text)
        return "updated"

    def list_manage(self) -> list:
        """Every card with its review status (`publishStatus`), unpaginated. Needs an nms_oss credential."""
        r = self._c.get(f"{BASE}/agent-cards/manage", headers=self._h)
        if r.status_code != 200:
            raise RegistryError(r.status_code, r.text)
        body = r.json()
        cards = body.get("agentCards") if isinstance(body, dict) else None
        if not isinstance(cards, list):
            raise RegistryError(r.status_code, "manage list without agentCards")
        return cards

    def changes(self, since: int, limit: int = 1000) -> Dict[str, Any]:
        """Change feed page: {"changes": [...], "has_more": bool, "next_since": int}. Needs an nms_oss credential."""
        r = self._c.get(f"{BASE}/changes", params={"since": since, "limit": limit}, headers=self._h)
        if r.status_code != 200:
            raise RegistryError(r.status_code, r.text)
        body = r.json()
        if not isinstance(body, dict) or not isinstance(body.get("changes"), list):
            raise RegistryError(r.status_code, "change feed page without changes")
        return body

    def delete(self, org: str, name: str) -> bool:
        r = self._c.delete(self._path(org, name), headers=self._h)
        if r.status_code == 404:
            return False
        if r.status_code not in (200, 204):
            raise RegistryError(r.status_code, r.text)
        return True
