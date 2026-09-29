"""DATA-01: only minimized data reaches the language model.

Two layers:

1. The context is serialized from an explicit allow-list of LLMContext fields, never with a
   generic dump, so a field attached by mistake (even bypassing validation) is dropped.
2. The customer's own message is scrubbed of personal data it may contain: emails, card
   numbers (kept as ****last4, COM-06), phone numbers, document numbers and dates of birth
   announced by a keyword, and CPF-shaped numbers. Names and street addresses cannot be
   detected reliably by rules; that is a documented limitation. Transaction amounts and dates
   are kept, because the model needs them to identify the transaction.
"""

from __future__ import annotations

import re
from typing import Any

from app.contracts import LLMContext

ALLOWED_CONTEXT_FIELDS = ("customer_ref", "language", "masked_products", "transactions",
                          "pending_slot")  # fmt: skip
ALLOWED_TRANSACTION_FIELDS = ("transaction_ref", "transaction_date", "amount", "currency",
                              "merchant_name", "transaction_status")  # fmt: skip

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_PHONE_INTL = re.compile(r"(?<![\w])\+\d[\d\s().-]{7,}\d")
_PHONE_KEYWORD = re.compile(
    r"(?i)\b(?:tel(?:efono|éfono|efone)?|cel(?:ular)?|m[oó]vil|whatsapp|fone)\b[\s:.#nº°-]*"
    r"(?:\(?\+?\d[\d\s().-]{5,}\d)"
)
_DOCUMENT_KEYWORD = re.compile(
    r"(?i)\b(?:documento(?: de identidad)?|doc|c[eé]dula|cc|ce|dni|pasaporte|passaporte|cpf|rg"
    r"|n[uú]mero de documento)\b[\s:.#nº°-]*(?:es |é |n[uú]mero )?([A-Za-z]{0,2}[\d.\-]{5,15}\w?)"
)
_CPF = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")
_BIRTH = re.compile(
    r"(?i)\b(?:fecha de nacimiento|nac[ií] el|naci el|data de nascimento|nasci em)\b"
    r"[\s:]*[\d/.\-]{6,10}"
)


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return f"****{digits[-4:]}"


def scrub_message(message: str) -> str:
    text = _EMAIL.sub("[email]", message)
    text = _BIRTH.sub("[fecha de nacimiento]", text)
    text = _DOCUMENT_KEYWORD.sub(lambda m: m.group(0).replace(m.group(1), "[documento]"), text)
    text = _CPF.sub("[documento]", text)
    text = _PHONE_KEYWORD.sub(
        lambda m: re.sub(r"\+?\d[\d\s().-]+\d", "[telefono]", m.group(0)), text
    )
    text = _PHONE_INTL.sub("[telefono]", text)
    return _CARD.sub(_mask_card, text)


def context_payload(context: LLMContext) -> dict[str, Any]:
    """The context as sent to the model, built only from allow-listed fields."""
    payload: dict[str, Any] = {}
    for name in ALLOWED_CONTEXT_FIELDS:
        value = getattr(context, name, None)
        if name == "transactions":
            payload[name] = [
                {
                    field: _plain(getattr(transaction, field, None))
                    for field in ALLOWED_TRANSACTION_FIELDS
                }
                for transaction in (value or [])
            ]
        elif name == "masked_products":
            payload[name] = [str(product) for product in (value or [])]
        else:
            payload[name] = _plain(value)
    return payload


def _plain(value: Any) -> Any:
    # StrEnum values (language, pending slot) are str already.
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)  # dates, decimals
