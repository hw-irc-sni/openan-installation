# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate (once) the secret material an OpenAN stack needs. Pure functions, no cluster access."""
import hashlib
import hmac
import secrets

import bcrypt
from typing import Dict

from .card_signer import generate_keypair

OPERATOR_IDENTITY = "openan-operator"


def token_fingerprint(token: str, key: str) -> str:
    """Same algorithm as registry-center agent_registry.integration.authn.token_fingerprint."""
    return hmac.new(key.encode(), token.encode(), hashlib.sha256).hexdigest()


def access_password_hash(password: str) -> str:
    """Orchestration Center file-mode account hash (common/util/password_hash.py scheme "v4")."""
    digest = hashlib.sha256(b"openan-password-v4\0" + password.encode("utf-8")).hexdigest().encode("ascii")
    return "v4:" + bcrypt.hashpw(digest, bcrypt.gensalt()).decode("ascii")


def credentials_conf(token: str, hmac_key: str, identity: str = OPERATOR_IDENTITY, role: str = "vendor_agent") -> str:
    cid = "operator"
    return "\n".join([
        f"credential.{cid}.identity={identity}",
        f"credential.{cid}.token_hash={token_fingerprint(token, hmac_key)}",
        f"credential.{cid}.role={role}",
        f"credential.{cid}.owner={identity}",
        f"credential.{cid}.enabled=true", ""])


def new_stack_secrets(registry_url: str) -> Dict[str, Dict[str, str]]:
    """Return {secretName-suffix: stringData}. Callers must persist and never regenerate."""
    hmac_key, fp_key, token = secrets.token_hex(32), secrets.token_hex(32), secrets.token_urlsafe(48)
    admin = secrets.token_urlsafe(18)
    priv, pub = generate_keypair()
    return {
        "registry-runtime": {"INTEGRATION_TOKEN_HMAC_KEY": hmac_key, "INTEGRATION_FINGERPRINT_KEY": fp_key},
        "registry-credentials": {"integration_credentials.conf": credentials_conf(token, hmac_key)},
        "operator-token": {"token": token},
        "orchestration-runtime": {"OC_ADMIN_INITIAL_PASSWORD": admin, "ORCH_ACCESS_PASSWORD": access_password_hash(admin),
                                  "REGISTRY_ACCESS_TOKEN": token},
        "signing-key": {"private.pem": priv, "public.pem": pub},
    }
