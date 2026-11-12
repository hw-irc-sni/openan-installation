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
# Read-only side of the agent sync: the change feed and the full card list need nms_oss. A separate identity owns no
# card (owner isolation compares identities), so this token can read everything and overwrite nothing it shouldn't;
# writes keep using OPERATOR_IDENTITY.
SYNC_IDENTITY, SYNC_CREDENTIAL = "openan-operator-sync", "sync"


def token_fingerprint(token: str, key: str) -> str:
    """Same algorithm as registry-center agent_registry.integration.authn.token_fingerprint."""
    return hmac.new(key.encode(), token.encode(), hashlib.sha256).hexdigest()


def access_password_hash(password: str) -> str:
    """Orchestration Center file-mode account hash (common/util/password_hash.py scheme "v4")."""
    digest = hashlib.sha256(b"openan-password-v4\0" + password.encode("utf-8")).hexdigest().encode("ascii")
    return "v4:" + bcrypt.hashpw(digest, bcrypt.gensalt()).decode("ascii")


def credentials_conf(entries, hmac_key: str) -> str:
    """entries: [(credential id, identity, token, role)] -> integration_credentials.conf."""
    lines = []
    for cid, identity, token, role in entries:
        lines += [f"credential.{cid}.identity={identity}",
                  f"credential.{cid}.token_hash={token_fingerprint(token, hmac_key)}",
                  f"credential.{cid}.role={role}", f"credential.{cid}.owner={identity}",
                  f"credential.{cid}.enabled=true"]
    return "\n".join(lines) + "\n"


def with_sync_credential(conf: str, token: str, hmac_key: str) -> str:
    """integration_credentials.conf with the sync credential set to `token` (replacing an older one, e.g. after its
    token Secret was lost); every other credential is kept verbatim."""
    prefix = f"credential.{SYNC_CREDENTIAL}."
    kept = [line for line in conf.splitlines() if line.strip() and not line.startswith(prefix)]
    return "\n".join(kept) + "\n" + credentials_conf([(SYNC_CREDENTIAL, SYNC_IDENTITY, token, "nms_oss")], hmac_key)


def new_stack_secrets(registry_url: str) -> Dict[str, Dict[str, str]]:
    """Return {secretName-suffix: stringData}. Callers must persist and never regenerate."""
    hmac_key, fp_key, token = secrets.token_hex(32), secrets.token_hex(32), secrets.token_urlsafe(48)
    oc_token = secrets.token_urlsafe(48)  # own identity: revocable and auditable apart from the operator
    admin = secrets.token_urlsafe(18)
    priv, pub = generate_keypair()
    return {
        "registry-runtime": {"INTEGRATION_TOKEN_HMAC_KEY": hmac_key, "INTEGRATION_FINGERPRINT_KEY": fp_key},
        "registry-credentials": {"integration_credentials.conf": credentials_conf(
            [("operator", OPERATOR_IDENTITY, token, "vendor_agent"),
             ("orchestration", "openan-orchestration-center", oc_token, "vendor_agent")], hmac_key)},
        "operator-token": {"token": token},
        "orchestration-runtime": {"OC_ADMIN_INITIAL_PASSWORD": admin, "ORCH_ACCESS_PASSWORD": access_password_hash(admin),
                                  "REGISTRY_ACCESS_TOKEN": oc_token},
        "signing-key": {"private.pem": priv, "public.pem": pub},
    }
