"""Retention of the customer's message in the turn trace (architecture §9).

``AUDIT_MESSAGE_MODE`` chooses what the trace keeps: ``masked`` (default), ``full`` or
``omitted``. Masking is one implementation, not two that could diverge: it first applies the
DATA-01 minimization the LLM Adapter uses (``scrub_message``: e-mails, cards as ``****last4``,
phones, documents announced by a keyword, CPF, dates of birth) and then adds the rules a log
needs because nobody announces them: bare document numbers ("X1234567"), local phone formats
("8888-1234", "(11) 98765-4321") and any other run of seven or more digits. Amounts
("COP 1.250.000,00", "USD 1,250.50") and dates ("17/06/2026", "2026-06-17") stay readable.
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.llm_adapter.minimization import scrub_message


class AuditMessageMode(StrEnum):
    MASKED = "masked"
    FULL = "full"
    OMITTED = "omitted"


NUMBER_MASK = "[number]"
# Document numbers with a letter prefix and six or more digits (passports, "X1234567").
_ALPHANUMERIC_DOCUMENT = re.compile(r"(?<![\w-])[A-Za-z]{1,3}\d{6,}(?![\w])")
# Phones with an area code in parentheses: "(11) 98765-4321".
_AREA_CODE_PHONE = re.compile(r"\(\d{2,3}\)\s?\d{4,5}[- ]?\d{4}(?!\d)")
# Seven or more digits, optionally separated by single spaces or dashes: "8888-1234",
# account and document numbers. Dots and commas (amounts) and slashes (dates) break a run.
_LONG_NUMBER = re.compile(r"(?<![\w-])\d(?:[ -]?\d){6,}(?![\w])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def mask_message(text: str) -> str:
    masked = scrub_message(text)
    masked = _ALPHANUMERIC_DOCUMENT.sub(NUMBER_MASK, masked)
    masked = _AREA_CODE_PHONE.sub(NUMBER_MASK, masked)
    return _LONG_NUMBER.sub(
        lambda match: match.group(0) if _ISO_DATE.fullmatch(match.group(0)) else NUMBER_MASK,
        masked,
    )


def retain_message(text: str | None, mode: AuditMessageMode) -> str | None:
    if text is None or mode is AuditMessageMode.OMITTED:
        return None
    if mode is AuditMessageMode.FULL:
        return text
    return mask_message(text)
