# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Registry Center client: idempotent upsert/delete of agent cards (identity = organization + name)."""
from typing import Any, Dict
from urllib.parse import quote

import httpx

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

    def upsert(self, card: Dict[str, Any]) -> str:
        """Create or replace; returns 'created' or 'updated'."""
        org, name = card["provider"]["organization"], card["name"]
        body = {"agentCards": [card]}
        if self.get(org, name) is None:
            r = self._c.post(f"{BASE}/agent-cards", json=body, headers=self._h)
            if r.status_code == 409:  # raced with another writer: fall through to replace
                pass
            elif r.status_code in (200, 201):
                return "created"
            else:
                raise RegistryError(r.status_code, r.text)
        r = self._c.put(self._path(org, name), json=body, headers=self._h)
        if r.status_code not in (200, 201):
            raise RegistryError(r.status_code, r.text)
        return "updated"

    def delete(self, org: str, name: str) -> bool:
        r = self._c.delete(self._path(org, name), headers=self._h)
        if r.status_code == 404:
            return False
        if r.status_code not in (200, 204):
            raise RegistryError(r.status_code, r.text)
        return True
