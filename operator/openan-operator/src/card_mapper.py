# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Map an A2A agent card (v0.3 or v1 shape) to the Registry Center AgentCard schema."""
import re
from typing import Any, Dict, Optional

NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+(\s+[a-zA-Z0-9_-]+)*$")
MAX_NAME, MAX_DESC, MAX_VERSION, MAX_SKILLS = 100, 1000, 50, 100


class CardError(ValueError):
    pass


def _sanitize_name(name: str, allow_spaces: bool = True) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\s-]", "-", name or "").strip()
    cleaned = re.sub(r"\s+", " " if allow_spaces else "-", cleaned)[:MAX_NAME].strip()
    if not NAME_RE.match(cleaned or ""):
        raise CardError(f"cannot derive a valid registry name from {name!r}")
    return cleaned


def to_registry_card(card: Dict[str, Any], organization: str, fallback_url: Optional[str] = None,
                     allow_spaces: bool = True) -> Dict[str, Any]:
    """Return a Registry Center card. Raises CardError if mandatory data is missing."""
    if not organization:
        raise CardError("organization is required")
    interfaces = card.get("supportedInterfaces")
    if not interfaces:  # pre-v1 card: top-level url + preferredTransport
        url = card.get("url") or fallback_url
        if not url:
            raise CardError("card has neither supportedInterfaces nor url")
        interfaces = [{"url": url, "protocolBinding": card.get("preferredTransport", "JSONRPC"),
                       "protocolVersion": card.get("protocolVersion", "0.3.0")}]
    interfaces = [{"url": i["url"], "protocolBinding": i.get("protocolBinding") or i.get("transport", "JSONRPC"),
                   "protocolVersion": i.get("protocolVersion", "1.0")} for i in interfaces][:3]
    skills = [{"id": str(s["id"]), "name": s.get("name") or str(s["id"]),
               "description": s.get("description") or s.get("name") or str(s["id"]),
               **({"tags": s["tags"]} if s.get("tags") else {})} for s in (card.get("skills") or [])][:MAX_SKILLS]
    description = (card.get("description") or card.get("name") or "").strip()[:MAX_DESC]
    if not description:
        raise CardError("card has no description or name")
    out = {
        "name": _sanitize_name(card.get("name", ""), allow_spaces),
        "description": description,
        "version": str(card.get("version") or "0.0.0")[:MAX_VERSION],
        "provider": {"organization": organization[:100], **({"url": card["provider"]["url"]}
                     if isinstance(card.get("provider"), dict) and card["provider"].get("url") else {})},
        "capabilities": card.get("capabilities") or {},
        "supportedInterfaces": interfaces,
        "skills": skills,
        "defaultInputModes": card.get("defaultInputModes") or ["text/plain"],
        "defaultOutputModes": card.get("defaultOutputModes") or ["text/plain"],
    }
    return out
