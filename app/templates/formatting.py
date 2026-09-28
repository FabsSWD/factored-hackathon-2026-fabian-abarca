"""Values inserted into templates: masked products (COM-06), amounts and dates."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

MASK = "****"
MASKED_PRODUCT = re.compile(r"^\*{4}\d{4}$")

# Currencies written with a dot for thousands and a comma for decimals (Colombia, Argentina,
# Brazil). USD is written 1,250.00, as in Mexico and the United States.
_DECIMAL_COMMA = frozenset({"COP", "ARS", "BRL"})


def mask_product_number(number: str) -> str:
    """Keep only the last four digits: ``4111111111114821`` -> ``****4821`` (COM-06).

    An already masked value (``****4821``, as in ProductRecord) is returned unchanged.
    """
    digits = re.sub(r"\D", "", number)
    if len(digits) < 4:
        raise ValueError("a product number needs at least four digits")
    return f"{MASK}{digits[-4:]}"


def is_masked(value: str) -> bool:
    return bool(MASKED_PRODUCT.match(value))


def format_amount(amount: Decimal, currency: str) -> str:
    """``1250.5, "USD"`` -> ``1,250.50 USD``; ``1250.5, "COP"`` -> ``1.250,50 COP``."""
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.2f}"
    if currency.upper() in _DECIMAL_COMMA:
        text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{text} {currency.upper()}"


def format_date(value: date | datetime) -> str:
    """Day first in both languages: ``16/06/2026``."""
    day = value.date() if isinstance(value, datetime) else value
    return day.strftime("%d/%m/%Y")
