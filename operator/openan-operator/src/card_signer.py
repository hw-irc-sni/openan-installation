# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""ES256 JWS signing of agent cards (A2A AgentCardSignature: protected + signature)."""
import base64
import json
from typing import Any, Dict, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def canonical(card: Dict[str, Any]) -> bytes:
    """Canonical JSON of the card without 'signatures' (sorted keys, no whitespace; RFC 8785 for our value set)."""
    body = {k: v for k, v in card.items() if k != "signatures"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def generate_keypair() -> Tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def public_jwk(public_pem: str, kid: str) -> Dict[str, str]:
    pub = serialization.load_pem_public_key(public_pem.encode())
    n = pub.public_numbers()
    return {"kty": "EC", "crv": "P-256", "alg": "ES256", "use": "sig", "kid": kid,
            "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big"))}


def sign_card(card: Dict[str, Any], private_pem: str, kid: str, jku: str = "") -> Dict[str, Any]:
    """Return a copy of card with one JWS in 'signatures'."""
    header = {"alg": "ES256", "typ": "JOSE", "kid": kid, **({"jku": jku} if jku else {})}
    protected = b64u(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    payload = b64u(canonical(card))
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    r, s = decode_dss_signature(key.sign(f"{protected}.{payload}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return {**{k: v for k, v in card.items() if k != "signatures"},
            "signatures": [{"protected": protected, "signature": sig}]}


def verify_card(card: Dict[str, Any], public_pem: str) -> bool:
    """Verify the first signature (used by tests and self-checks)."""
    from cryptography.exceptions import InvalidSignature
    sig = card["signatures"][0]
    raw = base64.urlsafe_b64decode(sig["signature"] + "==")
    der = __import__("cryptography.hazmat.primitives.asymmetric.utils", fromlist=["x"]).encode_dss_signature(
        int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = serialization.load_pem_public_key(public_pem.encode())
    try:
        pub.verify(der, f"{sig['protected']}.{b64u(canonical(card))}".encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
