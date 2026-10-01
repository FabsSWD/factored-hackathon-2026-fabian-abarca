"""Retention of the customer's message in the turn trace (architecture §9).

``AUDIT_MESSAGE_MODE`` chooses what the trace keeps: ``masked`` (default) replaces e-mail
addresses, long digit runs (document, card, account, and phone numbers) and letter-prefixed
document numbers, and keeps the rest,
so a reviewer still sees what was asked; ``full`` keeps the text; ``omitted`` keeps nothing.
"""

from __future__ import annotations

import re
from enum import StrEnum


class AuditMessageMode(StrEnum):
    MASKED = "masked"
    FULL = "full"
    OMITTED = "omitted"


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Seven or more digits, optionally separated by single spaces or dashes: documents, cards,
# accounts, phone numbers. Amounts ("1,250.00", "400000") and short references are kept.
_LONG_NUMBER = re.compile(r"(?<![\w-])\d(?:[ -]?\d){6,}(?![\w])")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Document numbers with a letter prefix and six or more digits (e.g. passports "X1234567").
_ALPHANUMERIC_DOCUMENT = re.compile(r"(?<![\w-])[A-Za-z]{1,3}\d{6,}(?![\w])")
EMAIL_MASK = "[email]"
NUMBER_MASK = "[number]"


def mask_message(text: str) -> str:
    """Replace e-mail addresses and long digit runs; ISO dates stay readable."""
    masked = _ALPHANUMERIC_DOCUMENT.sub(NUMBER_MASK, _EMAIL.sub(EMAIL_MASK, text))
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
