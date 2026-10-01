"""Pseudonymous customer references (DATA-01, policy §13 ``customer_ref``).

The LLM context and the handoff packet name the customer only by a keyed hash of the
``customer_id``, so neither carries the identifier itself. The key is ``DOCUMENT_HASH_KEY`` with
a separate domain prefix, so a customer reference never equals a document hash.
"""

from __future__ import annotations

import hashlib
import hmac

UNAUTHENTICATED_REF = "CUS-unauthenticated"
_DOMAIN = b"customer_ref:"


def customer_ref(customer_id: str, key: str) -> str:
    """``CUS-`` and 16 hex characters of HMAC-SHA256; stable for one key."""
    if not key:
        raise ValueError("a pseudonym key is required")
    digest = hmac.new(key.encode(), _DOMAIN + customer_id.encode(), hashlib.sha256).hexdigest()
    return f"CUS-{digest[:16]}"
