"""Canonical request fingerprints for idempotent transfers.

The digest identifies the logical transfer payload. It is not authentication.
Scope is fixed to the laboratory transfer API; there is no tenant model yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

TRANSFER_SCOPE = "lab.transfer"
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def validate_idempotency_key(key: str) -> str:
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "Idempotency-Key must be 8-128 characters of A-Z, a-z, 0-9, '.', '_', ':', or '-'"
        )
    return key


def transfer_fingerprint(
    *,
    source_account_id: UUID,
    destination_account_id: UUID,
    amount_minor: int,
    currency: str,
) -> str:
    payload = {
        "amount_minor": amount_minor,
        "currency": currency.upper(),
        "destination_account_id": str(destination_account_id),
        "operation": "transfer",
        "scope": TRANSFER_SCOPE,
        "source_account_id": str(source_account_id),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
