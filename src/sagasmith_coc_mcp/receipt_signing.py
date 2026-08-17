"""MCP naming facade for core canonical envelope integrity primitives."""

from sagasmith_core.integrity import (
    sign_canonical_envelope as sign_receipt,
)
from sagasmith_core.integrity import (
    verify_canonical_envelope as verify_receipt_signature,
)

__all__ = ["sign_receipt", "verify_receipt_signature"]

