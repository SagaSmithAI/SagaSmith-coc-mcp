"""Canonical signing for short-lived MCP context receipts."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping

from sagasmith_core.integrity import canonical_json


def sign_receipt(payload: Mapping[str, Any], secret: bytes) -> dict[str, Any]:
    """Return one canonical JSON/HMAC receipt envelope."""

    value = dict(payload)
    value["signature"] = hmac.new(
        secret,
        canonical_json(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return value


def verify_receipt_signature(
    receipt: Any,
    secret: bytes,
    *,
    missing_error: str,
    invalid_error: str,
) -> dict[str, Any]:
    """Verify and unwrap one canonical JSON/HMAC receipt envelope."""

    if not isinstance(receipt, dict):
        raise ValueError(missing_error)
    payload = dict(receipt)
    signature = str(payload.pop("signature", ""))
    expected = hmac.new(
        secret,
        canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError(invalid_error)
    return payload


