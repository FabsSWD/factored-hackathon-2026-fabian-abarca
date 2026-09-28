"""Values inserted into templates: masked products (COM-06), amounts and dates.

Amounts follow the customer's locale: the conversation language plus the customer's country
(DATA-02 allows country for presentation defaults; it never influences an outcome). The
currency code always comes first, because "$" is ambiguous between pesos and dollars.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.contracts import Language

MASK = "****"
MASKED_PRODUCT = re.compile(r"^\*{4}\d{4}$")


class Locale(StrEnum):
    ES_MX = "es-MX"
    ES_CO = "es-CO"
    ES_AR = "es-AR"
    PT_BR = "pt-BR"


# es-MX writes 1,250.00; Colombia, Argentina and Brazil write 1.250,00.
_DECIMAL_POINT = frozenset({Locale.ES_MX})

_SPANISH_BY_COUNTRY = {
    "mexico": Locale.ES_MX,
    "colombia": Locale.ES_CO,
    "argentina": Locale.ES_AR,
}
DEFAULT_SPANISH_LOCALE = Locale.ES_CO
"""Used when the country is unknown; amounts are only shown to authenticated customers, whose
country is always known in the supplied data."""


class FormattedAmount(str):
    """An amount already formatted by ``format_amount``. Templates accept nothing else."""

    __slots__ = ()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def locale_for(language: Language | str, country: str | None) -> Locale:
    """``("es", "México")`` -> es-MX; Portuguese conversations always use pt-BR."""
    lang = language.value if isinstance(language, Language) else language
    if lang == Language.PT.value:
        return Locale.PT_BR
    if lang != Language.ES.value:
        raise ValueError(f"unsupported language {language!r}")
    return _SPANISH_BY_COUNTRY.get(_fold(country or ""), DEFAULT_SPANISH_LOCALE)


def format_amount(amount: Decimal, currency: str, locale: Locale) -> FormattedAmount:
    """``USD 1,250.00`` for es-MX; ``USD 1.250,00`` for es-CO, es-AR and pt-BR."""
    code = currency.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError(f"invalid currency code {currency!r}")
    quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = f"{quantized:,.2f}"
    if locale not in _DECIMAL_POINT:
        text = text.replace(",", "_").replace(".", ",").replace("_", ".")
    return FormattedAmount(f"{code} {text}")


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


def format_date(value: date | datetime) -> str:
    """Day first in both languages: ``16/06/2026``."""
    day = value.date() if isinstance(value, datetime) else value
    return day.strftime("%d/%m/%Y")
