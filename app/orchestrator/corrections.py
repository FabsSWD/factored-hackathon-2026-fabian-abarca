"""Corrections after a declined summary (policy §8), a contract for the Orchestrator (M12).

When the customer declines the COM-03 summary ("no, el monto está mal, eran 40"), the
extraction of that reply is a proposed correction, not a fact:

- A ``reason_code`` different from the established one never replaces it silently: the turn is
  a CLARIFY with ``clarify_reason_code`` and nothing else is applied until the customer answers.
- An amount in a declined reply without a change of reason corrects
  ``transaction_ref.amount``, not ``expected_amount``: the transaction identified was not the
  right one, so GATE-05 matches again (the transaction ID is dropped).
- Any other new slot is applied as a candidate for the Policy Engine to validate.
- ``confirmation`` is cleared, so the summary is shown again and a stale yes never confirms it.
- Deterministic backup (M18): when the extraction brings no amount at all, the reason is
  ``RC_INCORRECT_AMOUNT`` and the message has exactly one number, that number is the corrected
  ``expected_amount``. With ``extract@1.13.0`` the model stopped extracting the 40 of "no, el
  monto está mal, eran 40"; an amount correction must not depend on the model alone.

Found with the recorded answer ``tests/fixtures/llm/es_confirm_declined_amount.json``: the model
returned ``reason_code: RC_INCORRECT_AMOUNT`` and ``expected_amount: 40``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.contracts import ClarifyTarget, ReasonCode, Slots, TransactionRecord, TransactionRef

# 40, 40,50, 1.200, 1.200,50, 1,200.50: digits with thousands and decimal separators.
_NUMBER = re.compile(
    r"(?<![\w.,])\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?(?![\w])|(?<![\w.,])\d+(?:[.,]\d{1,2})?(?![\w])"
)

_CORRECTABLE = (
    "card_in_possession",
    "shared_credentials",
    "duplicate_ref",
    "expected_delivery_date",
    "merchant_contacted",
    "fee_ref",
)


@dataclass(frozen=True)
class Correction:
    """The slots to evaluate next, and whether the turn must ask for the reason first."""

    slots: Slots
    clarify_target: ClarifyTarget | None = None  # REASON_CODE when a new reason was proposed
    rematch: bool = False  # transaction_ref changed: GATE-05 matches again


def apply_declined_correction(
    established: Slots,
    proposed: Slots,
    identified: TransactionRecord | None = None,
    message: str = "",
) -> Correction:
    """Apply the extraction of a declined reply (``proposed``) to the conversation's slots.

    ``identified`` is the transaction the summary showed; its date and merchant keep narrowing
    the new match when the established reference was only an ID. ``message`` is the customer's
    reply, read for the deterministic amount backup."""
    if proposed.reason_code is not None and proposed.reason_code != established.reason_code:
        return Correction(
            slots=established.model_copy(update={"confirmation": None}),
            clarify_target=ClarifyTarget.REASON_CODE,
        )

    updates: dict[str, object] = {"confirmation": None}
    amount = _corrected_amount(proposed)
    reason = proposed.reason_code or established.reason_code
    if amount is None and reason is ReasonCode.INCORRECT_AMOUNT:
        backup = single_amount(message)
        if backup is not None:
            updates["expected_amount"] = backup  # the model gave no amount: the message's one
    ref_changes = _ref_changes(proposed.transaction_ref, amount)
    if ref_changes:
        updates["transaction_ref"] = _rematch_ref(
            established.transaction_ref, identified, ref_changes
        )
    for name in _CORRECTABLE:
        value = getattr(proposed, name)
        if value is not None:
            updates[name] = value
    return Correction(slots=established.model_copy(update=updates), rematch=bool(ref_changes))


def single_amount(message: str) -> Decimal | None:
    """The one number of a message as an amount (40, 40,50, 1.200, 1.200,50), or None when
    there is none or more than one (a date and an amount would be ambiguous)."""
    found = _NUMBER.findall(message)
    if len(found) != 1:
        return None
    text = found[0]
    if "." in text and "," in text:
        decimal = "." if text.rfind(".") > text.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        text = text.replace(thousands, "").replace(decimal, ".")
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        whole, _, tail = text.rpartition(sep)
        text = text.replace(sep, "") if len(tail) == 3 else f"{whole.replace(sep, '')}.{tail}"
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern only finds numbers
        return None
    return value if value > 0 else None


def _corrected_amount(proposed: Slots) -> Decimal | None:
    """The amount the customer gave: in a declined reply it always refers to the transaction."""
    if proposed.transaction_ref is not None and proposed.transaction_ref.amount is not None:
        return proposed.transaction_ref.amount
    return proposed.expected_amount


def _ref_changes(ref: TransactionRef | None, amount: Decimal | None) -> dict[str, object]:
    changes: dict[str, object] = {}
    if ref is not None:
        if ref.transaction_date is not None:
            changes["transaction_date"] = ref.transaction_date
        if ref.merchant is not None:
            changes["merchant"] = ref.merchant
    if amount is not None:
        changes["amount"] = amount
    return changes


def _rematch_ref(
    established: TransactionRef | None,
    identified: TransactionRecord | None,
    changes: dict[str, object],
) -> TransactionRef:
    base: dict[str, object] = {}
    if identified is not None:
        base["transaction_date"] = identified.transaction_date.date()
        if identified.merchant_name:
            base["merchant"] = identified.merchant_name
    if established is not None:
        base.update(
            {
                key: value
                for key, value in established.model_dump(exclude={"transaction_id"}).items()
                if value is not None
            }
        )
    base.update(changes)
    base.pop("transaction_id", None)  # the identified transaction was not the right one
    return TransactionRef.model_validate(base)
