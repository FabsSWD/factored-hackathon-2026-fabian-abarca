"""Data contract for Core Banking: the values the system expects in the supplied data.

Sets are the values observed when profiling the dataset (2026-09-28, raw data 2023-06-17 to
2026-06-17), never the data dictionary's: the dictionary is in English and the data in Spanish.
Loading fails if a row carries a value outside these sets, and the database enforces them with
CHECK constraints, so a rule can never meet a value it was not written for.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from enum import StrEnum

TRANSACTION_TYPES = frozenset(
    {"Purchase", "Withdrawal", "Transfer", "Payment", "Deposit", "Adjustment"}
)  # policy §4
TRANSACTION_STATUSES = frozenset({"Approved", "Pending", "Declined", "Reversed"})  # GATE-06
PRODUCT_TYPES = frozenset(
    {
        "Cuenta Ahorro",
        "Cuenta Corriente",
        "Inversión",
        "Préstamo Hipotecario",
        "Préstamo Personal",
        "Seguro",
        "Tarjeta Crédito",
        "Tarjeta Débito",
    }
)
CARD_PRODUCT_TYPES = frozenset({"Tarjeta Crédito", "Tarjeta Débito"})  # ACT-03
PRODUCT_STATUSES = frozenset({"Active", "Blocked", "Closed", "Suspended"})  # GATE-09
CUSTOMER_STATUSES = frozenset({"Active", "Inactive", "Closed", "Suspended"})  # GATE-03
CURRENCIES = frozenset({"USD", "COP", "ARS"})


class AmountUsdSource(StrEnum):
    """How ``transactions.amount_usd`` was established (policy §6, glossary "USD equivalent")."""

    SOURCE = "source"  # supplied in the dataset
    FX_RATE = "fx_rate"  # amount x latest daily rate within FX_MAX_STALENESS_DAYS
    IDENTITY = "identity"  # the transaction is already in USD
    MISSING = "missing"  # no rate within the margin: the tier is unknown (treated as T3)


AGE_BANDS = ("18-24", "25-34", "35-44", "45-54", "55-64", "65+")
"""Stored instead of date_of_birth; used only for fairness reporting (DATA-02)."""


def age_band(age: int) -> str:
    """Band for an age in whole years. Ages under 18 are not expected in the data."""
    if age < 18:
        raise ValueError(f"unexpected customer age: {age}")
    for band, upper in zip(AGE_BANDS, (24, 34, 44, 54, 64), strict=False):
        if age <= upper:
            return band
    return AGE_BANDS[-1]


_DOCUMENT_NOISE = re.compile(r"[\s.\-]")


def normalize_document(document_number: str) -> str:
    """Uppercase, without spaces, dots or hyphens. Supplied documents are already in this form
    (uppercase letters and digits only), so their hashes do not change."""
    return _DOCUMENT_NOISE.sub("", document_number).upper()


def document_hash(document_number: str, key: str) -> str:
    """HMAC-SHA256 of a normalized document number; the number itself is never stored."""
    normalized = normalize_document(document_number).encode()
    return hmac.new(key.encode(), normalized, hashlib.sha256).hexdigest()
